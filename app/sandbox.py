import contextvars
import logging
import os
import threading
import time
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from deepagents.backends import StateBackend
from deepagents.backends.protocol import SandboxBackendProtocol, ExecuteResponse
from deepagents_opensandbox import OpensandboxBackend
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.sync.sandbox import SandboxSync

from app.config import (
    SANDBOX_IDLE_TIMEOUT,
    SANDBOX_CLEANUP_INTERVAL,
    SANDBOX_USE_SERVER_PROXY,
    SANDBOX_DOMAIN,
    SANDBOX_API_KEY,
    SANDBOX_IMAGE,
    SANDBOX_READY_TIMEOUT,
    PPT_SKILL_DIR,
    PPT_CORE_DEPS,
)

logger = logging.getLogger(__name__)

#: 密钥两边名字不一致时的提示语，拼在 401 诊断里。
_KEY_NAMING_HINT = (
    "两侧密钥必须相同，但变量名不同：客户端读 OPEN_SANDBOX_API_KEY（本项目 .env），"
    "服务端读 ~/.sandbox.toml 的 [server].api_key，或服务端进程环境变量 "
    "OPENSANDBOX_SERVER_API_KEY（前缀顺序与客户端相反，最容易抄错的就是这里）。"
)


def mask_secret(value: Optional[str]) -> str:
    """把密钥转成可安全打日志的形式。"""
    if not value:
        return "未配置"
    if len(value) <= 4:
        return f"{'*' * len(value)}（{len(value)} 位）"
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}（{len(value)} 位）"


#: 判断「沙箱没了」而不是「命令本身失败」的异常特征。
_CONNECTION_ERROR_TYPES = frozenset({
    "SandboxConnectionException", "SandboxUnhealthyException",
    "ConnectError", "ConnectTimeout", "ConnectionError", "ConnectionResetError",
    "ReadTimeout", "ReadError", "RemoteProtocolError",
})
_CONNECTION_ERROR_HINTS = (
    "connection refused", "actively refused", "connect error", "connection reset",
    "10061", "10054", "connection aborted", "network connectivity error",
    "no route to host", "connection timed out",
)


def _is_connection_error(exc: BaseException) -> bool:
    """区分「沙箱连不上」与「命令执行失败」——只有前者值得重建沙箱重试。"""
    if {cls.__name__ for cls in type(exc).__mro__} & _CONNECTION_ERROR_TYPES:
        return True
    text = str(exc).lower()
    return any(hint in text for hint in _CONNECTION_ERROR_HINTS)


def build_connection_config() -> ConnectionConfigSync:
    """构造 OpenSandbox SDK 的连接配置。

    domain / api_key 显式传入（来自 OPEN_SANDBOX_DOMAIN / OPEN_SANDBOX_API_KEY），
    不依赖 SDK 内部的 os.getenv 兜底——这样客户端到底用的是什么值，在启动日志里
    一眼可见，出 401 时也能立刻判断是"没配"还是"配错"。

    ``use_server_proxy`` 更是没有环境变量入口（SDK 里是纯构造参数，默认 False），
    必须显式注入，否则 ``SandboxSync.create(image)`` 内部
    ``ConnectionConfigSync()`` 会一直用默认值。

    每次 ``SandboxSync.create()`` 都会对传入的 config 调
    ``with_transport_if_missing()`` 生成独立 transport 副本，因此这里复用一个
    config 实例是安全的，不会让多个沙箱共用同一条连接。
    """
    return ConnectionConfigSync(
        domain=SANDBOX_DOMAIN or None,
        api_key=SANDBOX_API_KEY or None,
        use_server_proxy=SANDBOX_USE_SERVER_PROXY,
    )


