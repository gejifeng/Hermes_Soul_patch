"""
time_context 与 pre_llm_call 时间注入的单元测试。

不依赖任何 Hermes 模块，可独立运行：
    cd hermes-companion && python -m pytest tests/test_time_context.py -v
"""

import importlib
import os
import re
import sys
from pathlib import Path

# 让 tests/ 可直接 import 同级的 companion/ 与 plugin/
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


_TIME_TAG_RE = re.compile(
    r"^\[Current time: \d{4}-\d{2}-\d{2} \d{2}:\d{2} .+ "
    r"(星期一|星期二|星期三|星期四|星期五|星期六|星期日)\]$"
)


def _fresh_time_context(monkeypatch, *, tz: str | None = None, hermes_home: Path | None = None):
    """重新 import time_context，让模块级 _tz_str 重新求值。"""
    if tz is None:
        monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    else:
        monkeypatch.setenv("HERMES_TIMEZONE", tz)
    if hermes_home is not None:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    else:
        monkeypatch.delenv("HERMES_HOME", raising=False)

    # Most tests exercise the local fallback path. A real hermes_time module may
    # be installed on the developer machine and would otherwise override the
    # temporary HERMES_HOME config. The dedicated precedence test injects its own
    # fake hermes_time after this helper runs.
    if "hermes_time" not in getattr(_fresh_time_context, "_keep_modules", set()):
        monkeypatch.setitem(sys.modules, "hermes_time", None)

    sys.modules.pop("companion.time_context", None)
    return importlib.import_module("companion.time_context")


def test_format_current_time_default_local(monkeypatch):
    mod = _fresh_time_context(monkeypatch)
    tag = mod.format_current_time()
    assert _TIME_TAG_RE.match(tag), f"tag 格式不符: {tag!r}"


def test_format_current_time_explicit_tz(monkeypatch):
    mod = _fresh_time_context(monkeypatch, tz="Asia/Shanghai")
    tag = mod.format_current_time()
    assert _TIME_TAG_RE.match(tag), f"tag 格式不符: {tag!r}"
    # 新实现直接展示 IANA 名
    assert "Asia/Shanghai" in tag, tag


def test_format_current_time_invalid_tz_falls_back(monkeypatch):
    mod = _fresh_time_context(monkeypatch, tz="Not/A_Real_Zone")
    # 不应抛异常，应回退到本地时区
    tag = mod.format_current_time()
    assert _TIME_TAG_RE.match(tag), f"tag 格式不符: {tag!r}"


def test_config_yaml_timezone_used_when_env_unset(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("timezone: Asia/Tokyo\n", encoding="utf-8")
    mod = _fresh_time_context(monkeypatch, tz=None, hermes_home=tmp_path)
    tag = mod.format_current_time()
    assert _TIME_TAG_RE.match(tag), tag
    assert "Asia/Tokyo" in tag, tag


def test_hermes_time_module_takes_precedence(monkeypatch, tmp_path):
    """伪造 hermes_time._resolve_timezone_name，验证它优先于本地 env/config。"""
    import types
    fake = types.ModuleType("hermes_time")
    fake._resolve_timezone_name = lambda: "America/New_York"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_time", fake)
    _fresh_time_context._keep_modules = {"hermes_time"}
    try:
        # 即使 env 设了别的也应被 hermes_time 覆盖
        mod = _fresh_time_context(monkeypatch, tz="Asia/Shanghai", hermes_home=tmp_path)
    finally:
        _fresh_time_context._keep_modules = set()
    tag = mod.format_current_time()
    assert "America/New_York" in tag, tag


def test_pre_llm_call_hook_returns_time_context(monkeypatch, tmp_path):
    _fresh_time_context(monkeypatch, hermes_home=tmp_path)
    # 让 hooks 模块也使用新 HERMES_HOME（emotion_state / heartbeat 动态求值，无需重 import）
    for mod in ("companion.emotion_state", "companion.heartbeat",
                "companion.emotion_inference", "plugin.hooks"):
        sys.modules.pop(mod, None)
    hooks = importlib.import_module("plugin.hooks")

    result = hooks.on_pre_llm_call(
        session_id="s1",
        user_message="hi",
        is_first_turn=True,
        model="gpt-4",
        platform="cli",
        sender_id="user",
    )
    assert isinstance(result, dict)
    assert "context" in result
    # 第一行必须是时间标签
    first_line = result["context"].splitlines()[0]
    assert _TIME_TAG_RE.match(first_line), first_line
    # 应包含情感状态块（默认状态）
    assert "[Companion状态]" in result["context"]


def test_register_hooks_registers_pre_llm_call(monkeypatch, tmp_path):
    _fresh_time_context(monkeypatch, hermes_home=tmp_path)
    for mod in ("companion.emotion_state", "companion.heartbeat",
                "companion.emotion_inference", "plugin.hooks"):
        sys.modules.pop(mod, None)
    hooks = importlib.import_module("plugin.hooks")

    registered: dict[str, object] = {}

    class _FakeCtx:
        def register_hook(self, name, cb):
            registered[name] = cb

    hooks.register_hooks(_FakeCtx())
    assert "pre_llm_call" in registered
    assert "post_llm_call" in registered
    assert "post_tool_call" in registered
    assert "on_session_start" in registered
    assert registered["pre_llm_call"] is hooks.on_pre_llm_call
