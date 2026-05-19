"""
每日初始日程生成器（方案 C：固定锚点 + LLM 增补，按人设生成）。

设计参见 core_idea/core.md §4.8。

工作流：
    daily archiver 在 00:00:30 跑完 roll_over_if_new_day() 后调一次
    seed_today_if_empty(now)。如果当天 events.json.schedule 是空的：

      Step A — 写入固定锚点（来自配置文件，例：晨间冥想 / 午餐 / 睡前整理）
      Step B — 调辅助 LLM 基于 SOUL.md + 锚点生成 1~2 条"今日特色"事件
               例：「今天阳光不错，下午想去河边走 30 分钟」

      若 schedule 已经被用户/LLM 手动填过，整步骤跳过（不覆盖手工编辑）。
      LLM 失败仅记 warning，至少锚点已落地，不阻塞 agent 启动。

配置（按优先级）：
    1. ~/.hermes/companion/daily_seed.json    完整配置（可选）
    2. 环境变量
         HERMES_COMPANION_DAILY_SEED=0       完全关闭
         HERMES_COMPANION_DAILY_SEED_LLM=0   只关 LLM 增补，保留锚点
    3. 内置默认值（DEFAULT_ANCHORS）

daily_seed.json schema:
    {
      "enabled": true,
      "anchors": [
        {"start": "08:00", "end": "08:30", "title": "...", "kind": "self"}
      ],
      "llm": {
        "enabled": true,
        "task": "title_generation",     # 复用 hermes 辅助任务配置
        "provider": null,                # 显式覆盖：openai/anthropic/custom 等
        "model": null,
        "base_url": null,                # OpenAI 兼容 API 入口
        "api_key": null,
        "temperature": 0.7,
        "max_tokens": 800,
        "extra_count": 2                 # 期望 LLM 增补几条
      },
      "soul_path": null                  # null = 用 $HERMES_HOME/SOUL.md
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from companion.emotion_state import hermes_home
from companion.world_state import (
    _KIND_INTERACTION,
    _KIND_SELF,
    _VALID_KINDS,
    add_event,
    list_today,
)

logger = logging.getLogger(__name__)

# ---------------- 默认锚点 ----------------
# 用户没提供 daily_seed.json 时的兜底，体现"基础生活节奏"。
DEFAULT_ANCHORS: list[dict] = [
    {"start": "08:00", "end": "08:30", "title": "起床、简单整理一下房间", "kind": "self"},
    {"start": "12:30", "end": "13:30", "title": "午餐与短暂休息", "kind": "self"},
    {"start": "18:30", "end": "19:30", "title": "晚餐", "kind": "self"},
    {"start": "22:30", "end": "23:00", "title": "睡前整理今日笔记", "kind": "self"},
]

_AUX_TASK_DEFAULT = "title_generation"  # 同 emotion_inference，用最轻量的任务通道
_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")


# ---------------- 配置加载 ----------------

def _config_path() -> Path:
    return hermes_home() / "companion" / "daily_seed.json"


def _load_config() -> dict:
    """读 daily_seed.json，失败则返回空 dict（走默认值）。"""
    p = _config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("[daily_seed] 读取配置失败 (%s): %s", p, e)
        return {}


def _enabled_overall(cfg: dict) -> bool:
    if os.environ.get("HERMES_COMPANION_DAILY_SEED", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return False
    return bool(cfg.get("enabled", True))


def _enabled_llm(cfg: dict) -> bool:
    if os.environ.get("HERMES_COMPANION_DAILY_SEED_LLM", "1").strip().lower() in (
        "0", "false", "no", "off"
    ):
        return False
    return bool(cfg.get("llm", {}).get("enabled", True))


def _resolve_anchors(cfg: dict) -> list[dict]:
    anchors = cfg.get("anchors")
    if anchors is None:
        return list(DEFAULT_ANCHORS)
    if not isinstance(anchors, list):
        logger.warning("[daily_seed] anchors 必须是数组，忽略，用默认值")
        return list(DEFAULT_ANCHORS)
    return anchors


def _resolve_soul(cfg: dict) -> str:
    """读取 SOUL.md 内容。优先 cfg.soul_path，否则 $HERMES_HOME/SOUL.md。"""
    explicit = cfg.get("soul_path")
    if explicit:
        p = Path(explicit).expanduser()
    else:
        p = hermes_home() / "SOUL.md"
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("[daily_seed] 读取 SOUL.md 失败: %s", e)
        return ""


# ---------------- 时间归一 ----------------

def _to_today_iso(s: str, today: str) -> str:
    """把 HH:MM 或完整 ISO 转成 'YYYY-MM-DDTHH:MM'（今天的）。"""
    s = (s or "").strip()
    if not s:
        return ""
    if "T" in s and len(s) >= 16:
        return s
    if len(s) >= 5 and s[2] == ":":
        return f"{today}T{s[:5]}"
    return s  # 让 add_event 自己校验


def _default_end(start_iso: str, minutes: int = 30) -> str:
    from datetime import timedelta
    try:
        fmt = "%Y-%m-%dT%H:%M" if len(start_iso) == 16 else "%Y-%m-%dT%H:%M:%S"
        return (datetime.strptime(start_iso, fmt) + timedelta(minutes=minutes)).strftime(
            "%Y-%m-%dT%H:%M"
        )
    except (ValueError, TypeError):
        return ""


# ---------------- LLM 增补 ----------------

_SYSTEM_PROMPT_TPL = """你是一个 AI 伴侣 agent 的"日程编排器"，需要为这个 agent 生成她今天会"自然而然想做"的特色事件。

