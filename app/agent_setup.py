import logging
from typing import Any, List, Optional

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StoreBackend, StateBackend, FilesystemBackend
from langgraph.store.postgres import PostgresStore
from langgraph.checkpoint.postgres import PostgresSaver

from .config import MODEL_NAME, LOCAL_SKILLS_DIR, PPT_SKILL_DIR
from .context_type import MemoryContext
from .namespace_router import user_namespace
from .sandbox import _OrgScopedSandboxBackendProxy, get_sandbox_manager

logger = logging.getLogger(__name__)

# /memories/AGENTS.md 的初始内容（即系统提示词），首次使用时写入 PostgresStore
AGENTS_MEMORY_BASE = """你是一个智能助手，可以通过持续对话学习和记录用户的习惯与偏好。

## 关于用户习惯记录

在对话过程中，你应当主动观察用户的行为、言论和选择，识别以下类型的信息：

### 应该记录的内容：
- **编程语言与技术偏好**：用户常用的编程语言、框架、工具、代码风格等
- **工作习惯**：用户的工作流程、时间安排、常用工具、偏好的方法论等
- **沟通与输出偏好**：用户偏好的语言（中文/英文）、输出详细程度、格式要求、回复风格等
- **其他特征**：用户的特殊需求、注意事项、习惯、个人特点等

### 记录规则：
1. 使用 `edit_file` 工具将识别到的习惯追加到 `/memories/habits.md` 的对应分类下
2. 记录格式为：`- [具体习惯描述] (来源：YYYY-MM-DD 对话)`
3. 每次只记录新发现的习惯，不要重复已有内容
4. 如果用户明确要求"记住"某事，务必记录
5. 每个用户的习惯记录是相互隔离的，你只需关注当前对话用户

### 不应记录的内容：
- 临时性信息（如"我今天迟到了"、"帮我订个会议室"）
- 一次性任务请求
- 简单问答的答案
- 敏感信息（密码、密钥、身份证号等）
- 过期或无意义的闲聊
"""

#: PPT 技能段只在本地确实存在该技能时才拼进系统提示词。
#: 技能目录不存在时，提示词里就不该出现一个用不了的技能，否则模型会去
#: read_file 一个不存在的 SKILL.md，再给用户报错。
#: 里面的 @@SKILL@@ 是技能目录名占位符，由 build_agents_memory() 用
#: PPT_SKILL_DIR.name 填充——改技能名只需要动 app/config.py 一处。
AGENTS_MEMORY_PPT = """
## PPT 制作能力（@@SKILL@@ 技能）

**重要约束：绝不主动向用户提起、推销或询问是否需要制作 PPT。** 用户可能只是在闲聊、提问或做文件分析，不要主动提及 PPT，也不要主动给出 PPT 相关建议。只有当用户明确表达"做PPT/制作演示文稿/生成PPT/create PPT/做幻灯片/极简PPT/轻量化汇报PPT/导出pptx简报"等意图时，才按以下约定直接执行，无需额外确认：

1. 先用 `read_file` 读取 `/skills/@@SKILL@@/SKILL.md`（`limit=1000`），确认版式约束与参数写法。
2. 把材料整理成**论点列表**，再用 `execute` 调生成脚本（写成一条命令，不要用反斜杠换行）：

   `python3 /skills/@@SKILL@@/generate_pptx.py --topic "主题" --points "论点1,论点2：子条1；子条2" --page-num 8 --output /workspace/light_presentation.pptx --json`

   - `--points` 用中英文逗号分隔；写成 `标题：要点1；要点2` 可让该页带条目；每页最多 5 条，超出会被截断，所以要点化表达，别把整段原文搬上页面。
   - `--page-num` 是目标**上限**，实际页数由论点数量决定，不会空凑页数。
   - 需要图表时加 `--data "12,25,31,44"`，会自动多生成一页原生柱状图。
   - **务必加 `--json`**：stdout 只输出一行 JSON，从里面读 `output` 与 `pages` 最稳（日志都在 stderr）。
3. **关键约定**：
   - 技能目录是只读的：`/skills/@@SKILL@@/`。要改版式，就把脚本复制到 `/workspace/` 再改，不要动原件。
   - 运行环境是当前组织的沙箱（Linux，`python3`，没有 pandas/numpy）。
   - 输出必须落在沙箱 `/workspace/` 下的绝对路径。
   - 依赖 `python-pptx` 由沙箱预置；万一缺失，脚本会自己装 pip 与依赖（含 PEP 668 需要的 `--break-system-packages`），**不要把时间花在手动 pip 上**。首次执行时给 `execute` 的 timeout 至少 300 秒。
4. 生成后，把沙箱里的绝对路径拼成可点击的下载链接返回给用户：
   `/api/sandbox/download?org_id=<消息末尾【运行环境】中的org_id>&path=<沙箱中pptx绝对路径>`
5. 若沙箱刚创建、后台预置尚未跑完导致首次执行失败，等几秒重试一次即可。
"""