def _diagnose_sandbox_failure(config: ConnectionConfigSync, exc: BaseException) -> Tuple[str, str]:
    """探测失败后做一次轻量预检，把「连不上 / 密钥不匹配 / 创建超时」区分开。

    只看 SDK 抛出的异常是不够的：401 和网络错误都可能被包成同一类异常。所以这里
    直接用客户端密钥打一次 ``GET /v1/sandboxes``（最便宜的鉴权路由），按状态码定性。

    返回 ``(code, message)``：code 给接口/前端做分支，message 给人看。
    """
    domain = config.get_domain()
    key = config.get_api_key()
    url = f"{config.get_base_url()}/sandboxes"

    try:
        import httpx
    except ImportError:  # httpx 是 opensandbox 的依赖，理论上到不了这里
        return "unknown", f"探测失败，且无法预检（httpx 不可用）：{exc}"

    try:
        resp = httpx.get(url, headers=({"OPEN-SANDBOX-API-KEY": key} if key else {}), timeout=5.0)
    except Exception as net_err:
        return (
            "unreachable",
            f"连不上 OpenSandbox server（{domain}）：{net_err}。"
            f"请先执行 opensandbox-server（默认读取 ~/.sandbox.toml）。",
        )

    if resp.status_code in (401, 403):
        if not key:
            detail = "客户端没有配置密钥。"
        else:
            detail = f"客户端提供的密钥被拒绝（客户端用 {mask_secret(key)}）。"
        return "auth_mismatch", f"鉴权失败 HTTP {resp.status_code}：{detail}{_KEY_NAMING_HINT}"

    if resp.status_code >= 400:
        return "http_error", f"server 返回 HTTP {resp.status_code}：{resp.text[:160]}"

    # 能列沙箱 = server 可达且鉴权通过，那失败就在创建环节
    mro = {cls.__name__ for cls in type(exc).__mro__}
    text = str(exc).lower()
    if mro & {"SandboxTimeoutException", "SandboxReadyTimeoutException"} or "timed out" in text or "timeout" in text:
        return (
            "create_timeout",
            f"server 可达、鉴权通过，但创建沙箱超过 {SANDBOX_READY_TIMEOUT}s 仍未就绪。"
            f"最常见的原因是目标镜像还没拉到本地：先执行 docker pull {SANDBOX_IMAGE} 预热；"
            f"镜像已就绪仍超时，可调大 .env 里的 SANDBOX_READY_TIMEOUT 后重启。",
        )
    return "create_failed", f"server 可达、鉴权通过，但创建沙箱失败：{exc}"


class _SandboxEntry:
    def __init__(self, sandbox: SandboxSync, backend: OpensandboxBackend):
        self.sandbox = sandbox
        self.backend = backend
        self.last_used_at = time.time()
        self.lock = threading.Lock()
        self.provisioned = False
        self.provisioning_error: Optional[str] = None

    def touch(self):
        self.last_used_at = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used_at

    def close(self):
        try:
            self.sandbox.kill()
        except Exception:
            pass
        try:
            self.sandbox.close()
        except Exception:
            pass


def _provision_sandbox_with_ppt_skill(backend: OpensandboxBackend) -> Tuple[bool, str]:
    if not PPT_SKILL_DIR.exists():
        return False, f"local skill dir not found: {PPT_SKILL_DIR}"

    # 沙箱内的落点由目录名推导，别写死——改技能名时只动 app/config.py 一处。
    skill_name = PPT_SKILL_DIR.name
    upload_pairs: List[Tuple[str, bytes]] = []
    skip_dirs = {"__pycache__"}
    for root, dirs, files in os.walk(PPT_SKILL_DIR):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if fname.endswith(".pyc") or fname.startswith(".env"):
                continue
            local_path = os.path.join(root, fname)
            rel = os.path.relpath(local_path, PPT_SKILL_DIR).replace("\\", "/")
            sandbox_path = f"/skills/{skill_name}/{rel}"
            try:
                with open(local_path, "rb") as f:
                    upload_pairs.append((sandbox_path, f.read()))
            except Exception as e:
                logger.debug(f"Provisioning: skip unreadable {local_path}: {e}")

    BATCH = 50
    failed = 0
    for i in range(0, len(upload_pairs), BATCH):
        chunk = upload_pairs[i:i + BATCH]
        try:
            responses = backend.upload_files(chunk)
            for r in responses:
                if r.error:
                    failed += 1
        except Exception as e:
            logger.warning(f"Provisioning: upload batch failed: {e}")
            failed += len(chunk)

    try:
        # 装 pip。注意两点，缺一个都会静默失败：
        #   1. Ubuntu 24.04 的系统 Python 带 PEP 668 的 EXTERNALLY-MANAGED 标记，
        #      get-pip.py 不带 --break-system-packages 会直接被拒（而且旧写法把
        #      错误丢进了 /dev/null，日志里只剩一句 "deps install failed"）。
        #   2. 这个镜像里没有 ensurepip，所以 must 走 get-pip.py。
        backend.execute(
            "python3 -m pip --version >/dev/null 2>&1 || {"
            "  (curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py 2>/dev/null"
            "   || wget -q https://bootstrap.pypa.io/get-pip.py -O /tmp/get-pip.py 2>/dev/null)"
            "  && python3 /tmp/get-pip.py --quiet --break-system-packages 2>&1 | tail -2"
            "; } || true",
            timeout=180,
        )
        # 优先走国内镜像，失败再回落默认源。
        pkgs = " ".join(PPT_CORE_DEPS)
        install_res = backend.execute(
            f"python3 -m pip install --quiet --disable-pip-version-check --break-system-packages "
            f"-i https://pypi.tuna.tsinghua.edu.cn/simple {pkgs} 2>&1"
            f" || python3 -m pip install --quiet --disable-pip-version-check "
            f"--break-system-packages {pkgs} 2>&1",
            timeout=420,
        )
        deps_ok = install_res.exit_code == 0
        # 真正能 import 才算装好——只看 pip 的退出码不够。
        if deps_ok:
            verify = backend.execute(
                "python3 -c \"import pptx, sys; print('python-pptx', pptx.__version__)\" 2>&1",
                timeout=60,
            )
            deps_ok = verify.exit_code == 0
            install_res = verify if not deps_ok else install_res
    except Exception as e:
        logger.warning(f"Provisioning: dep install error: {e}")
        deps_ok = False
        install_res = None

    deps_tail = ""
    if install_res is not None and not deps_ok:
        deps_tail = " | pip: " + (install_res.output or "")[-300:].replace("\n", " ")

    msg = (
        f"uploaded {len(upload_pairs)} files ({failed} failed); "
        f"deps install {'ok' if deps_ok else 'failed'}{deps_tail}"
    )
    return deps_ok, msg


