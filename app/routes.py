import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore

from .agent_setup import _ensure_user_habits
from .auth import (
    AuthError,
    SESSION_COOKIE_NAME,
    get_user_store,
    issue_session,
    session_cookie_kwargs,
)
from .budget import (
    BudgetExceeded,
    TokenBudget,
    client_ip,
    estimate_tokens,
    extract_tokens_from_chunk,
    get_budget,
)
from .config import (
    AGENT_RECURSION_LIMIT,
    ASSISTANT_NAME,
    ENABLE_CODE_EXECUTION,
    EXECUTE_RPM_PER_ORG,
    EXECUTE_TIMEOUT_MAX,
)
from .context_type import MemoryContext
from .identity import (
    Identity,
    get_current_identity,
    get_current_user,
    is_legacy_org,
    is_valid_id,
    new_id,
)
from .models import (
    MessageItem, ChatHistoryResponse, ThreadInfo,
    FileAnalysisResponse,
)
from .sandbox import (
    _set_current_org, _get_current_org, _sandbox_available,
    _get_org_backend, run_in_sandbox,
    sandbox_status as sandbox_status_payload,
)
from tools.file_handler import FileHandler, ParsedFile

logger = logging.getLogger(__name__)

router = APIRouter()

_store: Optional[PostgresStore] = None
_checkpointer: Optional[PostgresSaver] = None
_agent: Any = None
_uploaded_files: Dict[str, ParsedFile] = {}
_file_org_map: Dict[str, str] = {}
_file_content_cache: Dict[str, bytes] = {}


# ---------------------------------------------------------------------------
# 身份与授权
#
# org_id 决定用哪个沙箱容器、能下载哪个容器里的文件，是授权键。
# 它不再直接采信请求体里的值，而是必须与中间件校验过的签名身份一致
# （见 app/identity.py 的模块文档）。所有涉及「谁的沙箱」的入口都先过
# _resolve_org()，越权一律 403。
# ---------------------------------------------------------------------------

#: 沙箱内允许用户下载文件的根目录。用户上传的文件落在这里
#: （_sync_upload_file 拼 /workspace/{file_id}_{filename}），
#: skill 脚本与产物也都在 /workspace 下。
SANDBOX_DOWNLOAD_ROOT = "/workspace"

#: execute 的硬上限（秒）与每 org 每分钟次数，统一从 config 读（可用环境变量覆盖）。
#: 前端默认请求 60，允许调大但有天花板，否则一个用户能把容器占死。
EXECUTE_TIMEOUT_DEFAULT = 60


def _resolve_org(requested: Optional[str]) -> str:
    """把请求声明的 org_id 与已校验身份比对，返回可信的 org_id。

    请求没带 org_id 时回落到身份里的值（兼容老前端）；带了但不一致就是
    拿别人的授权键来用，直接 403。
    """
    identity = get_current_identity()
    if identity is None:
        # 正常不会发生：中间件给每个请求都挂了身份。走到这里说明身份中间件
        # 未生效（例如路由被单独挂载），此时拒绝服务比放行安全。
        logger.error("identity missing on request; refusing to resolve org")
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")

    if is_legacy_org(requested) or not requested:
        return identity.org_id

    if requested != identity.org_id:
        logger.warning(
            f"org 越权被拒：请求声明 org={requested}，实际身份 org={identity.org_id}"
        )
        raise HTTPException(status_code=403, detail="无权访问该组织的工作区。")

    return identity.org_id


def _resolve_user(requested: Optional[str]) -> str:
    """解析 user_id：与身份一致才采信，否则回落到身份里的值。

    与 org 不同，user_id 只是会话/记忆的命名空间，不决定容器。声明一个
    不匹配的值没有越权收益，但会导致后续按 user 过滤的查询取到别人的数据，
    所以这里不报错、直接忽略，保证行为可预期。
    """
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")
    if requested and requested == identity.user_id:
        return requested
    if requested and is_valid_id(requested) and requested != identity.user_id:
        logger.info("user_id 与身份不一致，已忽略请求值，改用身份内的 user_id。")
    return identity.user_id


def _guard_thread_owner(thread_id: str, requested_user: Optional[str] = None) -> None:
    """校验 thread_id 是否属于当前身份。

    thread_id 形如 ``org__user__后缀``（见 static/js/app.js 的约定与
    _parse_thread_owner）。**只比 org 是不够的**：同一个容器（org）里的不同
    用户，会话表在 checkpointer 里是分开的，但光看 org 就能互相读到。

    ``requested_user`` 为空时，一律以身份里的 user_id 为准，而不是「跳过
    比对」——否则调用方只要省略这个参数就能越过同 org 的会话隔离。
    """
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")

    expected_user = requested_user or identity.user_id

    thread_org, thread_user = _parse_thread_owner(thread_id)
    if thread_org is None:
        raise HTTPException(
            status_code=403,
            detail="无法识别的会话标识。请新建一个会话。",
        )
    if thread_org != identity.org_id:
        logger.warning(f"会话越权被拒：thread={thread_id}，身份 org={identity.org_id}")
        # 措辞要对**两种情况**都成立：真的拿了别人的 thread_id（越权），
        # 以及身份改造前的老会话（自己的，但已作废）。对后者说「无权」会让人
        # 以为账号出了问题，所以把「新建会话」这条出路写清楚。
        raise HTTPException(
            status_code=403,
            detail="该会话不属于当前身份（可能是早期版本的旧会话，已作废）。请新建一个会话继续。",
        )
    if thread_user is not None and thread_user != expected_user:
        logger.warning(f"会话越权被拒：thread={thread_id}，身份 user={identity.user_id}")
        raise HTTPException(
            status_code=403,
            detail="该会话不属于当前身份（可能是早期版本的旧会话，已作废）。请新建一个会话继续。",
        )


