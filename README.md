# 阿林助手 · AlinAgent

一个自托管的对话助手：基于 DeepAgents + LangGraph，带**真实代码沙箱**。
能聊天、能读你上传的文件、能在隔离容器里跑代码，还能按技能生成极简 PPT。

前端是一套无构建步骤的静态页（原生 JS + 手写 CSS），由后端直接托管。

## 功能

- **流式对话** —— SSE 逐字返回；推理过程与工具调用单独渲染成可折叠的「思考轨迹」，并且随消息落库，刷新后仍能回看
- **会话管理** —— 多会话、历史回放、删除
- **文件问答** —— 支持 PDF / DOCX / XLSX / CSV / JSON / MD / TXT 等；小文件直接进上下文，大文件生成分析脚本交给沙箱执行
- **代码沙箱** —— 容器内执行命令、文件落到沙箱 `/workspace`、产物可下载
- **PPT 技能** —— 内置 `simple-pptx-generator`，把材料整理成论点，一键出极简 PPTX
- **长期记忆** —— 跨对话记住用户偏好

> 它不是 RAG 项目：没有向量检索、没有索引、没有知识库。上传的文件只是被解析成文本发给模型，或落到沙箱里被代码读取。

## 技术栈

| | |
|---|---|
| 后端 | FastAPI + LangGraph（Postgres 存 checkpoint 与长期记忆） |
| Agent | DeepAgents `create_deep_agent`，模型 `deepseek:deepseek-flash` |
| 沙箱 | OpenSandbox（Docker runtime），按 org 一容器、空闲 10 分钟回收 |
| 前端 | 原生 JS + 手写 CSS，无构建步骤 |

## 目录结构

```
main.py                  入口
app/
  routes.py              全部 HTTP 接口
  agent_setup.py         组装 DeepAgents（模型 / 记忆 / 技能 / 沙箱）
  sandbox.py             OpenSandbox 接入、故障自愈与容器回收
  config.py              环境变量
  models.py              响应模型
tools/file_handler.py    上传文件解析 + 沙箱分析脚本生成
skills/                  Agent Skills（simple-pptx-generator）
static/                  前端：index.html / css / js
tests/                   验证脚本（独立运行，不是 pytest）
```



