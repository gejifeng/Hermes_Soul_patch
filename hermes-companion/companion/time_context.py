"""
Turn-level 时间格式化工具。

设计要点：
  - 时区优先级：HERMES_TIMEZONE 环境变量 > ~/.hermes/config.yaml 的 timezone 字段 > 系统本地时区
  - 时区字符串读取一次缓存（_tz_str），但每次调用 format_current_time() 重新求 datetime.now()
  - 任何异常都降级到 datetime.now().astimezone()，保证 hook 永不抛错
"""

import os
from datetime import datetime

# 模块导入时解析一次时区字符串。HERMES_TIMEZONE 优先。
_tz_str = os.environ.get("HERMES_TIMEZONE", "").strip()

if not _tz_str:
    try:
        import yaml  # PyYAML 是 Hermes 的依赖，可直接使用
        from pathlib import Path
        _cfg = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "config.yaml"
        if _cfg.exists():
            _loaded = yaml.safe_load(_cfg.read_text(encoding="utf-8")) or {}
            _tz_str = (_loaded.get("timezone") or "").strip()
    except Exception:
        _tz_str = ""


_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def format_current_time() -> str:
    """
    返回形如 `[Current time: 2026-05-20 14:30 CST 星期三]` 的标签字符串。

    永不抛异常：任何时区解析失败都回退到本地时区。
    """
    try:
        if _tz_str:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(_tz_str))
        else:
            now = datetime.now().astimezone()
    except Exception:
        now = datetime.now().astimezone()

    weekday = _WEEKDAYS_ZH[now.weekday()]
    tz_label = now.strftime("%Z") or now.strftime("%z")
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M')} {tz_label} {weekday}]"