def _budget_preflight(request: Request, org_id: str, message: str, extra_chars: int = 0) -> int:
    """对话前的额度/频率检查；超限抛 429（带 Retry-After）。

    必须在**进入 agent 之前**调用：一旦开始 streaming，HTTP 状态码已经发出去了，
    只能用 SSE 事件表达错误——那是兜底，不是主闸门。
    """
    try:
        return get_budget().check_preflight(
            org_id=org_id,
            ip=client_ip(request),
            message=message,
            extra_chars=extra_chars,
        )
    except BudgetExceeded as e:
        logger.warning(f"对话被限流拒绝：org={org_id}, code={e.code}, {e.message}")
        raise HTTPException(
            status_code=429,
            detail=e.message,
            headers={"Retry-After": str(e.retry_after)},
        )


def _safe_download_path(path: str) -> Optional[str]:
    """校验沙箱下载路径，返回规范化后的路径；不合法返回 ``None``。

    必须落在 /workspace 下、是绝对路径、且不含 ``..``。这一层挡的是
    「把 path 当任意文件读取接口用」——之前 path 直接透传给沙箱的
    read_bytes，path=/etc/passwd 就能把容器里的任意文件读出来。
    """
    if not path or not isinstance(path, str):
        return None
    if "\x00" in path:
        return None

    candidate = path.strip()
    if not candidate.startswith("/"):
        return None

    # 先做纯字符串层面的规范化，避免 ".." 组合绕过前缀判断。
    parts: List[str] = []
    for segment in candidate.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            return None
        parts.append(segment)
    normalized = "/" + "/".join(parts)

    root = SANDBOX_DOWNLOAD_ROOT.rstrip("/")
    if normalized != root and not normalized.startswith(root + "/"):
        return None
    return normalized


_execute_hits: Dict[str, List[float]] = {}
_execute_hits_lock = threading.Lock()
EXECUTE_RATE_WINDOW = 60.0


def _check_execute_rate(org_id: str) -> None:
    """沙箱 execute 的按 org 滑动窗口限流；超限抛 429。

    与对话额度分开计：这里限的是「容器被占用的次数」，与 token 无关。
    真正的成本上限是单次时长（EXECUTE_TIMEOUT_MAX，来自 config）。
    """
    now = time.time()
    with _execute_hits_lock:
        hits = _execute_hits.setdefault(org_id, [])
        cutoff = now - EXECUTE_RATE_WINDOW
        hits[:] = [t for t in hits if t > cutoff]
        if EXECUTE_RPM_PER_ORG > 0 and len(hits) >= EXECUTE_RPM_PER_ORG:
            retry_after = max(1, int(hits[0] + EXECUTE_RATE_WINDOW - now))
            raise HTTPException(
                status_code=429,
                detail=f"执行过于频繁，请 {retry_after} 秒后重试。",
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


def set_globals(store: PostgresStore, checkpointer: PostgresSaver, agent: Any):
    global _store, _checkpointer, _agent
    _store = store
    _checkpointer = checkpointer
    _agent = agent

    # 把用量计数挂到 LangGraph 的 PostgresStore 上，让每日额度**重启不清零**。
    # 不挂也能跑，只是刷满额度后重启服务就能重置——那等于没限。
    try:
        _attach_budget_store(store)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"用量计数落库未启用（仅内存限流，重启会清零）：{e}")

    # 用户账号同样落在 PostgresStore：重启后账号必须还在，否则重启等于清空注册。
    try:
        loaded = get_user_store().attach(store)
        logger.info(f"用户系统已就绪（载入 {loaded} 个账号）。")
    except Exception as e:  # noqa: BLE001
        logger.error(f"用户系统初始化失败，账号将只在内存中有效：{e}", exc_info=True)


BUDGET_NAMESPACE = ("usage_budget",)
BUDGET_INDEX_KEY = "/orgs.json"


def _budget_store_key(org_id: str) -> str:
    """每个 org 一条记录。

    不共用一个 key：把不同 org 的用量写在同一条记录上会让「谁用了多少」变成
    竞争写入，任一 org 的更新都要覆写别人的数据。
    """
    return f"/usage_{org_id}.json"


def _attach_budget_store(store: Any) -> None:
    """把预算计数接到 PostgresStore，并在启动时把当天用量读回来。

    为什么值得做：不落库的话，刷满每日额度后**重启服务就能重置**，等于没限。

    记录分两类：``/orgs.json`` 是活跃 org 的索引（否则重启后不知道该读谁），
    每个 org 一条 ``/usage_<org>.json``。写失败不影响对话——``TokenBudget``
    内部吞掉异常，只是那一笔丢失。
    """
    known_orgs: List[str] = []

    def persist(key: str, day: str, tokens: int, requests: int) -> None:
        store.put(
            BUDGET_NAMESPACE,
            _budget_store_key(key),
            {"key": key, "day": day, "tokens": tokens, "requests": requests},
        )
        if key not in known_orgs:
            known_orgs.append(key)
            store.put(BUDGET_NAMESPACE, BUDGET_INDEX_KEY, {"orgs": known_orgs})

    budget = get_budget()
    budget.attach_store(persist)

    index = store.get(BUDGET_NAMESPACE, BUDGET_INDEX_KEY)
    index_value = getattr(index, "value", None) if index else None
    restored = 0
    if isinstance(index_value, dict):
        for org_id in index_value.get("orgs") or []:
            if not isinstance(org_id, str):
                continue
            known_orgs.append(org_id)
            record = store.get(BUDGET_NAMESPACE, _budget_store_key(org_id))
            value = getattr(record, "value", None) if record else None
            if isinstance(value, dict) and budget.restore(
                org_id, value.get("day"), value.get("tokens", 0), value.get("requests", 0)
            ):
                restored += 1
    logger.info(f"用量计数已落库：恢复 {restored} 个 org 的当天用量，额度重启不清零。")


def _extract_reasoning_and_content(message: Any) -> Tuple[str, Optional[str]]:
    if not isinstance(message, AIMessage):
        content = str(getattr(message, "content", "") or "")
        return content, None

    raw_content = str(message.content or "")
    reasoning: Optional[str] = None

    extra = message.additional_kwargs or {}
    rc = extra.get("reasoning_content") or extra.get("reasoning")
    if isinstance(rc, str) and rc.strip():
        reasoning = rc.strip()

    think_match = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        inline_reasoning = think_match.group(1).strip()
        if inline_reasoning:
            reasoning = (reasoning + "\n\n" + inline_reasoning) if reasoning else inline_reasoning
        raw_content = (raw_content[:think_match.start()] + raw_content[think_match.end():]).strip()

    return raw_content.strip(), reasoning


