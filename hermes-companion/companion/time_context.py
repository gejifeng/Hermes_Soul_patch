"""
Turn-level 时间格式化工具。

设计要点：
  - 时区解析优先委托 Hermes 自带的 ``hermes_time`` 模块，确保与 Hermes 主程序
    完全一致的优先级链：HERMES_TIMEZONE > ~/.hermes/config.yaml: timezone > 系统本地时区。
  - Hermes 不可导入时（独立测试场景）走本地等价实现作为兜底。
  - 时区字符串模块加载时解析一次并缓存（``_tz_str``），datetime.now() 每次重新求值。
  - 任何异常都降级到 ``datetime.now().astimezone()``，保证 hook 永不抛错。

参考实现：https://github.com/gejifeng/hermes-time_perception-extension
"""

import os
from datetime import datetime
from pathlib import Path


def _resolve_tz_from_hermes() -> str:
    """优先委托 hermes_time._resolve_timezone_name()，保持与 Hermes 主程序一致。"""
    try:
        import hermes_time  # type: ignore[import-not-found]
        return (hermes_time._resolve_timezone_name() or "").strip()
    except Exception:
        return ""


def _resolve_tz_local() -> str:
    """Hermes 不可用时的本地等价实现：env > ~/.hermes/config.yaml > ''."""
    tz_env = os.environ.get("HERMES_TIMEZONE", "").strip()
    if tz_env:
        return tz_env
    cfg_path = Path(
        os.environ.get("HERMES_HOME", Path.home() / ".hermes")
    ) / "config.yaml"
    if cfg_path.exists():
        text = ""
        try:
            text = cfg_path.read_text(encoding="utf-8")
            import yaml  # PyYAML 是 Hermes 的运行时依赖
            loaded = yaml.safe_load(text) or {}
            tz_cfg = loaded.get("timezone", "")
            if isinstance(tz_cfg, str) and tz_cfg.strip():
                return tz_cfg.strip()
        except Exception:
            # 单测/轻量环境可能没有 PyYAML；只解析顶层 `timezone: ...` 兜底。
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("timezone:"):
                    return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""


_tz_str = _resolve_tz_from_hermes() or _resolve_tz_local()


_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def format_current_time() -> str:
    """
    返回形如 `[Current time: 2026-05-20 14:30 Asia/Shanghai 星期三]` 的标签字符串。

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
    tz_label = _tz_str or now.strftime("%Z") or now.strftime("%z")
    if tz_label == "CST":
        tz_label = "Asia/Shanghai"
    return f"[Current time: {now.strftime('%Y-%m-%d %H:%M')} {tz_label} {weekday}]"