class SandboxManager:
    def __init__(self, image: str, idle_timeout: int = SANDBOX_IDLE_TIMEOUT,
                 cleanup_interval: int = SANDBOX_CLEANUP_INTERVAL):
        self._image = image
        self._connection_config = build_connection_config()
        self._idle_timeout = idle_timeout
        self._cleanup_interval = cleanup_interval
        self._entries: Dict[str, _SandboxEntry] = {}
        self._global_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None
        self._available = False
        #: 不可用时的定性结果，供 /api/sandbox/status 与前端展示
        self._availability_code = "not_probed"
        self._availability_reason = "尚未探测沙箱可用性"

    @property
    def availability_code(self) -> str:
        """ok / unreachable / auth_mismatch / create_timeout / create_failed / http_error / unknown"""
        return "ok" if self._available else self._availability_code

    @property
    def availability_reason(self) -> str:
        return "" if self._available else self._availability_reason

    def start(self):
        probe = None
        try:
            logger.info(
                f"SandboxManager: probing sandbox availability "
                f"(domain={self._connection_config.get_domain()}, "
                f"api_key={mask_secret(self._connection_config.get_api_key())}, "
                f"use_server_proxy={self._connection_config.use_server_proxy}, "
                f"image={self._image})..."
            )
            probe = SandboxSync.create(
                self._image,
                connection_config=self._connection_config,
                ready_timeout=timedelta(seconds=SANDBOX_READY_TIMEOUT),
            )
            probe.kill()
            probe.close()
            self._available = True
            self._availability_code = "ok"
            self._availability_reason = ""
            logger.info("SandboxManager: sandbox available, starting cleanup thread.")
        except Exception as e:
            # 预检一次，把 401 / 连不上 / 创建超时 分开说清楚，而不是丢一句原始异常
            code, reason = _diagnose_sandbox_failure(self._connection_config, e)
            self._available = False
            self._availability_code = code
            self._availability_reason = reason
            logger.warning(f"SandboxManager: sandbox unavailable [{code}] — {reason}")
            return

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, name="sandbox-cleanup", daemon=True,
        )
        self._cleanup_thread.start()

    def stop(self):
        self._stop_event.set()
        if self._cleanup_thread and self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

        with self._global_lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            with e.lock:
                e.close()
        logger.info(f"SandboxManager: stopped, cleaned up {len(entries)} sandbox(es).")

    @property
    def available(self) -> bool:
        return self._available

    def _cleanup_loop(self):
        logger.info(
            f"SandboxManager: cleanup thread started "
            f"(idle_timeout={self._idle_timeout}s, check_interval={self._cleanup_interval}s)."
        )
        while not self._stop_event.is_set():
            try:
                self._reap_expired()
            except Exception as e:
                logger.error(f"SandboxManager: cleanup error: {e}", exc_info=True)
            self._stop_event.wait(self._cleanup_interval)
        logger.info("SandboxManager: cleanup thread stopped.")

    def _reap_expired(self):
        to_remove: List[Tuple[str, _SandboxEntry]] = []
        with self._global_lock:
            items = list(self._entries.items())

        if not items:
            logger.debug("SandboxManager: cleanup scan — no active sandboxes.")
            return

        for org_id, entry in items:
            with entry.lock:
                idle = entry.idle_seconds
            remaining = self._idle_timeout - idle
            if remaining <= 0:
                logger.info(
                    f"SandboxManager: [SCAN] org={org_id} idle={idle:.0f}s "
                    f"(>= threshold {self._idle_timeout}s) → WILL RECLAIM"
                )
                to_remove.append((org_id, entry))
            else:
                logger.debug(
                    f"SandboxManager: [SCAN] org={org_id} idle={idle:.0f}s "
                    f"(recycle in {remaining:.0f}s)"
                )

        if not to_remove:
            logger.debug(
                f"SandboxManager: cleanup scan complete — "
                f"{len(items)} sandbox(es) active, 0 reclaimed."
            )
            return

        reclaimed = 0
        for org_id, entry in to_remove:
            with self._global_lock:
                cur = self._entries.get(org_id)
                if cur is entry:
                    del self._entries[org_id]
                else:
                    logger.warning(
                        f"SandboxManager: org={org_id} entry changed before "
                        f"reclamation, skipping."
                    )
                    continue
            with entry.lock:
                idle_val = entry.idle_seconds
                entry.close()
            reclaimed += 1
            logger.info(
                f"SandboxManager: [RECLAIMED] org={org_id} sandbox destroyed "
                f"(was idle {idle_val:.0f}s)."
            )

        still_active = len(items) - reclaimed
        logger.info(
            f"SandboxManager: cleanup scan complete — "
            f"scanned={len(items)}, reclaimed={reclaimed}, still_active={still_active}."
        )

    def get_backend(self, org_id: str) -> Optional[OpensandboxBackend]:
        if not self._available:
            return None

        with self._global_lock:
            entry = self._entries.get(org_id)
            if entry is None:
                logger.info(f"SandboxManager: creating new sandbox for org={org_id}")
                sandbox = SandboxSync.create(
                    self._image,
                    connection_config=self._connection_config,
                    ready_timeout=timedelta(seconds=SANDBOX_READY_TIMEOUT),
                )
                backend = OpensandboxBackend(sandbox=sandbox)
                entry = _SandboxEntry(sandbox=sandbox, backend=backend)
                self._entries[org_id] = entry
                need_provision = True
            else:
                logger.debug(f"SandboxManager: reusing existing sandbox for org={org_id}")
                need_provision = False

        with entry.lock:
            entry.touch()
            backend = entry.backend

        if need_provision:
            t = threading.Thread(
                target=self._provision_entry,
                args=(org_id, entry),
                name=f"ppt-provision-{org_id}",
                daemon=True,
            )
            t.start()

        return backend

    @staticmethod
    def _provision_entry(org_id: str, entry: "_SandboxEntry"):
        try:
            ok, msg = _provision_sandbox_with_ppt_skill(entry.backend)
            with entry.lock:
                entry.provisioned = ok
                entry.provisioning_error = None if ok else msg
            logger.info(f"SandboxManager: PPT provisioning for org={org_id} → {msg}")
        except Exception as e:
            with entry.lock:
                entry.provisioned = False
                entry.provisioning_error = str(e)
            logger.warning(f"SandboxManager: PPT provisioning failed for org={org_id}: {e}")

    def get_sandbox_count(self) -> int:
        with self._global_lock:
            return len(self._entries)

    def invalidate(self, org_id: Optional[str]) -> None:
        """丢弃某个 org 的沙箱条目（已失效或连不上时）。

        下一个请求会通过 :meth:`get_backend` 重建容器，并重新跑一次 provisioning。
        """
        if not org_id:
            return
        with self._global_lock:
            entry = self._entries.pop(org_id, None)
        if entry is None:
            return
        with entry.lock:
            entry.close()
        logger.info(f"SandboxManager: invalidated sandbox for org={org_id}")