她的人设（SOUL.md 摘录）：
---
{soul}
---

今天是 {date_human}。

她今天已经预设了这些固定锚点（不要重复，但请避免与它们冲突）：
{anchors_brief}

请生成 {count} 条 1-2 件"今日特色"事件。要求：
- 必须符合人设、有"今天特别想做"的味道，而不是泛泛的日常
- 时间避开已有锚点 ±30 分钟
- 中文 title，简洁一句话
- kind 二选一：self（她自己安排）/ interaction（涉及与用户互动的提议）

严格输出一行 JSON，不要任何前后缀，不要 ```：
{{"events": [{{"start": "HH:MM", "end": "HH:MM", "title": "...", "kind": "self"}}]}}
"""


def _format_anchors_brief(anchors: list[dict]) -> str:
    if not anchors:
        return "（无）"
    return "\n".join(
        f"- {a.get('start', '?')}~{a.get('end', '?')} {a.get('title', '?')}"
        for a in anchors
    )


def _call_aux_llm(messages: list[dict], llm_cfg: dict) -> str:
    """复用 hermes auxiliary_client。llm_cfg 中非空字段会覆盖 task 配置。"""
    from agent.auxiliary_client import call_llm, extract_content_or_reasoning

    kwargs: dict[str, Any] = {
        "task": llm_cfg.get("task") or _AUX_TASK_DEFAULT,
        "messages": messages,
        "temperature": llm_cfg.get("temperature", 0.7),
        "max_tokens": llm_cfg.get("max_tokens", 800),
        # Qwen3+ 关闭 thinking 直接输出 JSON；其他 provider 忽略
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    # 显式覆盖（OpenAI 兼容 API 入口走这里）
    for k in ("provider", "model", "base_url", "api_key"):
        v = llm_cfg.get(k)
        if v:
            kwargs[k] = v

    resp = call_llm(**kwargs)
    return extract_content_or_reasoning(resp) or ""


def _parse_events_json(raw: str) -> list[dict]:
    """从 LLM 输出中提取 events 数组。容错：抽最后一个合法 {...}。"""
    if not raw:
        return []
    matches = list(_JSON_OBJ_RE.finditer(raw))
    for m in reversed(matches):
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            continue
        evs = obj.get("events") if isinstance(obj, dict) else None
        if isinstance(evs, list):
            return evs
    return []


def _llm_augment(
    anchors: list[dict],
    soul: str,
    today: str,
    date_human: str,
    llm_cfg: dict,
) -> int:
    """调 LLM 生成 N 条增补，写入 events.json。返回实际写入条数。"""
    count = int(llm_cfg.get("extra_count", 2))
    if count <= 0:
        return 0

    sys_prompt = _SYSTEM_PROMPT_TPL.format(
        soul=(soul[:2000] or "（未提供 SOUL.md）"),
        date_human=date_human,
        anchors_brief=_format_anchors_brief(anchors),
        count=count,
    )
    try:
        raw = _call_aux_llm(
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"请为 {date_human} 生成 {count} 条今日特色事件。"},
            ],
            llm_cfg,
        )
    except ImportError:
        logger.info("[daily_seed] agent.auxiliary_client 不可用，跳过 LLM 增补")
        return 0
    except Exception as e:
        logger.warning("[daily_seed] LLM 调用失败 %s: %s", type(e).__name__, e)
        return 0

    events = _parse_events_json(raw)
    if not events:
        logger.warning("[daily_seed] LLM 输出未解析出 events，原始=%r", raw[:200])
        return 0

    added = 0
    for ev in events[:count]:
        if not isinstance(ev, dict):
            continue
        start_iso = _to_today_iso(ev.get("start", ""), today)
        end_iso = _to_today_iso(ev.get("end", ""), today) or _default_end(start_iso)
        title = (ev.get("title") or "").strip()
        kind = (ev.get("kind") or _KIND_SELF).strip()
        if kind not in _VALID_KINDS:
            kind = _KIND_SELF
        if not (start_iso and title):
            continue
        try:
            add_event(start_iso, end_iso, title, kind=kind)
            added += 1
        except ValueError as e:
            logger.warning("[daily_seed] LLM 事件丢弃 (%s): %r", e, ev)
    logger.info("[daily_seed] LLM 增补 %d 条事件", added)
    return added


# ---------------- 主入口 ----------------

_WEEKDAYS_ZH = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def seed_today_if_empty(now: datetime | None = None) -> dict:
    """
    若今天 events.json.schedule 为空，写入固定锚点 + LLM 增补。

    返回 {"skipped": bool, "anchors_added": int, "llm_added": int}。
    任何阶段失败都不抛异常，最坏情况返回 anchors_added=0/llm_added=0。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    date_human = f"{today}（{_WEEKDAYS_ZH[now.weekday()]}）"
    result = {"skipped": False, "anchors_added": 0, "llm_added": 0}

    cfg = _load_config()
    if not _enabled_overall(cfg):
        result["skipped"] = True
        return result

    # 幂等：已经有日程就不动
    try:
        doc = list_today()
    except Exception as e:
        logger.warning("[daily_seed] 读 events.json 失败: %s", e)
        return result
    if doc.get("schedule"):
        result["skipped"] = True
        logger.info("[daily_seed] 今日 schedule 非空，跳过")
        return result

    # Step A — 固定锚点
    anchors = _resolve_anchors(cfg)
    for a in anchors:
        try:
            start_iso = _to_today_iso(a.get("start", ""), today)
            end_iso = _to_today_iso(a.get("end", ""), today) or _default_end(start_iso)
            title = (a.get("title") or "").strip()
            kind = (a.get("kind") or _KIND_SELF).strip()
            if kind not in _VALID_KINDS:
                kind = _KIND_SELF
            if not (start_iso and title):
                continue
            add_event(start_iso, end_iso, title, kind=kind)
            result["anchors_added"] += 1
        except ValueError as e:
            logger.warning("[daily_seed] 锚点丢弃 (%s): %r", e, a)

    # Step B — LLM 增补
    if _enabled_llm(cfg):
        soul = _resolve_soul(cfg)
        result["llm_added"] = _llm_augment(
            anchors, soul, today, date_human, cfg.get("llm", {}),
        )

    logger.info(
        "[daily_seed] seeded %s: anchors=%d llm=%d",
        today, result["anchors_added"], result["llm_added"],
    )
    return result
