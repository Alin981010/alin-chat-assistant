"""通用对话工具：时间、天气、单位换算。

## 设计原则

这几个工具的共同点是**答案只有一个、且模型自己算不准**——当前时间、实时天气、
精确换算。凡是模型凭常识能答对的事情（历史、概念、写作、代码解释）都不做工具，
否则只会多烧 token 换更差的答案。

## 无外网依赖的取舍

- **时间**：纯标准库 ``zoneinfo``，不联网。系统缺 tzdata 时退回固定 UTC 偏移，
  并在结果里说明——宁可能力降级也要给出答案，不要抛异常。
- **天气**：Open-Meteo（https://open-meteo.com），免费且**不需要 API key**，
  所以不用往部署里再塞一个密钥。它需要外网；调用失败时返回一句人话，
  让模型据此如实转述，而不是编造天气。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_HTTP_TIMEOUT = 10.0

#: WMO 天气代码 → 中文描述（Open-Meteo 用的是这套码表）
_WMO_CODES: Dict[int, str] = {
    0: "晴",
    1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨（弱）", 53: "毛毛雨（中）", 55: "毛毛雨（强）",
    56: "冻毛毛雨（弱）", 57: "冻毛毛雨（强）",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨（弱）", 67: "冻雨（强）",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨（弱）", 81: "阵雨（中）", 82: "阵雨（强）",
    85: "阵雪（弱）", 86: "阵雪（强）",
    95: "雷阵雨", 96: "雷阵雨伴小冰雹", 99: "雷阵雨伴大冰雹",
}

#: 常见城市的本地称呼 → Open-Meteo 需要的中/英文检索词。
#: 不做全量城市库（那要引数据文件），只补最容易被问到的几个。
_CITY_ALIASES: Dict[str, str] = {
    "北京": "Beijing", "上海": "Shanghai", "广州": "Guangzhou", "深圳": "Shenzhen",
    "杭州": "Hangzhou", "成都": "Chengdu", "重庆": "Chongqing", "武汉": "Wuhan",
    "西安": "Xi'an", "南京": "Nanjing", "天津": "Tianjin", "苏州": "Suzhou",
    "香港": "Hong Kong", "台北": "Taipei", "澳门": "Macau",
    "东京": "Tokyo", "大阪": "Osaka", "首尔": "Seoul", "新加坡": "Singapore",
    "曼谷": "Bangkok", "伦敦": "London", "巴黎": "Paris", "纽约": "New York",
    "洛杉矶": "Los Angeles", "旧金山": "San Francisco", "西雅图": "Seattle",
    "柏林": "Berlin", "慕尼黑": "Munich", "莫斯科": "Moscow", "悉尼": "Sydney",
}


def _describe_wmo(code: Optional[int]) -> str:
    if code is None:
        return "未知"
    return _WMO_CODES.get(int(code), f"未知（代码 {code}）")


def _geocode(client: httpx.Client, city: str, country: Optional[str] = None) -> Dict[str, Any]:
    """把城市名解析成坐标；失败抛 ``RuntimeError``（消息是给人看的）。"""
    query = _CITY_ALIASES.get(city.strip(), city.strip())
    params: Dict[str, Any] = {"name": query, "count": 5, "language": "zh", "format": "json"}
    if country:
        params["countryCode"] = country.strip().upper()[:2]

    try:
        resp = client.get(_GEOCODE_URL, params=params)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as e:
        raise RuntimeError(f"城市检索服务不可用（{type(e).__name__}）：{e}") from e

    results = payload.get("results") or []
    if not results:
        raise RuntimeError(f"没找到城市「{city}」。可以换用英文名或加上国家/地区重试。")

    top = results[0]
    return {
        "name": top.get("name") or city,
        "country": top.get("country") or "",
        "admin": top.get("admin1") or "",
        "latitude": top.get("latitude"),
        "longitude": top.get("longitude"),
        "timezone": top.get("timezone") or "UTC",
        "alternatives": [
            f"{r.get('name')}（{r.get('admin1') or ''} {r.get('country') or ''}）".strip()
            for r in results[1:4]
        ],
    }


@tool
def get_current_time(timezone_name: str = "Asia/Shanghai") -> str:
    """查询当前日期与时间。

    当问题涉及「现在几点 / 今天几号 / 星期几 / 距今多少天」这类需要真实当前时间
    的信息时必须调用本工具——你的训练数据里没有「现在」。

    Args:
        timezone_name: IANA 时区名，例如 ``Asia/Shanghai``、``America/New_York``、
            ``Europe/London``、``UTC``。用户只说城市时自行换算成对应时区，
            例如北京/上海 → ``Asia/Shanghai``，东京 → ``Asia/Tokyo``。
    """
    tz_name = (timezone_name or "Asia/Shanghai").strip()
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    utc_now = datetime.now(timezone.utc)

    try:
        from zoneinfo import ZoneInfo

        local = utc_now.astimezone(ZoneInfo(tz_name))
    except Exception:
        # 缺 tzdata 的镜像会走到这里：退回 UTC，并在结果里说明，避免模型把
        # UTC 当成用户本地时间直接报出去。
        local = utc_now
        tz_name = f"{tz_name}（本机无该时区数据，已按 UTC 返回）"

    offset = local.utcoffset() or timedelta(0)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    # 印度（+5:30）这类半小时时区也要显示正确，所以不能只写小时。
    offset_text = f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")
    offset_hours = offset.total_seconds() / 3600
    beijing = utc_now.astimezone(timezone(timedelta(hours=8))) if offset_hours != 8 else local

    lines = [
        f"时区：{tz_name}（{offset_text}）",
        f"日期：{local.strftime('%Y-%m-%d')}",
        f"时间：{local.strftime('%H:%M:%S')}",
        f"星期：{weekday_cn[local.weekday()]}",
    ]
    if beijing is not local:
        lines.append(f"北京时间：{beijing.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Unix 时间戳：{int(utc_now.timestamp())}")
    return "\n".join(lines)


@tool
def get_weather(city: str, country: Optional[str] = None, days: int = 3) -> str:
    """查询某个城市的实时天气与未来几天预报。

    用户问「今天/明天天气」「要不要带伞」「那边冷不冷」时使用。数据来自
    Open-Meteo，覆盖全球主要城市。

    Args:
        city: 城市名，中文或英文都可以，例如 ``杭州``、``Tokyo``。
        country: 可选，ISO 两位国家代码（``CN``/``JP``/``US``），重名城市较多时用它限定。
        days: 预报天数，1–7，默认 3。
    """
    try:
        day_count = max(1, min(int(days), 7))
    except (TypeError, ValueError):
        day_count = 3

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            place = _geocode(client, city, country)
            resp = client.get(
                _FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                               "precipitation,weather_code,wind_speed_10m",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                             "precipitation_probability_max,sunrise,sunset",
                    "forecast_days": day_count,
                    "timezone": "auto",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except RuntimeError as e:
        return f"查询失败：{e}"
    except httpx.HTTPError as e:
        return f"天气服务暂时不可用（{type(e).__name__}），无法给出实时天气，请稍后再试。"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"get_weather 异常：{e}", exc_info=True)
        return f"查询天气时出错：{e}"

    label = f"{place['name']}"
    if place.get("admin") and place["admin"] != place["name"]:
        label += f"（{place['admin']}）"
    if place.get("country"):
        label += f"，{place['country']}"

    current = data.get("current") or {}
    lines = [
        f"【{label}】实时天气（当地 {current.get('time', '')}）",
        f"天气：{_describe_wmo(current.get('weather_code'))}",
        f"气温：{current.get('temperature_2m')}°C（体感 {current.get('apparent_temperature')}°C）",
        f"湿度：{current.get('relative_humidity_2m')}%",
        f"风速：{current.get('wind_speed_10m')} km/h",
        f"降水：{current.get('precipitation')} mm",
    ]

    daily = data.get("daily") or {}
    dates: List[str] = daily.get("time") or []
    if dates:
        lines.append("")
        lines.append("未来预报：")
        for i, day in enumerate(dates):
            code = (daily.get("weather_code") or [None])[i] if i < len(daily.get("weather_code") or []) else None
            high = (daily.get("temperature_2m_max") or [None])[i] if i < len(daily.get("temperature_2m_max") or []) else None
            low = (daily.get("temperature_2m_min") or [None])[i] if i < len(daily.get("temperature_2m_min") or []) else None
            rain = (daily.get("precipitation_probability_max") or [None])[i] if i < len(daily.get("precipitation_probability_max") or []) else None
            lines.append(f"- {day}：{_describe_wmo(code)}，{low}~{high}°C，降水概率 {rain}%")

    if place.get("alternatives"):
        lines.append("")
        lines.append("同名地点还有：" + "、".join(place["alternatives"]) + "（不是这个的话请说明国家和地区）")
    return "\n".join(lines)


@tool
def convert_units(value: float, from_unit: str, to_unit: str) -> str:
    """做常见单位换算（长度、重量、温度、面积、速度、存储容量）。

    模型自己算这类数字很容易出错，需要精确结果时用本工具。

    Args:
        value: 待换算的数值。
        from_unit: 原单位，例如 ``km``、``mile``、``kg``、``lb``、``celsius``、
            ``fahrenheit``、``m2``、``kmh``、``mb``。
        to_unit: 目标单位，同上。温度要在摄氏度/华氏度/开尔文之间转换。
    """
    # 单位 → 对应「基准单位」的换算系数。温度单独处理（有偏移量，不能只乘系数）。
    factors: Dict[str, tuple] = {
        # 长度（基准：米）
        "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0),
        "km": ("length", 1000.0), "inch": ("length", 0.0254), "ft": ("length", 0.3048),
        "mile": ("length", 1609.344), "海里": ("length", 1852.0),
        # 重量（基准：千克）
        "mg": ("weight", 1e-6), "g": ("weight", 0.001), "kg": ("weight", 1.0),
        "ton": ("weight", 1000.0), "lb": ("weight", 0.45359237), "oz": ("weight", 0.028349523125),
        "斤": ("weight", 0.5),
        # 面积（基准：平方米）
        "m2": ("area", 1.0), "km2": ("area", 1e6), "cm2": ("area", 1e-4),
        "亩": ("area", 666.6666666666666), "公顷": ("area", 10000.0),
        "ft2": ("area", 0.09290304), "acre": ("area", 4046.8564224),
        # 速度（基准：米/秒）
        "ms": ("speed", 1.0), "kmh": ("speed", 1 / 3.6), "mph": ("speed", 0.44704),
        "节": ("speed", 0.514444),
        # 存储（基准：字节）
        "b": ("data", 1.0), "kb": ("data", 1024.0), "mb": ("data", 1024.0 ** 2),
        "gb": ("data", 1024.0 ** 3), "tb": ("data", 1024.0 ** 4),
    }
    aliases = {
        "毫米": "mm", "厘米": "cm", "米": "m", "公里": "km", "千米": "km",
        "英寸": "inch", "英尺": "ft", "英里": "mile",
        "毫克": "mg", "克": "g", "千克": "kg", "公斤": "kg", "吨": "ton",
        "磅": "lb", "盎司": "oz",
        "平方米": "m2", "平方公里": "km2", "平方英尺": "ft2", "英亩": "acre",
        "米每秒": "ms", "公里每小时": "kmh", "英里每小时": "mph",
        "字节": "b", "千字节": "kb", "兆字节": "mb", "吉字节": "gb", "太字节": "tb",
        "c": "celsius", "°c": "celsius", "摄氏度": "celsius",
        "f": "fahrenheit", "°f": "fahrenheit", "华氏度": "fahrenheit",
        "k": "kelvin", "开尔文": "kelvin",
    }

    def norm(unit: str) -> str:
        key = (unit or "").strip().lower().replace(" ", "")
        return aliases.get(key, key)

    src, dst = norm(from_unit), norm(to_unit)
    temp_units = {"celsius", "fahrenheit", "kelvin"}

    if src in temp_units and dst in temp_units:
        celsius = {
            "celsius": float(value),
            "fahrenheit": (float(value) - 32) * 5 / 9,
            "kelvin": float(value) - 273.15,
        }[src]
        result = {
            "celsius": celsius,
            "fahrenheit": celsius * 9 / 5 + 32,
            "kelvin": celsius + 273.15,
        }[dst]
        return f"{value} {from_unit} = {round(result, 6)} {to_unit}"

    if src not in factors or dst not in factors:
        unknown = src if src not in factors else dst
        return f"暂不支持单位「{unknown}」。支持：长度(mm/cm/m/km/inch/ft/mile/海里)、重量(mg/g/kg/ton/lb/oz/斤)、面积(m2/km2/亩/公顷/ft2/acre)、速度(ms/kmh/mph/节)、存储(b/kb/mb/gb/tb)、温度(celsius/fahrenheit/kelvin)。"

    src_kind, src_factor = factors[src]
    dst_kind, dst_factor = factors[dst]
    if src_kind != dst_kind:
        return f"这两者不是同一类单位（{src} 是{src_kind}，{dst} 是{dst_kind}），无法换算。"

    result = float(value) * src_factor / dst_factor
    return f"{value} {from_unit} = {round(result, 6)} {to_unit}"


#: 注册给 agent 的通用工具。顺序只影响提示词里的列举顺序。
GENERAL_TOOLS = [get_current_time, get_weather, convert_units]
