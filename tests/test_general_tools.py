"""通用能力验证：工具、提示词定位、以及「代码只走对话不执行」。

    python tests/test_general_tools.py

三块：

1. 工具本身——时间、单位换算（离线可测）；天气只验证错误路径与代码表，
   不把联网结果写进断言（离线环境会失败，那不是代码的问题）；
2. 提示词——能力清单、代码处理约定、名字占位符都被正确拼装；
3. 执行开关——关闭时必须真的摘掉 execute 工具（提示词约束之外的第二道），
   打开时提示词回到「能跑脚本」的版本。

不依赖 Postgres 与沙箱。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("APP_SECRET_KEY", "test-secret-do-not-use-in-production")

from langchain_core.tools import BaseTool  # noqa: E402

from tools.general_tools import (  # noqa: E402
    GENERAL_TOOLS,
    _CITY_ALIASES,
    _describe_wmo,
    convert_units,
    get_current_time,
)

RESULTS = []


def check(name: str, ok: bool, detail: object = "") -> None:
    RESULTS.append(ok)
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail != "" else ""))


# ---------------------------------------------------------------------------
# 1. 工具
# ---------------------------------------------------------------------------

def test_tool_registration() -> None:
    names = [t.name for t in GENERAL_TOOLS]
    check("三个通用工具都注册了",
          names == ["get_current_time", "get_weather", "convert_units"], names)
    check("都是 LangChain BaseTool（否则 agent 挂不上）",
          all(isinstance(t, BaseTool) for t in GENERAL_TOOLS))
    check("每个工具都有 docstring（模型靠它决定何时调用）",
          all((t.description or "").strip() for t in GENERAL_TOOLS),
          [bool((t.description or '').strip()) for t in GENERAL_TOOLS])
    check("工具的入参 schema 完整",
          all(t.args for t in GENERAL_TOOLS), [list(t.args) for t in GENERAL_TOOLS])


def test_time_tool() -> None:
    out = get_current_time.invoke({"timezone_name": "Asia/Shanghai"})
    check("时间工具返回日期/时间/星期/时区",
          all(k in out for k in ("时区：", "日期：", "时间：", "星期：")), out.replace("\n", " | "))

    out_utc = get_current_time.invoke({"timezone_name": "UTC"})
    check("UTC 与其他时区结果不同（真的做了时区换算）", out != out_utc)

    out_bad = get_current_time.invoke({"timezone_name": "Nowhere/Fake"})
    check("非法时区不抛异常，而是降级并说明",
          "本机无该时区数据" in out_bad and "日期：" in out_bad, out_bad.split("\n")[0])

    out_half = get_current_time.invoke({"timezone_name": "Asia/Kolkata"})
    check("半小时时区偏移显示正确（UTC+5:30）", "UTC+5:30" in out_half, out_half.split("\n")[0])


def test_convert_tool() -> None:
    cases = [
        ({"value": 100, "from_unit": "km", "to_unit": "mile"}, "62.137119"),
        ({"value": 37, "from_unit": "celsius", "to_unit": "fahrenheit"}, "98.6"),
        ({"value": 1, "from_unit": "斤", "to_unit": "g"}, "500.0"),
        ({"value": 1, "from_unit": "gb", "to_unit": "mb"}, "1024.0"),
        ({"value": 1, "from_unit": "公顷", "to_unit": "亩"}, "15.0"),
    ]
    for args, expect in cases:
        out = convert_units.invoke(args)
        check(f"换算 {args['value']}{args['from_unit']}→{args['to_unit']}", expect in out, out)

    crossed = convert_units.invoke({"value": 1, "from_unit": "km", "to_unit": "kg"})
    check("跨类别换算被拒绝而不是硬算", "不是同一类单位" in crossed, crossed)

    unknown = convert_units.invoke({"value": 1, "from_unit": "parsec", "to_unit": "m"})
    check("未知单位给出支持列表", "暂不支持" in unknown and "km" in unknown, unknown[:40])


def test_weather_offline_parts() -> None:
    check("WMO 码表覆盖常见天气",
          _describe_wmo(0) == "晴" and _describe_wmo(61) == "小雨" and _describe_wmo(95) == "雷阵雨")
    check("未在码表里的值不崩", _describe_wmo(1234).startswith("未知"), _describe_wmo(1234))
    check("中文城市别名有覆盖（北京/杭州/东京）",
          {"北京", "杭州", "东京"} <= set(_CITY_ALIASES), len(_CITY_ALIASES))

    # 天气工具的错误路径不联网也能测：城市名解析必然失败时返回人话
    from tools.general_tools import get_weather

    out = get_weather.invoke({"city": ""})
    check("空城市名返回可读提示而不是抛异常",
          isinstance(out, str) and ("失败" in out or "没找到" in out or "不可用" in out), out[:60])


# ---------------------------------------------------------------------------
# 2. 提示词
# ---------------------------------------------------------------------------

def test_prompt_content() -> None:
    from app.agent_setup import build_agents_memory
    from app.config import ASSISTANT_NAME

    with_exec = build_agents_memory(has_execution=True)
    without = build_agents_memory(has_execution=False)

    check("提示词里没有残留的占位符",
          "@@NAME@@" not in without and "@@SKILL@@" not in with_exec)
    check("助手名字被填进提示词", ASSISTANT_NAME in without, ASSISTANT_NAME)

    for capability in ("信息查询与解答", "写作与创作", "编程与技术", "分析与处理", "学习与辅导", "日常陪伴"):
        check(f"能力清单包含「{capability}」", capability in without)

    check("明确写了「代码一律通过对话完成，不执行」",
          "代码一律通过对话完成，不执行" in without)
    check("明确禁止声称运行过代码", "不要声称你运行过代码" in without)
    check("要求代码块标注语言（方便用户复制）", "Markdown 代码块并标注语言" in without)

    check("写清了三个工具各自的使用时机",
          all(name in without for name in ("get_current_time", "get_weather", "convert_units")))
    check("要求涉及当前时间时必须调工具", "必须调用工具" in without or "必须先调用" in without)
    check("要求工具失败时如实说明、不编造", "不要编造结果" in without)

    check("关执行时 PPT 段是「不能生成文件」版本", "本部署不能生成文件" in without)
    check("开执行时 PPT 段回到了「跑脚本」版本",
          "generate_pptx.py" in with_exec and "本部署不能生成文件" not in with_exec)


# ---------------------------------------------------------------------------
# 3. 执行开关
# ---------------------------------------------------------------------------

def test_execution_switch() -> None:
    from app.agent_setup import _code_execution_middleware

    mw = _code_execution_middleware()
    check("默认（ENABLE_CODE_EXECUTION=false）挂上工具排除中间件",
          len(mw) == 1 and type(mw[0]).__name__ == "_ToolExclusionMiddleware",
          [type(m).__name__ for m in mw])
    check("排除的就是 execute",
          getattr(mw[0], "_excluded", frozenset()) == frozenset({"execute"}),
          getattr(mw[0], "_excluded", None))

    # 真正验证「模型看不到 execute」：构造一个带 execute 的工具列表，
    # 走中间件后应该少掉它。比只看私有属性更能说明问题。
    from langchain_core.tools import tool as make_tool

    @make_tool
    def execute(command: str) -> str:  # noqa: D401 - 测试桩
        """跑命令。"""
        return command

    @make_tool
    def read_file(path: str) -> str:  # noqa: D401 - 测试桩
        """读文件。"""
        return path

    class _Req:
        def __init__(self, tools):
            self.tools = tools

        def override(self, **kw):
            return _Req(kw.get("tools", self.tools))

    seen = {}

    class _Tool:
        def __init__(self, name):
            self.name = name

    req = _Req([_Tool("execute"), _Tool("read_file")])
    mw[0].wrap_model_call(req, lambda r: seen.update(names=[t.name for t in r.tools]) or r)
    check("模型实际看到的工具列表里没有 execute",
          seen.get("names") == ["read_file"], seen.get("names"))

    # 第二道：即使模型从历史上下文里"记得" execute（旧会话回放），调用边界也必须拦住。
    # 只过滤工具列表是不够的——执行器里 execute 仍然注册着（deepagents 的设计如此）。
    class _Call(dict):
        pass

    blocked = {}

    def handler(call_req):
        blocked["reached"] = True
        return "executed"

    call_req = type("R", (), {"tool_call": {"name": "execute", "args": {"command": "ls"}, "id": "c1"}})()
    result = mw[0].wrap_tool_call(call_req, handler)
    check("调用边界拦下 execute（没走到 handler）", not blocked.get("reached"), blocked)
    check("拦截返回的是「不可用」而不是异常", "not available" in str(getattr(result, "content", result)),
          str(getattr(result, "content", result))[:60])

    allowed = {}
    call_req2 = type("R", (), {"tool_call": {"name": "read_file", "args": {}, "id": "c2"}})()
    mw[0].wrap_tool_call(call_req2, lambda r: allowed.update(ok=True) or "read")
    check("其他工具正常放行", allowed.get("ok") is True)

    # 开着的时候不该有任何排除
    import app.agent_setup as setup
    original = setup.ENABLE_CODE_EXECUTION
    try:
        setup.ENABLE_CODE_EXECUTION = True
        check("打开执行时不排除任何工具", setup._code_execution_middleware() == [])
    finally:
        setup.ENABLE_CODE_EXECUTION = original


def test_http_config_endpoint() -> None:
    from fastapi.testclient import TestClient
    from app import app

    c = TestClient(app)
    r = c.get("/api/config")
    check("/api/config 告知前端是否开启代码执行",
          r.status_code == 200 and isinstance(r.json().get("code_execution"), bool),
          r.json())
    check("/api/config 带上助手名字", bool(r.json().get("assistant_name")), r.json())


def main() -> int:
    test_tool_registration()
    test_time_tool()
    test_convert_tool()
    test_weather_offline_parts()
    test_prompt_content()
    test_execution_switch()
    test_http_config_endpoint()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r)
    print(f"\n{passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