#: 发给模型用的内部上下文段落。它们会被拼在用户消息尾部、随消息一起进 checkpoint，
#: 所以读取历史时会出现——不剥掉的话，刷新后用户气泡里会多出整份上传文件内容和
#: 「【运行环境】…」那行，会话列表的标题也会带着它们。
_INTERNAL_CONTEXT_MARKERS = (
    "\n\n【已上传文件内容】",
    "\n\n【已上传文件信息】",
    "\n\n【运行环境】",
)


def _strip_internal_context(text: str) -> str:
    """剥掉用户消息尾部由 ``_prepare_message_context`` 拼进去的内部上下文。"""
    cut = len(text)
    for marker in _INTERNAL_CONTEXT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut].rstrip() if cut < len(text) else text


def _get_messages_from_state(state: Dict[str, Any]) -> List[MessageItem]:
    messages = state.get("messages", [])
    result = []
    for msg in messages:
        if isinstance(msg, BaseMessage):
            role = "assistant" if isinstance(msg, AIMessage) else "user" if isinstance(msg, HumanMessage) else "system"
            content, reasoning = _extract_reasoning_and_content(msg) if isinstance(msg, AIMessage) else (str(msg.content), None)
            if role == "user":
                content = _strip_internal_context(content)
            result.append(MessageItem(
                role=role,
                content=content,
                reasoning=reasoning,
            ))
        elif isinstance(msg, dict):
            result.append(MessageItem(
                role=msg.get("role", "unknown"),
                content=str(msg.get("content", "")),
                reasoning=msg.get("reasoning"),
            ))
    return result


def _build_env_footer(org_id: str) -> str:
    """消息尾部的运行环境说明。

    只在开启代码执行时才有意义：那时模型需要 ``org_id`` 去拼沙箱下载链接
    （``/api/sandbox/download?org_id=…&path=…``）、需要知道 ``/workspace`` 与
    ``/skills/`` 的存在。对话模式下这些路径都用不到，写进去只会让模型以为
    自己有一个沙箱，还可能把 org_id 当"内部信息"泄露给用户。
    """
    if not ENABLE_CODE_EXECUTION:
        return ""
    return (
        f"\n\n【运行环境】org_id={org_id}；沙箱工作目录 /workspace；"
        f"技能目录 /skills/。"
    )


def _sync_chat(message: str, thread_id: str, user_id: str, org_id: str) -> Dict[str, Any]:
    try:
        _set_current_org(org_id)
        _ensure_user_habits(_store, user_id)
        logger.info(f"Processing chat: thread_id={thread_id}, org_id={org_id}, message={message[:50]}...")
        config = RunnableConfig(configurable={"thread_id": thread_id})
        context = MemoryContext(user_id=user_id, org_id=org_id)
        full_message = message + _build_env_footer(org_id)
        res = _agent.invoke(
            {"messages": [{"role": "user", "content": full_message}]},
            context=context,
            config=config,
        )
        messages = _get_messages_from_state(res)
        last_message = messages[-1] if messages else None
        reply = last_message.content if last_message else ""
        reasoning = last_message.reasoning if last_message else None
        logger.info(f"Chat completed: thread_id={thread_id}, reply_length={len(reply)}")
        return {
            "status": "ok",
            "thread_id": thread_id,
            "reply": reply,
            "reasoning": reasoning,
            "messages": [m.model_dump() for m in messages],
        }
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return {
            "status": "error",
            "thread_id": thread_id,
            "reply": f"Error: {str(e)}",
            "reasoning": None,
            "messages": [],
        }
    finally:
        _set_current_org(None)


def _sync_get_history(thread_id: str) -> ChatHistoryResponse:
    try:
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = _agent.get_state(config)
        messages: List[MessageItem] = []
        if snapshot and snapshot.values:
            messages = _get_messages_from_state(snapshot.values)
        return ChatHistoryResponse(thread_id=thread_id, messages=messages)
    except Exception as e:
        logger.error(f"Get history error: {e}", exc_info=True)
        return ChatHistoryResponse(thread_id=thread_id, messages=[])


def _parse_thread_owner(thread_id: str) -> Tuple[Optional[str], Optional[str]]:
    parts = thread_id.split("__", 2)
    if len(parts) >= 3:
        return parts[0], parts[1]
    return None, None


