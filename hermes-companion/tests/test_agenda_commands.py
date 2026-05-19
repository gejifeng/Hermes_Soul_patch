"""
/agenda 系列 slash command 的端到端单元测试。

验证：
  - register_commands 注册了全部 5 个 agenda 相关命令
  - 各命令的 happy path 返回字符串、不抛异常
  - 参数错误时返回友好提示而不是 traceback
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fresh_commands(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for m in ("companion.world_state", "companion.emotion_state",
              "companion.heartbeat", "plugin.commands"):
        sys.modules.pop(m, None)
    return importlib.import_module("plugin.commands")


def test_register_includes_agenda_commands(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    registered: dict[str, object] = {}

    class _FakeCtx:
        def register_command(self, name, handler, description="", args_hint=""):
            registered[name] = handler

    cmds.register_commands(_FakeCtx())
    for name in ("agenda", "agenda-add", "agenda-done",
                 "agenda-ambient", "recall"):
        assert name in registered, f"missing /{name}"


def test_agenda_empty(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda("")
    assert "无日程" in out


def test_agenda_add_and_list(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda_add('14:00 15:00 "讨论架构" interaction')
    assert out.startswith("✓")
    listing = cmds.cmd_agenda("")
    assert "讨论架构" in listing
    assert "interaction" in listing


def test_agenda_add_missing_args(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda_add("14:00")
    assert out.startswith("用法")


def test_agenda_add_invalid_kind(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda_add('14:00 15:00 "x" bogus')
    assert "参数错误" in out


def test_agenda_add_no_end_dash(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda_add('14:00 - "无终点"')
    assert out.startswith("✓")


def test_agenda_done_roundtrip(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    cmds.cmd_agenda_add('14:00 15:00 "x"')
    # 拿到 id：从 cmd_agenda 输出解析
    listing = cmds.cmd_agenda("")
    # 行形如 "  · [abc12345] 14:00–15:00 x  «self»"
    eid = None
    for ln in listing.splitlines():
        if "[" in ln and "]" in ln:
            eid = ln.split("[", 1)[1].split("]", 1)[0]
            break
    assert eid
    out = cmds.cmd_agenda_done(eid)
    assert out.startswith("✓")
    again = cmds.cmd_agenda_done("nonexistent")
    assert "未找到" in again


def test_agenda_done_no_args(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    assert cmds.cmd_agenda_done("").startswith("用法")


def test_agenda_ambient(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    out = cmds.cmd_agenda_ambient("下雨了")
    assert out.startswith("✓")
    assert "下雨了" in cmds.cmd_agenda("")
    assert cmds.cmd_agenda_ambient("").startswith("用法")


def test_recall_missing_and_invalid(monkeypatch, tmp_path):
    cmds = _fresh_commands(monkeypatch, tmp_path)
    assert cmds.cmd_recall("").startswith("用法")
    assert "格式无效" in cmds.cmd_recall("not-a-date")
    assert "没有归档" in cmds.cmd_recall("2025-01-01")