def build_agents_memory() -> str:
    """按当前仓库实际具备的能力拼出系统提示词。

    目前唯一的可选能力是 PPT 技能：``skills/<PPT_SKILL_DIR.name>/`` 存在才启用，
    并把技能目录名填进提示词里的 @@SKILL@@ 占位符。
    """
    if PPT_SKILL_DIR.exists():
        return AGENTS_MEMORY_BASE + AGENTS_MEMORY_PPT.replace("@@SKILL@@", PPT_SKILL_DIR.name)
    return AGENTS_MEMORY_BASE


# AGENTS.md 是全局配置，与用户无关，统一存放在此命名空间（数据库仅一份）
AGENTS_NAMESPACE = ("global",)
AGENTS_KEY = "/AGENTS.md"


class _GlobalAgentsStore:
    """把 composite 剥离出的 "/" 归一化回 "/AGENTS.md" 的 Store 包装。

    CompositeBackend 对路由前缀与文件路径完全相等的路径（"/memories/AGENTS.md"）
    会把传入后端的 key 剥成 "/"，这里统一还原，保证全局只存一份 key="/AGENTS.md"。
    """

    def __init__(self, store: Any):
        self._store = store

    @staticmethod
    def _key(key: str) -> str:
        return AGENTS_KEY if key == "/" else key

    def get(self, namespace, key, *args, **kwargs):
        return self._store.get(namespace, self._key(key), *args, **kwargs)

    def aget(self, namespace, key, *args, **kwargs):
        return self._store.aget(namespace, self._key(key), *args, **kwargs)

    def put(self, namespace, key, value, *args, **kwargs):
        return self._store.put(namespace, self._key(key), value, *args, **kwargs)

    def aput(self, namespace, key, value, *args, **kwargs):
        return self._store.aput(namespace, self._key(key), value, *args, **kwargs)

    def delete(self, namespace, key, *args, **kwargs):
        return self._store.delete(namespace, self._key(key), *args, **kwargs)

    def adelete(self, namespace, key, *args, **kwargs):
        return self._store.adelete(namespace, self._key(key), *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _ensure_agents_memory(store: PostgresStore):
    """确保全局 AGENTS.md（系统提示词配置）已存在且为当前版本，内容不一致时更新。

    内容由 ``build_agents_memory()`` 按仓库实际能力生成：技能目录增删后重启即可生效。
    """
    memory = build_agents_memory()
    current = {"content": memory, "encoding": "utf-8"}
    existing = store.get(AGENTS_NAMESPACE, AGENTS_KEY)
    if existing:
        value = getattr(existing, "value", None) or {}
        stored_content = value.get("content") if isinstance(value, dict) else None
        if stored_content == memory:
            return
        logger.info("Global /AGENTS.md differs from current version, updating.")
        store.put(AGENTS_NAMESPACE, AGENTS_KEY, current)
        return

    store.put(AGENTS_NAMESPACE, AGENTS_KEY, current)
    logger.info("Created global /AGENTS.md")


def _remove_per_user_agents_memory(store: PostgresStore):
    """清理历史遗留的按用户存放的 AGENTS.md 副本（只保留全局一份）。"""
    try:
        removed = 0
        for ns in store.list_namespaces(limit=1000):
            if len(ns) == 1 and ns != AGENTS_NAMESPACE:
                try:
                    store.delete(ns, AGENTS_KEY)
                    removed += 1
                except Exception:
                    pass
        if removed:
            logger.info(f"Removed {removed} per-user AGENTS.md copy/copies.")
    except Exception as e:
        logger.warning(f"Failed to clean per-user AGENTS.md copies: {e}", exc_info=True)


def _ensure_user_habits(store: PostgresStore, user_id: str):
    HABITS_KEY = "/habits.md"
    NAMESPACE = (user_id,)

    existing = store.get(NAMESPACE, HABITS_KEY)
    if existing:
        return

    initial_content = """# 用户习惯记录

_此文件由 Agent 自动维护，持续记录用户的偏好、习惯和特点。_

## 编程语言与技术偏好
<!-- 用户使用的编程语言、框架、工具等偏好 -->

## 工作习惯
<!-- 用户的工作时间、工作流程、常用工具等 -->

## 沟通与输出偏好
<!-- 用户的语言风格、输出格式、详细程度等偏好 -->

## 其他特征
<!-- 用户的特殊习惯、需求、注意事项等 -->

---

_记录格式：_  
_## [类别名称]_  
_- [具体习惯描述] (来源：YYYY-MM-DD 对话)_
"""

    store.put(NAMESPACE, HABITS_KEY, {
        "content": initial_content,
        "encoding": "utf-8",
    })
    logger.info(f"Created /habits.md for user: {user_id}")


def _skill_sources() -> Optional[List[str]]:
    """``skills/`` 下确实存在条目时才声明技能源。

    deepagents 的 ``SkillsMiddleware`` 只要拿到 sources，就会往系统提示词里塞
    "You have access to a skills library …"，**即使目录是空的**（空目录只在列表处
    显示 "No skills available yet"）。结果是模型每轮都去 ``ls /skills`` 找一个并不
    存在的技能库，白烧几轮 token。目录为空时直接不传 ``skills``，中间件就不会加载。
    """
    try:
        has_entries = LOCAL_SKILLS_DIR.is_dir() and any(LOCAL_SKILLS_DIR.iterdir())
    except OSError:
        has_entries = False
    if has_entries:
        return ["/skills/"]
    logger.warning("skills/ 为空，本次不向模型声明技能库（避免模型反复探查不存在的技能）。")
    return None


def create_agent(checkpointer: PostgresSaver, store: PostgresStore) -> Any:
    manager = get_sandbox_manager()
    if manager and manager.available:
        default_backend = _OrgScopedSandboxBackendProxy(manager)
        logger.info("Sandbox manager initialized (per-org container, 10-min idle recycle).")
    else:
        default_backend = StateBackend()
        logger.warning("Sandbox unavailable, running in degraded mode (no code execution).")

    agent = create_deep_agent(
        model=MODEL_NAME,
        context_schema=MemoryContext,
        memory=["/memories/AGENTS.md", "/memories/habits.md"],
        skills=_skill_sources(),
        checkpointer=checkpointer,
        backend=CompositeBackend(
            default=default_backend,
            routes={
                # AGENTS.md 是全局配置：单独路由到全局命名空间（仅存一份），
                # habits.md 仍按用户命名空间隔离。
                "/memories/AGENTS.md": StoreBackend(
                    namespace=lambda _rt: AGENTS_NAMESPACE,
                    store=_GlobalAgentsStore(store),
                ),
                "/memories/": StoreBackend(
                    namespace=user_namespace,
                    store=store,
                ),
                "/skills/": FilesystemBackend(
                    root_dir=str(LOCAL_SKILLS_DIR),
                    virtual_mode=True,
                ),
            },
        ),
    )
    return agent
