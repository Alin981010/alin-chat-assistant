# 阿林对话助手 · Alin Chat Assistant

一个自托管的**通用对话助手**：基于 DeepAgents + LangGraph。
问答、写作、翻译、编程、分析、学习辅导都在一个对话框里完成。

默认**不执行代码**——写代码、调试、解释代码一律通过对话：用户把代码贴进来，
助手读它、讲它、改它。这既是产品定位（它是个"讲清楚"的助手），也是安全边界
（没有容器、没有"跑任意脚本"的滥用面）。需要真跑代码时用
`ENABLE_CODE_EXECUTION=true` 打开沙箱模式，见下文。

前端是一套无构建步骤的静态页（原生 JS + 手写 CSS），由后端直接托管。

## 能做什么

- **信息查询与解答** —— 历史、科学、文化、生活常识；解释概念术语、梳理复杂信息
- **建议与参考** —— 旅行规划、学习方法、健身计划等（给方案和取舍，不装作有实时数据）
- **写作与创作** —— 文章/报告/邮件/演讲稿/文案；润色与改语气；故事、诗歌、剧本；多语言翻译
- **编程与技术** —— 写代码、调试、解释代码逻辑；讲语言与算法；技术方案建议（走对话，不执行）
- **分析与处理** —— 总结长文本、提取要点、对比信息、整理表格、逻辑推理
- **学习与辅导** —— 讲知识点、出练习题、辅导作业、制定学习计划
- **日常陪伴** —— 闲聊、倾听、情感支持、头脑风暴
- **通用工具** —— 当前时间/日期/星期（`get_current_time`）、实时天气与预报
  （`get_weather`，Open-Meteo，无需 API key）、单位换算（`convert_units`）

## 功能

- **流式对话** —— SSE 逐字返回；推理过程与工具调用单独渲染成可折叠的「思考轨迹」，并且随消息落库，刷新后仍能回看
- **会话管理** —— 多会话、历史回放、删除
- **文件问答** —— 支持 PDF / DOCX / XLSX / CSV / JSON / MD / TXT 等，解析成文本进上下文
- **通用工具** —— 时间、天气、单位换算（见上）
- **长期记忆** —— 跨对话记住用户偏好
- **额度与限流** —— 按身份/按 IP 的窗口限流 + 每日 token 上限，见下文

> 它不是 RAG 项目：没有向量检索、没有索引、没有知识库。也不是代码执行平台——
> 除非显式打开 `ENABLE_CODE_EXECUTION`。

## 技术栈

| | |
|---|---|
| 后端 | FastAPI + LangGraph（Postgres 存 checkpoint 与长期记忆） |
| Agent | DeepAgents `create_deep_agent`，模型 `deepseek:deepseek-flash` |
| 沙箱 | OpenSandbox（Docker runtime），**仅在 `ENABLE_CODE_EXECUTION=true` 时启用** |
| 前端 | 原生 JS + 手写 CSS，无构建步骤 |

## 目录结构

```
main.py                  入口
app/
  routes.py              全部 HTTP 接口
  agent_setup.py         组装 DeepAgents（模型 / 提示词 / 记忆 / 技能 / 工具）
  identity.py            匿名身份的签名与校验（org_id 是沙箱的隔离键）
  budget.py              额度与限流（窗口 / 每日 token 上限 / 单次上限）
  limits.py              限额默认值（单一事实来源）
  sandbox.py             OpenSandbox 接入、故障自愈与容器回收（执行模式用）
  config.py              环境变量
  models.py              响应模型
tools/general_tools.py   通用工具：时间 / 天气 / 单位换算
tools/file_handler.py    上传文件解析 + 沙箱分析脚本生成
skills/                  Agent Skills（simple-pptx-generator，执行模式下可用）
static/                  前端：index.html / css / js
tests/                   验证脚本（独立运行，不是 pytest）
```

## 代码执行模式（可选）

默认关闭。关闭时 `execute` 工具会被 `_ToolExclusionMiddleware` 从模型眼前摘掉，
并在工具调用边界拦截——所以"我帮你跑一下"这种回复在物理上不可能发生，
而不是仅靠提示词约束。同时启动时不再探测/预热沙箱，省掉十几个 GB 的镜像拉取。

打开后恢复：文件落进容器 `/workspace`、`execute` 真跑命令、大文件分析走脚本、
PPT 技能生成 .pptx 并可下载。此时请务必同时配置 `APP_SECRET_KEY` 与安全组来源。


## 部署给他人使用时的隔离

- **Agent 只能碰沙箱容器里的文件。** 文件工具全部走沙箱后端（`CompositeBackend`
  的 default 是 `OpensandboxBackend`），`execute` 在容器内执行。用户的本地磁盘
  从设计上就够不着——下载是服务端把文件流给浏览器、由用户自己另存。
- **一个 org 一个容器，org 由服务端签发。** 前端不再自己生成身份，而是
  `GET /api/identity` 取回签名身份并原样回传；服务端把请求里的 `org_id`
  与签名比对，不一致直接 403（`app/identity.py`、`app/routes.py:_resolve_org`）。
- **下载限定在 `/workspace`。** `path` 必须是该目录下的绝对路径且不含 `..`，
  堵住「把下载接口当任意文件读取用」。
- **会话按 org + user 校验归属。** 历史与删除都要求调用方身份与
  `thread_id` 里的 `org__user` 一致。
- 对外部署前请配置 `APP_SECRET_KEY`、`APP_ALLOWED_ORIGINS`，见 `.env.example`。

## 控制 token 消耗

排在最前面的成本是**反复对话**，四层防线都在 `app/budget.py`，默认值见
`app/limits.py`，全部可用环境变量覆盖（`.env.example` 有逐项说明）：

| 层 | 默认 | 挡什么 |
|---|---|---|
| 每分钟次数（按身份 / 按 IP） | 8 / 30 | 刷屏式连问；清 cookie 换身份也绕不过 IP 那层 |
| 每日 token 额度（按身份） | 200 万 | **唯一能封顶账单的一层**，到量后 429，流式生成中途也会被截断 |
| 单次输入上限 | 6 万 token | 粘贴整本书 |
| 单次输出上限 `max_tokens` | 4096 | 一次回复生成到上下文上限 |
| agent 图步数 `recursion_limit` | 40 | 工具打转（DeepAgents 默认 9999，等于不设限） |

用量计数会写进 Postgres（`usage_budget` 命名空间），**重启不清零**——否则
刷满额度后重启服务就能重置，等于没限。前端侧栏有「今日额度」进度条，
数据来自 `GET /api/usage`。

> 注意：按身份的额度以 cookie 为载体，访客清 cookie 即可重置。所以
> `CHAT_RPM_PER_IP` 必须开着；真要严格封顶，应在反向代理层再按 IP/账号限流，
> 并把安全组来源收紧到你自己的网段。





