"""
daily_seed 模块单元测试（设计见 core_idea/core.md §4.8）。

验证：
  - 默认锚点写入
  - 配置文件 anchors 覆盖默认值
  - 已有 schedule 时跳过（幂等，不覆盖手工编辑）
  - 全局/LLM 开关（env + config）生效
  - LLM 不可用时只写锚点
  - LLM happy path：mock _call_aux_llm 验证事件被解析、归一时间、写入
  - LLM 输出乱码时优雅降级
  - base_url/provider/model 等显式覆盖参数被传给 call_llm
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _fresh(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for m in ("companion.world_state", "companion.emotion_state",
              "companion.heartbeat", "companion.daily_seed"):
        sys.modules.pop(m, None)
    ds = importlib.import_module("companion.daily_seed")
    ws = importlib.import_module("companion.world_state")
    return ds, ws


def _write_config(tmp_path: Path, cfg: dict) -> None:
    p = tmp_path / "companion"
    p.mkdir(parents=True, exist_ok=True)
    (p / "daily_seed.json").write_text(json.dumps(cfg), encoding="utf-8")


# ---------------- 基础行为 ----------------

def test_seed_writes_default_anchors(monkeypatch, tmp_path):
    """无配置时写入 DEFAULT_ANCHORS（不调 LLM）。"""
    ds, ws = _fresh(monkeypatch, tmp_path)
    # 强制关闭 LLM 隔离测试
    monkeypatch.setenv("HERMES_COMPANION_DAILY_SEED_LLM", "0")

    r = ds.seed_today_if_empty()
    assert r["skipped"] is False
    assert r["anchors_added"] == len(ds.DEFAULT_ANCHORS)
    assert r["llm_added"] == 0

    today = ws.list_today()
    titles = [e["title"] for e in today["schedule"]]
    assert any("午餐" in t for t in titles)


def test_seed_uses_config_anchors(monkeypatch, tmp_path):
    _write_config(tmp_path, {
        "enabled": True,
        "anchors": [
            {"start": "07:00", "end": "07:30", "title": "晨跑", "kind": "self"},
        ],
        "llm": {"enabled": False},
    })
    ds, ws = _fresh(monkeypatch, tmp_path)

    r = ds.seed_today_if_empty()
    assert r["anchors_added"] == 1
    assert ws.list_today()["schedule"][0]["title"] == "晨跑"


def test_seed_idempotent_when_schedule_exists(monkeypatch, tmp_path):
    """已有 schedule 时直接 skipped=True，不重复写。"""
    ds, ws = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_COMPANION_DAILY_SEED_LLM", "0")
    # 预先放一条
    ws.add_event("09:00", "10:00", "已有的事", kind="self") if False else None
    # add_event 需要 ISO，绕过用直接 add
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    ws.add_event(f"{today}T09:00", f"{today}T10:00", "已有的事", kind="self")

    r = ds.seed_today_if_empty()
    assert r["skipped"] is True
    assert r["anchors_added"] == 0
    titles = [e["title"] for e in ws.list_today()["schedule"]]
    assert titles == ["已有的事"]


def test_env_disable_overall(monkeypatch, tmp_path):
    ds, ws = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_COMPANION_DAILY_SEED", "0")

    r = ds.seed_today_if_empty()
    assert r["skipped"] is True
    assert r["anchors_added"] == 0
    assert ws.list_today()["schedule"] == []


def test_config_disable_overall(monkeypatch, tmp_path):
    _write_config(tmp_path, {"enabled": False})
    ds, ws = _fresh(monkeypatch, tmp_path)
    r = ds.seed_today_if_empty()
    assert r["skipped"] is True
    assert ws.list_today()["schedule"] == []


# ---------------- LLM 增补 ----------------

def test_llm_unavailable_falls_back_to_anchors_only(monkeypatch, tmp_path):
    """auxiliary_client 不可导入时，至少锚点已落地。"""
    ds, ws = _fresh(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise ImportError("no hermes")

    monkeypatch.setattr(ds, "_call_aux_llm", _boom)
    r = ds.seed_today_if_empty()
    assert r["anchors_added"] > 0
    assert r["llm_added"] == 0


def test_llm_happy_path(monkeypatch, tmp_path):
    _write_config(tmp_path, {
        "enabled": True,
        "anchors": [{"start": "12:00", "end": "13:00", "title": "午餐", "kind": "self"}],
        "llm": {"enabled": True, "extra_count": 2},
    })
    ds, ws = _fresh(monkeypatch, tmp_path)

    fake_raw = json.dumps({"events": [
        {"start": "15:00", "end": "16:00", "title": "去河边散步", "kind": "self"},
        {"start": "21:30", "title": "给用户写晚安消息", "kind": "interaction"},
    ]}, ensure_ascii=False)
    monkeypatch.setattr(ds, "_call_aux_llm", lambda msgs, cfg: fake_raw)

    r = ds.seed_today_if_empty()
    assert r["anchors_added"] == 1
    assert r["llm_added"] == 2

    titles = [e["title"] for e in ws.list_today()["schedule"]]
    assert "去河边散步" in titles
    assert "给用户写晚安消息" in titles
    # end 缺省补 30 分钟
    interaction = next(e for e in ws.list_today()["schedule"] if e["title"] == "给用户写晚安消息")
    assert interaction["end"].endswith("T22:00")
    assert interaction["kind"] == "interaction"


def test_llm_bad_output_logged_no_crash(monkeypatch, tmp_path):
    ds, _ = _fresh(monkeypatch, tmp_path)
    monkeypatch.setattr(ds, "_call_aux_llm", lambda msgs, cfg: "我无法生成 JSON 😅")
    r = ds.seed_today_if_empty()
    assert r["llm_added"] == 0
    assert r["anchors_added"] > 0  # 锚点应已落地


def test_llm_extra_count_zero_skips_call(monkeypatch, tmp_path):
    _write_config(tmp_path, {
        "anchors": [{"start": "12:00", "end": "13:00", "title": "午餐"}],
        "llm": {"enabled": True, "extra_count": 0},
    })
    ds, _ = _fresh(monkeypatch, tmp_path)

    called = []
    monkeypatch.setattr(ds, "_call_aux_llm", lambda msgs, cfg: called.append(1) or "{}")
    r = ds.seed_today_if_empty()
    assert r["llm_added"] == 0
    assert called == []


def test_llm_config_passthrough_for_openai_compat(monkeypatch, tmp_path):
    """验证 base_url/api_key/provider/model 显式覆盖被透传给 call_llm。"""
    _write_config(tmp_path, {
        "anchors": [],
        "llm": {
            "enabled": True,
            "extra_count": 1,
            "provider": "custom",
            "model": "gpt-4o-mini",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-fake",
            "temperature": 0.9,
        },
    })
    ds, _ = _fresh(monkeypatch, tmp_path)

    captured: dict = {}

    def _fake_call(messages, llm_cfg):
        captured.update(llm_cfg)
        return json.dumps({"events": [
            {"start": "15:00", "title": "x", "kind": "self"},
        ]})

    monkeypatch.setattr(ds, "_call_aux_llm", _fake_call)
    ds.seed_today_if_empty()

    assert captured["provider"] == "custom"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["base_url"] == "https://api.example.com/v1"
    assert captured["api_key"] == "sk-fake"
    assert captured["temperature"] == 0.9


def test_call_aux_llm_forwards_overrides(monkeypatch, tmp_path):
    """单测 _call_aux_llm 自身：mock auxiliary_client 验证 kwargs 正确组装。"""
    ds, _ = _fresh(monkeypatch, tmp_path)

    captured_kwargs: dict = {}

    class _FakeResp:
        pass

    def _fake_call_llm(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeResp()

    def _fake_extract(resp):
        return '{"events": []}'

    fake_module = type(sys)("agent.auxiliary_client")
    fake_module.call_llm = _fake_call_llm  # type: ignore
    fake_module.extract_content_or_reasoning = _fake_extract  # type: ignore
    parent = type(sys)("agent")
    parent.auxiliary_client = fake_module  # type: ignore

    with patch.dict(sys.modules, {"agent": parent, "agent.auxiliary_client": fake_module}):
        out = ds._call_aux_llm(
            [{"role": "user", "content": "hi"}],
            {
                "task": "compression",
                "provider": "custom",
                "model": "qwen3-coder",
                "base_url": "https://x.example.com/v1",
                "api_key": "sk-x",
                "temperature": 0.55,
                "max_tokens": 1234,
            },
        )
    assert out == '{"events": []}'
    assert captured_kwargs["task"] == "compression"
    assert captured_kwargs["provider"] == "custom"
    assert captured_kwargs["model"] == "qwen3-coder"
    assert captured_kwargs["base_url"] == "https://x.example.com/v1"
    assert captured_kwargs["api_key"] == "sk-x"
    assert captured_kwargs["temperature"] == 0.55
    assert captured_kwargs["max_tokens"] == 1234
    # extra_body 总是带 enable_thinking=False
    assert captured_kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


# ---------------- archiver 集成 ----------------

def test_seed_invoked_by_archiver_startup(monkeypatch, tmp_path):
    """start_daily_archiver 的启动 pass 会调 seed_today_if_empty。"""
    ds, ws = _fresh(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_COMPANION_DAILY_SEED_LLM", "0")

    # 让线程不要进入无限循环（archiver 本身有 sleep，这里我们等启动 pass 就够）
    ws.start_daily_archiver()
    # 等启动 pass 跑完
    import time
    for _ in range(50):
        if ws.list_today()["schedule"]:
            break
        time.sleep(0.02)

    assert len(ws.list_today()["schedule"]) == len(ds.DEFAULT_ANCHORS)
