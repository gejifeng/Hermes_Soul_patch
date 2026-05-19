"""
emotion_state 模块测试。
"""

import json
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def emo(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sys.modules.pop("companion.emotion_state", None)
    import importlib
    return importlib.import_module("companion.emotion_state")


def test_default_when_no_file(emo):
    state = emo._read_state()
    assert state["dominant"] == "calm"
    assert state["valence"] == 0.2
    assert state["arousal"] == 0.4


def test_update_emotion_writes_atomic_json(emo, tmp_path):
    emo.update_emotion(0.5, 0.6, "curious", "新主题让我兴奋")
    p = tmp_path / "EMOTION_STATE.md"
    assert p.exists()
    text = p.read_text(encoding="utf-8")
    m = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    assert m
    obj = json.loads(m.group(1))
    assert obj["dominant"] == "curious"
    assert obj["note"] == "新主题让我兴奋"
    assert obj["updated_at"]  # 非空 ISO 时间戳
    assert -1.0 <= obj["valence"] <= 1.0
    assert 0.0 <= obj["arousal"] <= 1.0


def test_clamp(emo):
    s = emo.update_emotion(2.5, -3, "wild", "out of range")
    assert s["valence"] == 1.0
    assert s["arousal"] == 0.0


def test_load_emotion_state_returns_markdown_block(emo):
    emo.update_emotion(-0.3, 0.7, "weary", "测试描述")
    block = emo.load_emotion_state()
    assert "valence" in block
    assert "weary" in block
    assert "测试描述" in block


def test_nudge_emotion_increments(emo):
    emo.update_emotion(0.0, 0.5, "calm", "init")
    s = emo.nudge_emotion(valence_delta=-0.2, dominant="frustrated", note="工具错")
    assert s["dominant"] == "frustrated"
    assert s["valence"] == pytest.approx(-0.2)
    # arousal 未指定 → 保持
    assert s["arousal"] == 0.5


def test_corrupt_file_falls_back_to_default(emo, tmp_path):
    (tmp_path / "EMOTION_STATE.md").write_text("garbage no json block", encoding="utf-8")
    state = emo._read_state()
    assert state == emo._DEFAULT_STATE


def test_partial_schema_merged_with_defaults(emo, tmp_path):
    (tmp_path / "EMOTION_STATE.md").write_text(
        '# x\n```json\n{"valence": 0.9}\n```\n', encoding="utf-8"
    )
    state = emo._read_state()
    assert state["valence"] == 0.9
    # 缺字段用默认补齐
    assert state["dominant"] == "calm"
    assert state["arousal"] == 0.4
