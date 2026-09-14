"""限额的默认值：单一事实来源。

单独放一个不依赖任何东西的小模块，是为了避免 ``config`` 与 ``budget``
互相 import：``config`` 读环境变量并以这里的值兜底，``budget`` 只从
``config`` 取最终值。

调这些值时优先用环境变量（见 .env.example），改这里的默认值要同时想清楚
「不配任何环境变量的部署会怎样」。
"""

#: 每个身份每分钟最多发起多少次对话请求
CHAT_RPM_PER_USER = 8

#: 每个 IP 每分钟的对话请求上限（清 cookie 能换身份，换不掉 IP）
CHAT_RPM_PER_IP = 30

#: 每个身份每天可消耗的 token 总量；0 = 不限
CHAT_DAILY_TOKEN_BUDGET = 2_000_000

#: 单次请求的输入估算上限（token）
CHAT_MAX_INPUT_TOKENS = 60_000

#: 模型单次回复的输出上限，直接作为 max_tokens 传给模型
AGENT_MAX_OUTPUT_TOKENS = 4_096

#: agent 单次提问允许的图步数上限（一步 ≈ 一次模型调用）
AGENT_RECURSION_LIMIT = 40

#: 沙箱 execute：每个 org 每分钟的执行次数
EXECUTE_RPM_PER_ORG = 60

#: 沙箱 execute：单次执行时长硬上限（秒）
EXECUTE_TIMEOUT_MAX = 300