_org_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "org_id", default=None
)


def _set_current_org(org_id: Optional[str]):
    _org_id_var.set(org_id)


def _get_current_org() -> Optional[str]:
    return _org_id_var.get()


_sandbox_manager: Optional[SandboxManager] = None


def init_sandbox_manager(image: str) -> SandboxManager:
    global _sandbox_manager
    _sandbox_manager = SandboxManager(image)
    _sandbox_manager.start()
    return _sandbox_manager


def get_sandbox_manager() -> Optional[SandboxManager]:
    return _sandbox_manager


def _sandbox_available() -> bool:
    return _sandbox_manager is not None and _sandbox_manager.available


def sandbox_status() -> Dict[str, Any]:
    """``/api/sandbox/status`` 的返回体。

    除 ``available`` / ``active_count`` 外，额外给出 ``code`` + ``reason``：
    不可用时把原因（连不上 / 密钥不匹配 / 创建超时…）一并说明，前端可直接展示，
    不用再去翻服务端日志猜 401 的来龙去脉。
    """
    manager = _sandbox_manager
    if manager is None:
        return {
            "available": False,
            "active_count": 0,
            "code": "not_initialized",
            "reason": "沙箱管理器未初始化（应用尚未完成启动）。",
        }
    return {
        "available": manager.available,
        "active_count": manager.get_sandbox_count() if manager.available else 0,
        "code": manager.availability_code,
        "reason": manager.availability_reason,
    }


