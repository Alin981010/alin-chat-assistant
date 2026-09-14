"""PostgresStore 的命名空间路由。

只有 ``user_namespace`` 在用（``/memories/`` 按用户隔离）；AGENTS.md 走的是
``agent_setup.AGENTS_NAMESPACE`` 这个固定全局命名空间，不经过这里。
"""


def user_namespace(rt):
    if rt.server_info and rt.server_info.user:
        return (rt.server_info.user.identity,)
    user_id = getattr(rt.context, "user_id", "local-user")
    return (user_id,)