def _sync_list_threads(
    org_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> List[ThreadInfo]:
    try:
        threads: List[ThreadInfo] = []
        # list() 的 limit 是「checkpoint 条数」而不是「会话数」，而一轮对话会写好几个
        # checkpoint。取太小会导致会话列表被截断（旧会话看不到），所以放宽到 300。
        tuples = list(_checkpointer.list(None, limit=300))
        for checkpoint_tuple in tuples:
            config = getattr(checkpoint_tuple, "config", {}) or {}
            configurable = config.get("configurable", {}) or {}
            thread_id = configurable.get("thread_id") or "unknown"
            if not thread_id or thread_id == "unknown":
                continue
            if any(t.thread_id == thread_id for t in threads):
                continue

            thread_org, thread_user = _parse_thread_owner(thread_id)
            if org_id is not None:
                if thread_org is None or thread_org != org_id:
                    continue
            if user_id is not None:
                if thread_user is None or thread_user != user_id:
                    continue

            info = ThreadInfo(thread_id=thread_id)
            try:
                snapshot = _agent.get_state({"configurable": {"thread_id": thread_id}})
                if snapshot and snapshot.values:
                    msgs = _get_messages_from_state(snapshot.values)
                    if msgs:
                        last_user_msg = next((m for m in reversed(msgs) if m.role == "user"), None)
                        info.last_message = last_user_msg.content if last_user_msg else (msgs[-1].content[:50] if msgs else None)
            except Exception:
                pass
            threads.append(info)
        return threads
    except Exception as e:
        logger.error(f"List threads error: {e}", exc_info=True)
        return []


def _sync_delete_thread(thread_id: str):
    _checkpointer.delete_thread({"configurable": {"thread_id": thread_id}})


def _sync_upload_file(file_id: str, filename: str, content: bytes, org_id: str) -> FileAnalysisResponse:
    try:
        _set_current_org(org_id)
        parsed = FileHandler.parse(filename, content)
        _uploaded_files[file_id] = parsed
        _file_org_map[file_id] = org_id
        _file_content_cache[file_id] = content

        sandbox_filename = f"{file_id}_{filename}"
        sandbox_path = f"/workspace/{sandbox_filename}"
        org_backend = _get_org_backend(org_id)
        if org_backend is not None:
            run_in_sandbox(org_id, lambda b: b.upload_files([(sandbox_path, content)]))
            logger.info(f"Uploaded file to sandbox (org={org_id}): {sandbox_path}")

        if org_backend is None:
            analysis_type = "llm_direct"
            if not parsed.is_small:
                truncated = parsed.preview
                parsed.is_small = True
                parsed.full_content = truncated
            code = None
        else:
            analysis_type = "llm_direct" if parsed.is_small else "code_execution"
            code = None
            if not parsed.is_small:
                code = FileHandler.generate_code_for_large_file(
                    sandbox_filename=sandbox_filename,
                    file_type=parsed.file_type,
                    columns=parsed.columns,
                )

        return FileAnalysisResponse(
            file_id=file_id,
            filename=parsed.filename,
            file_type=parsed.file_type,
            row_count=parsed.row_count,
            columns=parsed.columns,
            preview=parsed.preview,
            analysis_type=analysis_type,
            code=code,
        )
    except Exception as e:
        logger.error(f"File upload error: {e}", exc_info=True)
        raise
    finally:
        _set_current_org(None)


def _prepare_message_context(message: str, file_id: Optional[str], org_id: str) -> Tuple[str, str]:
    """Build the full message sent to the agent and resolve the org that owns
    the sandbox. Returns (full_message, execution_org)."""
    file_context = ""
    execution_org = org_id

    if file_id and file_id in _uploaded_files:
        parsed = _uploaded_files[file_id]

        file_org_id = _file_org_map.get(file_id, org_id)
        if file_org_id != org_id:
            # 以前这里只记一条 warning 然后「用文件所属 org 继续」——等于任何
            # 用户只要猜到一个别人的 file_id 就能借它读到该 org 沙箱里的文件，
            # 并且把自己的消息写进别人的容器上下文。file_id 是
            # f"file_{time:.0f}" 这种可枚举的值，不能当授权凭据用。
            logger.warning(
                f"拒绝跨 org 的文件访问：file_id={file_id} 属于 org={file_org_id}，"
                f"当前请求 org={org_id}"
            )
            raise HTTPException(status_code=403, detail="无权访问该文件。")

        _set_current_org(file_org_id)
        org_backend = _get_org_backend(file_org_id)
        execution_org = file_org_id

        # 只有真的开着代码执行、且拿到了容器，才把「路径 + 跑脚本」的流程告诉
        # 模型。否则 execute 工具已被摘掉（见 agent_setup._code_execution_middleware），
        # 这段提示会让模型答应一件做不到的事：让用户等一个不会出现的分析结果。
        can_execute = ENABLE_CODE_EXECUTION and org_backend is not None

        if parsed.is_small and parsed.full_content:
            file_context = f"\n\n【已上传文件内容】\n{parsed.full_content}\n\n请基于以上文件内容回答问题。"
        elif can_execute:
            sandbox_filename = f"{file_id}_{parsed.filename}"
            sandbox_path = f"/workspace/{sandbox_filename}"
            cached_content = _file_content_cache.get(file_id)
            if cached_content is not None:
                try:
                    run_in_sandbox(file_org_id, lambda b: b.upload_files([(sandbox_path, cached_content)]))
                    logger.info(
                        f"Re-uploaded file to sandbox (org={file_org_id}): "
                        f"{sandbox_path} (ensures file exists after possible recycle)"
                    )
                except Exception as e:
                    logger.warning(f"Failed to re-upload file to sandbox: {e}")

            file_context = (
                f"\n\n【已上传文件信息】\n"
                f"文件名: {parsed.filename}\n"
                f"文件类型: {parsed.file_type}\n"
                f"文件在沙箱中的路径: {sandbox_path}\n"
                f"总行数: {parsed.row_count}\n"
                f"列名: {parsed.columns}\n\n"
                f"你可以使用 execute 工具在沙箱中运行命令来分析此文件。\n"
                f"沙箱环境为 Python 3.12，**没有安装 pandas/numpy/pip**，请使用 Python 内置的 csv 模块。\n"
                f"示例步骤：\n"
                f"1. 用 write_file 工具将分析代码写入 /workspace/analyze.py\n"
                f"2. 用 execute 工具运行 `python3 /workspace/analyze.py`\n"
                f"代码示例:\n"
                f"```python\n"
                f"import csv, json\n"
                f"with open('{sandbox_path}', 'r', encoding='utf-8') as f:\n"
                f"    reader = csv.DictReader(f)\n"
                f"    rows = list(reader)\n"
                f"# 根据用户问题编写统计逻辑\n"
                f"print(json.dumps({{'count': len(rows)}}, ensure_ascii=False))\n"
                f"```\n"
                f"请根据用户的具体问题编写对应的代码并执行，基于执行结果回答问题。\n\n"
                f"【文件预览】\n{parsed.preview}"
            )
        else:
            file_context = (
                f"\n\n【已上传文件信息】\n"
                f"文件名: {parsed.filename}\n"
                f"行数: {parsed.row_count}\n"
                f"列名: {parsed.columns}\n"
                f"由于文件较大、当前又没有代码执行能力，只能提供预览数据。\n"
                f"请基于预览回答问题；如果问题必须看全量数据，如实说明这一点，"
                f"并建议用户缩小数据范围或把关键片段贴进对话。\n\n"
                f"【文件预览】\n{parsed.preview}"
            )
    else:
        _set_current_org(org_id)

    return message + file_context + _build_env_footer(org_id), execution_org


def _sync_chat_with_file(message: str, thread_id: str, user_id: str, org_id: str, file_id: Optional[str]) -> Dict[str, Any]:
    try:
        _ensure_user_habits(_store, user_id)
        full_message, execution_org = _prepare_message_context(message, file_id, org_id)
        _set_current_org(execution_org)

        effective_org = _get_current_org()
        logger.info(
            f"Processing chat with file: thread_id={thread_id}, org_id={org_id}, "
            f"file_id={file_id}, sandbox_org={effective_org}"
        )
        config = RunnableConfig(configurable={"thread_id": thread_id})
        context = MemoryContext(user_id=user_id, org_id=org_id)
        res = _agent.invoke(
            {"messages": [{"role": "user", "content": full_message}]},
            context=context,
            config=config,
        )
        messages = _get_messages_from_state(res)
        last_message = messages[-1] if messages else None
        reply = last_message.content if last_message else ""
        reasoning = last_message.reasoning if last_message else None
        logger.info(f"Chat completed: thread_id={thread_id}, reply_length={len(reply)}")
        return {
            "status": "ok",
            "thread_id": thread_id,
            "reply": reply,
            "reasoning": reasoning,
            "messages": [m.model_dump() for m in messages],
        }
    except Exception as e:
        logger.error(f"Chat with file error: {e}", exc_info=True)
        return {
            "status": "error",
            "thread_id": thread_id,
            "reply": f"Error: {str(e)}",
            "reasoning": None,
            "messages": [],
        }
    finally:
        _set_current_org(None)


def _sync_execute_code(code: str, timeout: int, org_id: str) -> Dict[str, Any]:
    try:
        _set_current_org(org_id)
        # run_in_sandbox：沙箱被外部回收/删除时会重建再重试一次，
        # 否则一次回收会让这个 org 之后的执行持续报连接错误。
        result = run_in_sandbox(org_id, lambda backend: backend.execute(code, timeout=timeout))
        return {
            "status": "ok" if result.exit_code == 0 else "error",
            "output": result.output,
            "exit_code": result.exit_code,
        }
    finally:
        _set_current_org(None)


def _sync_download_sandbox_file(org_id: str, path: str) -> Optional[bytes]:
    try:
        _set_current_org(org_id)
        try:
            resp = run_in_sandbox(org_id, lambda backend: backend.download_files([path])[0])
        except RuntimeError as e:
            logger.warning(f"Download: no backend for org={org_id}, path={path} ({e})")
            return None
        if resp.error:
            logger.warning(f"Download: sandbox returned error for path={path}: {resp.error}")
            return None
        if resp.content is None:
            logger.warning(f"Download: sandbox returned None content for path={path}")
            return None
        logger.info(f"Download: success, org={org_id}, path={path}, size={len(resp.content)} bytes")
        return resp.content
    except Exception as e:
        logger.error(f"Download: exception for org={org_id}, path={path}: {e}", exc_info=True)
        return None
    finally:
        _set_current_org(None)


# ---------------------------------------------------------------------------
# Streaming (SSE) helpers — 基于 DeepAgents/LangGraph 原生 graph.stream()
# ---------------------------------------------------------------------------

THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _sse(event_type: str, payload: Dict[str, Any]) -> str:
    return f"data: {json.dumps({'type': event_type, **payload}, ensure_ascii=False)}\n\n"


def _coerce_text(value: Any) -> str:
    """Extract plain text from a message content (str or list of blocks)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


class _ThinkStreamSplitter:
    """Incrementally split a streaming text stream into reasoning
    (<think>...</think>) and visible content."""

    def __init__(self, on_reasoning, on_content):
        self._on_reasoning = on_reasoning
        self._on_content = on_content
        self._buffer = ""

    def push(self, text: str) -> None:
        self._buffer += text
        self._drain()

    def flush(self) -> None:
        if self._buffer:
            self._on_content(self._buffer)
            self._buffer = ""

    def _drain(self) -> None:
        while True:
            buf = self._buffer
            if "<think" not in buf:
                if buf:
                    self._on_content(buf)
                    self._buffer = ""
                return
            m = THINK_RE.search(buf)
            if m:
                if m.start() > 0:
                    self._on_content(buf[:m.start()])
                reasoning = m.group(1).strip()
                if reasoning:
                    self._on_reasoning(reasoning)
                self._buffer = buf[m.end():]
                continue
            # Unclosed <think at the tail: flush completed content before it,
            # keep the tag onward until it closes.
            last_start = buf.rfind("<think")
            if last_start > 0:
                self._on_content(buf[:last_start])
                self._buffer = buf[last_start:]
                continue
            return


def _extract_tool_calls(update: Any) -> List[str]:
    """Recursively walk a node update (dict/list) for parsed tool calls."""
    names: List[str] = []

    def walk(value: Any):
        if isinstance(value, BaseMessage):
            for tc in getattr(value, "tool_calls", None) or []:
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    names.append(name)
        elif isinstance(value, list):
            for v in value:
                walk(v)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)

    walk(update)
    return names


def _stream_sse(
    message: str,
    thread_id: str,
    user_id: str,
    org_id: str,
    file_id: Optional[str] = None,
    client_ip_addr: Optional[str] = None,
) -> Iterator[str]:
    """同步生成器：直接调用 DeepAgents/LangGraph 原生的 graph.stream()，
    逐个产出 SSE 事件字符串。由 Starlette StreamingResponse 在线程池中迭代。

    token 记账在这里做：每次模型调用后累加 ``usage_metadata``，并在**下一轮
    模型调用之前**检查当日额度是否已耗尽——一轮问答里 agent 可能调用模型
    很多次（工具循环），只在一头一尾检查的话，中间能烧掉的量无法预估。

    ``client_ip_addr`` 只为游客的"每 IP 每日总量"记账用：按身份的额度清 cookie
    就能重置，按 IP 那层不能。
    """
    reasoning_parts: List[str] = []
    content_parts: List[str] = []
    seen_tools = set()
    pending_events: List[str] = []
    budget = get_budget()
    reported_tokens = 0
    tokens_at_last_call = 0
    truncated_for_budget = False

    def on_reasoning(text: str):
        reasoning_parts.append(text)
        pending_events.append(_sse("reasoning_token", {"content": text}))

    def on_content(text: str):
        content_parts.append(text)
        pending_events.append(_sse("token", {"content": text}))

    splitter = _ThinkStreamSplitter(on_reasoning, on_content)

    def drain() -> List[str]:
        if not pending_events:
            return []
        events = pending_events[:]
        pending_events.clear()
        return events

    try:
        _ensure_user_habits(_store, user_id)
        full_message, execution_org = _prepare_message_context(message, file_id, org_id)
        # 防御性设置：保证首个迭代线程的上下文正确（后续线程由端点上下文传播）
        _set_current_org(execution_org)

        logger.info(
            f"Streaming chat: thread_id={thread_id}, org_id={org_id}, "
            f"file_id={file_id}, sandbox_org={_get_current_org()}"
        )
        config = RunnableConfig(
            configurable={"thread_id": thread_id},
            # 不设的话走 DeepAgents 默认的 9999（deepagents/graph.py），
            # 工具一旦打转，一次提问就能烧掉几百次模型调用。
            recursion_limit=AGENT_RECURSION_LIMIT,
        )
        context = MemoryContext(user_id=user_id, org_id=org_id)

        for mode, data in _agent.stream(
            {"messages": [{"role": "user", "content": full_message}]},
            context=context,
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if mode == "updates":
                # 节点完成后的完整更新：从中识别工具调用
                for name in _extract_tool_calls(data):
                    if name and name not in seen_tools:
                        seen_tools.add(name)
                        yield _sse("tool_call", {"name": name})
                continue

            # mode == "messages": (chunk, metadata)
            chunk, _meta = data

            # 每轮都先记账：usage 是累计值，用 max 保证单调
            reported = extract_tokens_from_chunk(chunk)
            if reported > tokens_at_last_call:
                budget.add_tokens(org_id, reported - tokens_at_last_call, ip=client_ip_addr)
                tokens_at_last_call = reported

            if isinstance(chunk, ToolMessage):
                output = _coerce_text(chunk.content).strip()
                if output:
                    short = output if len(output) <= 300 else output[:300] + "..."
                    yield _sse("tool_result", {"output": short})
                continue
            if not isinstance(chunk, AIMessageChunk):
                continue

            # 流式思考内容（如 deepseek 的 reasoning_content）
            extra = chunk.additional_kwargs or {}
            rc = extra.get("reasoning_content") or extra.get("reasoning")
            rc_text = _coerce_text(rc)
            if rc_text:
                reasoning_parts.append(rc_text)
                yield _sse("reasoning_token", {"content": rc_text})

            # 正文内容（把 <think> 块拆到推理区，正文实时显示）
            text = _coerce_text(chunk.content)
            if text:
                splitter.push(text)
                for ev in drain():
                    yield ev

            # 额度用尽就停在这里，不再发起下一轮模型调用。
            # 已经吐出去的内容保留（用户看得到、也不重复计费），只是这一轮
            # 的答案会不完整——比放任它继续烧下去强。
            if budget.exceeded_midway(org_id, ip=client_ip_addr):
                truncated_for_budget = True
                break
    except Exception as e:
        logger.error(f"Streaming chat error: thread_id={thread_id}: {e}", exc_info=True)
        yield _sse("error", {"message": str(e)})
        return
    finally:
        _set_current_org(None)

    splitter.flush()
    for ev in drain():
        yield ev

    # 在真实 usage 之外补一笔估算：部分集成在流式下不返回 usage，
    # 只靠真实值会让「不报用量的供应商」完全绕过额度。
    if tokens_at_last_call == 0:
        estimated = estimate_tokens("".join(content_parts)) + estimate_tokens("".join(reasoning_parts))
        budget.add_tokens(org_id, estimated, ip=client_ip_addr)
        reported_tokens = estimated
    else:
        reported_tokens = tokens_at_last_call

    full_content = "".join(content_parts).strip()
    full_reasoning = "\n\n".join(p for p in reasoning_parts if p.strip()).strip() or None

    if truncated_for_budget:
        logger.warning(f"因额度耗尽中断生成：thread_id={thread_id}, org={org_id}")
        yield _sse("budget", {
            "message": "今日额度已用完，本轮回答被截断。请明天再试。",
            "tokens_used": budget.snapshot(org_id)["tokens_used"],
            "tokens_budget": budget.snapshot(org_id)["tokens_budget"],
        })

    logger.info(
        f"Streaming chat completed: thread_id={thread_id}, reply_length={len(full_content)}, "
        f"tokens≈{reported_tokens}"
    )
    yield _sse("done", {"reply": full_content, "reasoning": full_reasoning})


@router.post("/chat")
async def chat(request: Request):
    if not _agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
        message = data.get("message", "")
        thread_id = data.get("thread_id", "default-thread")
        org_id = _resolve_org(data.get("org_id"))
        user_id = _resolve_user(data.get("user_id"))
        _guard_thread_owner(thread_id, user_id)
        _budget_preflight(request, org_id, message)

        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_chat, message, thread_id, user_id, org_id),
            timeout=120.0
        )

        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("reply", "Unknown error"))

        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="请求超时，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _require_member(feature: str) -> None:
    """游客不能用的功能统一从这里拒。

    为什么文件上传对游客关闭：文件解析走的是一次性大上下文，游客额度（2 万 token）
    连一份中等 CSV 都读不完，放行只会得到"传了文件却分析不出来"的坏体验。
    注册后立刻可用。
    """
    identity = get_current_identity()
    if identity is None or not identity.is_member:
        raise HTTPException(
            status_code=403,
            detail=f"{feature}需要注册账号后使用。注册是免费的，还能保留历史会话。",
        )


@router.post("/files/upload", response_model=FileAnalysisResponse)
async def upload_file(file: UploadFile = File(...), org_id: str = Form("")):
    if not _agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    try:
        _require_member("上传文件")
        org_id = _resolve_org(org_id or None)
        content = await file.read()
        file_id = f"file_{asyncio.get_event_loop().time():.0f}"

        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_upload_file, file_id, file.filename, content, org_id),
            timeout=30.0
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="文件处理超时")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat-with-file")
async def chat_with_file(request: Request):
    if not _agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
        message = data.get("message", "")
        thread_id = data.get("thread_id", "default-thread")
        org_id = _resolve_org(data.get("org_id"))
        user_id = _resolve_user(data.get("user_id"))
        file_id = data.get("file_id")
        _guard_thread_owner(thread_id, user_id)
        _budget_preflight(request, org_id, message)

        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_chat_with_file, message, thread_id, user_id, org_id, file_id),
            timeout=180.0
        )

        if result.get("status") == "error":
            raise HTTPException(status_code=500, detail=result.get("reply", "Unknown error"))

        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="请求超时，请稍后重试")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


def _stream_headers() -> Dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


@router.post("/chat/stream")
async def chat_stream(request: Request):
    if not _agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    body = await request.body()
    data = json.loads(body) if body else {}
    message = data.get("message", "")
    thread_id = data.get("thread_id", "default-thread")
    org_id = _resolve_org(data.get("org_id"))
    user_id = _resolve_user(data.get("user_id"))
    if not message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    _guard_thread_owner(thread_id, user_id)
    _budget_preflight(request, org_id, message)

    # 在端点上下文设置 org：Starlette 在线程池迭代同步生成器时，
    # anyio 会把本任务的 contextvars 拷贝到每个工作线程，保证工具路由正确。
    _set_current_org(org_id)
    return StreamingResponse(
        _stream_sse(message, thread_id, user_id, org_id, client_ip_addr=client_ip(request)),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@router.post("/chat-with-file/stream")
async def chat_with_file_stream(request: Request):
    if not _agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    body = await request.body()
    data = json.loads(body) if body else {}
    message = data.get("message", "")
    thread_id = data.get("thread_id", "default-thread")
    org_id = _resolve_org(data.get("org_id"))
    user_id = _resolve_user(data.get("user_id"))
    file_id = data.get("file_id")
    if not message.strip():
        raise HTTPException(status_code=400, detail="消息内容不能为空")
    _guard_thread_owner(thread_id, user_id)
    _budget_preflight(request, org_id, message)

    # 与 _prepare_message_context 保持一致：文件所属组织的沙箱优先。
    # 文件登记在 _file_org_map 里，而上传时 org 已校验过，所以这里取到的
    # 只可能是本 org（跨 org 的 file_id 会在 _prepare_message_context 里被拒）。
    execution_org = _file_org_map.get(file_id, org_id) if file_id else org_id
    _set_current_org(execution_org)
    return StreamingResponse(
        _stream_sse(message, thread_id, user_id, org_id, file_id, client_ip(request)),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@router.post("/sandbox/execute")
async def execute_code(request: Request):
    if not _sandbox_available():
        raise HTTPException(status_code=503, detail="沙箱服务不可用。请先启动 OpenSandbox server，或将文件分析模式切换为直接分析。")
    try:
        body = await request.body()
        data = json.loads(body) if body else {}
        code = data.get("code", "")
        org_id = _resolve_org(data.get("org_id"))

        raw_timeout = data.get("timeout", EXECUTE_TIMEOUT_DEFAULT)
        try:
            timeout = int(raw_timeout)
        except (TypeError, ValueError):
            timeout = EXECUTE_TIMEOUT_DEFAULT
        # 夹到 [1, EXECUTE_TIMEOUT_MAX]：请求方原先可以把 timeout 设成任意大，
        # 一个用户就能把 org 的容器长期占住（容器是按 org 复用的）。
        timeout = max(1, min(timeout, EXECUTE_TIMEOUT_MAX))

        _check_execute_rate(org_id)

        result = await asyncio.wait_for(
            asyncio.to_thread(_sync_execute_code, code, timeout, org_id),
            timeout=timeout + 10,
        )
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="代码执行超时")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sandbox/download")
async def sandbox_download(path: str, org_id: Optional[str] = None):
    """把沙箱里的文件流给浏览器。

    两道校验，缺一不可：

    1. ``_resolve_org``：org_id 必须与签名身份一致，否则可以取别人的容器；
    2. ``_safe_download_path``：path 必须落在 /workspace 下且不含 ``..``。
       在此之前 path 是直接透传给沙箱的 read_bytes 的，``/etc/passwd``
       这类路径同样会被读出来并作为附件下发。

    该文件是否是「用户该看到的东西」由容器边界保证：一个 org 一个容器，
    容器内除了用户自己上传的文件，只有 Agent 为它生成的产物。
    """
    from fastapi.responses import Response

    resolved_org = _resolve_org(org_id)
    safe_path = _safe_download_path(path)
    if safe_path is None:
        logger.warning(f"拒绝越界的下载路径：org={resolved_org}, path={path!r}")
        raise HTTPException(status_code=400, detail="路径必须是 /workspace 下的文件。")

    content = await asyncio.to_thread(_sync_download_sandbox_file, resolved_org, safe_path)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found or sandbox unavailable")
    filename = safe_path.rsplit("/", 1)[-1]
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    )


@router.get("/identity")
async def get_identity():
    """把当前身份的 org_id / user_id / 档位告诉前端。

    前端需要 org_id 来拼 thread_id（``org__user__后缀``）与沙箱下载链接，
    而身份是服务端签发的，所以必须由服务端下发、前端原样回传。

    ``tier`` / ``registered`` / ``username`` 决定前端显示「游客体验额度」还是
    「今日额度」、以及要不要弹注册引导——**这些一律由服务端判定**。
    """
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")

    user = get_current_user()
    return {
        "org_id": identity.org_id,
        "user_id": identity.user_id,
        "tier": identity.tier,
        "registered": identity.is_member,
        "username": user.username if user is not None else None,
    }


# ---------------------------------------------------------------------------
# 账号：注册 / 登录 / 登出 / 当前状态
# ---------------------------------------------------------------------------

def _auth_payload(user, identity) -> Dict[str, Any]:
    return {
        "user": user.public(),
        "tier": identity.tier,
        "registered": identity.is_member,
    }


async def _read_json(request: Request) -> Dict[str, Any]:
    body = await request.body()
    try:
        data = json.loads(body) if body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON。")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象。")
    return data


def _identity_cookie_kwargs() -> Dict[str, Any]:
    from .identity import COOKIE_MAX_AGE, COOKIE_NAME, cookie_secure

    return {
        "key": COOKIE_NAME,
        "httponly": False,
        "samesite": "lax",
        "secure": cookie_secure(),
        "max_age": COOKIE_MAX_AGE,
        "path": "/",
    }


def _attach_session(response, user, identity):
    """写会话 cookie，并把身份 cookie 刷成新档位。

    第二步不能省：身份 cookie 里存着 ``tier``，不刷新的话下一个请求读到的还是
    guest——表现为"登录了但额度还是游客的"。
    """
    from .identity import issue_token

    response.set_cookie(value=issue_session(user), **session_cookie_kwargs())
    response.set_cookie(value=issue_token(identity), **_identity_cookie_kwargs())
    return response


def _upgrade_identity(user):
    """把当前匿名身份升级成会员档，**沿用原 org**。

    不换 org 的原因：正在看的会话其 thread_id 里嵌着 org，换掉等于把用户眼前的
    历史全部作废。跨设备时 org 按 user_id 派生（``identity.member_identity``），
    所以不同浏览器也能落到同一个命名空间。
    """
    from .identity import member_identity, set_current_identity

    current = get_current_identity()
    org_id = current.org_id if current is not None else None
    identity = member_identity(user.user_id, org_id=org_id)
    set_current_identity(identity)
    return identity


@router.post("/auth/register")
async def auth_register(request: Request):
    """注册并直接登录（省掉一次重复输入密码）。"""
    data = await _read_json(request)
    store = get_user_store()
    try:
        # pbkdf2 60 万次迭代约几十毫秒，放线程里跑，别卡住事件循环。
        user = await asyncio.to_thread(
            store.create, data.get("username", ""), data.get("password", "")
        )
    except AuthError as e:
        status = 409 if e.code == "username_taken" else 400
        logger.info(f"注册被拒：{e.code} username={data.get('username')!r}")
        raise HTTPException(status_code=status, detail=e.message)

    identity = _upgrade_identity(user)
    logger.info(f"注册并登录：{user.username}（{user.user_id}）")
    response = JSONResponse(_auth_payload(user, identity), status_code=201)
    return _attach_session(response, user, identity)


@router.post("/auth/login")
async def auth_login(request: Request):
    data = await _read_json(request)
    store = get_user_store()
    try:
        user = await asyncio.to_thread(
            store.authenticate, data.get("username", ""), data.get("password", "")
        )
    except AuthError as e:
        logger.info(f"登录失败：{e.code} username={data.get('username')!r}")
        raise HTTPException(status_code=401, detail=e.message)

    identity = _upgrade_identity(user)
    logger.info(f"登录成功：{user.username}（{user.user_id}）")
    response = JSONResponse(_auth_payload(user, identity))
    return _attach_session(response, user, identity)


@router.post("/auth/logout")
async def auth_logout():
    """登出：清会话 cookie，并把身份 cookie 降回游客档。

    **必须同时降级身份 cookie**：它里面存着 ``tier``，只删会话 cookie 的话，
    下一个请求读到的还是 ``tier=member``——用户清掉会话 cookie 就能无限期
    白拿会员额度。这是实测踩到的洞，别省这一步。

    延续原 org：登出后浏览器看到的还是同一批会话（属于当前匿名身份的那些），
    只是账号绑定的历史读不到了——这是"历史绑账号"的必然结果。
    """
    from .identity import (
        COOKIE_MAX_AGE,
        COOKIE_NAME,
        cookie_secure,
        get_current_identity,
        issue_token,
    )

    response = JSONResponse({"ok": True, "tier": "guest", "registered": False})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")

    current = get_current_identity()
    guest = Identity(
        org_id=current.org_id if current is not None else new_id("o"),
        user_id=current.user_id if current is not None else new_id("u"),
        tier="guest",
    )
    response.set_cookie(
        value=issue_token(guest),
        key=COOKIE_NAME,
        httponly=False,
        samesite="lax",
        secure=cookie_secure(),
        max_age=COOKIE_MAX_AGE,
        path="/",
    )
    return response


@router.get("/auth/me")
async def auth_me():
    """当前登录状态（前端启动时决定显示"登录"还是用户名）。"""
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")
    user = get_current_user()
    if user is None:
        return {"registered": False, "tier": identity.tier, "user": None}
    return _auth_payload(user, identity)


@router.get("/config")
async def get_runtime_config():
    """前端需要的运行时开关。"""
    return {
        "code_execution": ENABLE_CODE_EXECUTION,
        "assistant_name": ASSISTANT_NAME,
        "accounts_enabled": True,
    }


@router.get("/usage")
async def get_usage():
    """当前身份的额度用量快照，供前端在侧栏显示「今日已用 / 上限」。"""
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")
    return get_budget().snapshot(identity.org_id)


@router.get("/sandbox/status")
async def sandbox_status():
    return sandbox_status_payload()


@router.get("/history/{thread_id}", response_model=ChatHistoryResponse)
async def get_history(thread_id: str, user_id: Optional[str] = None):
    # 历史里含完整对话与推理轨迹，之前只凭 thread_id 就取——而 thread_id 是
    # org__user__后缀，知道就能读别人的会话。这里按身份校验归属。
    # user_id 必须先过 _resolve_user：它会把不一致的声明值换成身份里的值，
    # 否则请求方传一个跟 thread 里 user 相同的值就能骗过归属校验。
    _guard_thread_owner(thread_id, _resolve_user(user_id))
    return await asyncio.to_thread(_sync_get_history, thread_id)


@router.get("/threads", response_model=List[ThreadInfo])
async def list_threads(org_id: Optional[str] = None, user_id: Optional[str] = None):
    # 列表按身份过滤：请求里声明的 org/user 一律覆写为身份里的值，
    # 否则带上别人的 org 就能列出别人的会话标题与最后一条消息。
    identity = get_current_identity()
    if identity is None:
        raise HTTPException(status_code=500, detail="身份未初始化，请检查服务端中间件配置。")
    return await asyncio.to_thread(_sync_list_threads, identity.org_id, identity.user_id)


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str, user_id: Optional[str] = None):
    # 删除是不可逆的：不校验归属等于任何人都能删掉别人的全部对话。
    _guard_thread_owner(thread_id, _resolve_user(user_id))
    await asyncio.to_thread(_sync_delete_thread, thread_id)
    return {"status": "ok"}