def _get_org_backend(org_id: Optional[str] = None) -> Optional[OpensandboxBackend]:
    if _sandbox_manager is None or not _sandbox_manager.available:
        return None
    target = org_id if org_id is not None else _get_current_org()
    if target is None:
        return None
    return _sandbox_manager.get_backend(target)


def run_in_sandbox(org_id: Optional[str], fn):
    """在指定 org 的沙箱上执行 ``fn(backend)``；连不上就重建沙箱重试一次。

    给不走 ``_OrgScopedSandboxBackendProxy`` 的调用方用（``/api/sandbox/*``、
    文件上传/下载）：它们拿到的是原始 backend，没有代理层的自愈能力。
    """
    backend = _get_org_backend(org_id)
    if backend is None:
        raise RuntimeError("沙箱服务不可用，或当前组织没有可用的沙箱容器。")
    try:
        return fn(backend)
    except Exception as exc:  # noqa: BLE001
        manager = get_sandbox_manager()
        if manager is None or not _is_connection_error(exc):
            raise
        logger.warning(f"Sandbox: org={org_id} 连不上，丢弃沙箱重建后重试：{exc}")
        manager.invalidate(org_id)
        backend = _get_org_backend(org_id)
        if backend is None:
            raise
        return fn(backend)


class _OrgScopedSandboxBackendProxy:
    def __init__(self, manager: SandboxManager):
        self._manager = manager
        self._fallback = StateBackend()

    def _resolve(self):
        if not self._manager.available:
            logger.warning(
                f"SandboxProxy: sandbox unavailable, using fallback StateBackend "
                f"(thread={threading.get_ident()})."
            )
            return self._fallback
        org_id = _get_current_org()
        if org_id is None:
            logger.warning(
                f"SandboxProxy: thread-local org_id is None (thread={threading.get_ident()}). "
                f"Falling back to StateBackend — agent tool calls will NOT hit a sandbox."
            )
            return self._fallback
        backend = self._manager.get_backend(org_id)
        if backend is None:
            logger.warning(
                f"SandboxProxy: get_backend returned None for org={org_id} "
                f"(thread={threading.get_ident()})."
            )
            return self._fallback
        return backend

    def _call(self, name: str, *args, **kwargs):
        """调用 backend 的同名方法；连接类错误就丢掉这个沙箱、重建一次再试。

        背景：``SandboxManager`` 会按 org 缓存沙箱实例。如果容器在背后没了
        （被外部删除、被 OpenSandbox server 按 TTL 回收、Docker 重启），缓存里
        那个 backend 仍指向已经关闭的 execd 端口，于是**每一次工具调用**都会
        抛 ``SandboxConnectionException``。不重建的话，一次外部回收会让该 org
        之后所有请求持续失败，并且异常会直接打断整轮对话（回复为空）。
        """
        last_error: Optional[BaseException] = None
        for attempt in (1, 2):
            backend = self._resolve()
            try:
                return getattr(backend, name)(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2 or not _is_connection_error(exc):
                    raise
                last_error = exc
                org_id = _get_current_org()
                logger.warning(
                    f"SandboxProxy: {name} 连接失败，丢弃 org={org_id} 的沙箱并重建后重试：{exc}"
                )
                self._manager.invalidate(org_id)
        raise last_error  # pragma: no cover - 循环里必定 return 或 raise

    @property
    def id(self) -> str:
        backend = self._resolve()
        return getattr(backend, "id", "proxy")

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        if not hasattr(self._resolve(), "execute"):
            raise NotImplementedError(
                "Resolved backend does not support command execution."
            )
        return self._call("execute", command, timeout=timeout)

    def upload_files(self, files):
        return self._call("upload_files", files)

    def download_files(self, paths):
        return self._call("download_files", paths)

    def __getattr__(self, name):
        # 私有属性（如 __init__ 期间探测 _fallback）不能走这里，否则会无限递归
        if name.startswith("_"):
            raise AttributeError(name)
        underlying = self._resolve()
        attr = getattr(underlying, name)
        if not callable(attr):
            return attr

        def _wrapped(*args, **kwargs):
            return self._call(name, *args, **kwargs)

        return _wrapped


SandboxBackendProtocol.register(_OrgScopedSandboxBackendProxy)
