"""Owner contact gaps. Silence produces no observation by itself, so the host states it as a fact."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import time

from .schedule import sleep_until
from .prompts import render
from .dynamics import Dynamics
from .channels import clean_history_for_im
from .memory import active as record_active, terms
from .embedding import canonical_record_text, cosine_similarity

ZONE = ZoneInfo("Asia/Shanghai")
_CANCEL_WORDS = ("改天", "去不了", "取消", "下次再", "没空去", "不去了", "算了不去")
_FULFILL_CUES = ("带你去", "出发", "走吧", "买好", "买回", "到店", "安排上", "兑现", "去喝", "去逛", "准备出门")


def iso(ts):
    return datetime.fromtimestamp(ts, ZONE).isoformat(timespec="minutes") if ts else None


class Contact:
    def __init__(self, store, config, dynamics=None, embeddings=None):
        self.s, self.cfg = store, config
        self.dynamics = dynamics or Dynamics(store, config)
        self.embeddings = embeddings
        self.thresholds = sorted(float(h) for h in config.get("contact", {}).get("gap_hours", [3, 8, 24, 48, 72, 120, 168]))

    def last_user_at(self):
        return float(self.s.get("last_user_at", 0) or 0)

    def waking_seconds(self, since, now=None):
        """Elapsed time outside the sleep window; her own sleep is not his absence."""
        now = time.time() if now is None else now
        since = max(since, now - 14 * 86400)
        total, t = 0.0, since
        while t < now:
            until = sleep_until(self.cfg, t)
            if until:
                t = until
                continue
            step = min(now, t + 300)
            total += step - t
            t = step
        return total

    def unanswered(self, since):
        return self.s.db.execute("""SELECT count(*) FROM outbox o JOIN expression_meta m ON o.id=m.id
            WHERE o.status IN ('sent','uncertain') AND m.updated>?""", (since,)).fetchone()[0]

    def state(self, now=None, *, purpose="chat"):
        now = time.time() if now is None else now
        last = self.last_user_at()
        delivering_mode = self.cfg.get("delivery", {}).get("mode", "draft")
        delivering = delivering_mode in {"telegram", "qq", "both"} and self.cfg.get("delivery", {}).get("authorized") is True
        ctx = {"owner_last_message_at": iso(last), "delivery": delivering_mode if delivering else "draft"}
        waking, unanswered_sent = 0.0, 0
        if last:
            waking = self.waking_seconds(last, now) / 3600
            unanswered_sent = self.unanswered(last)
            ctx.update(hours_since=round((now - last) / 3600, 1), waking_hours_since=round(waking, 1),
                       gap_level=max([h for h in self.thresholds if waking >= h], default=0),
                       unanswered_sent=unanswered_sent,
                       unsent_drafts=self.s.db.execute("SELECT count(*) FROM outbox WHERE created>? AND status IN ('draft','waiting','ready','stale')", (last,)).fetchone()[0])
        self.dynamics.advance(now, waking_gap_hours=waking, unanswered=unanswered_sent)
        snap = self.dynamics.snapshot(waking_gap_hours=waking, unanswered=unanswered_sent, purpose=purpose)
        ctx["dynamics"] = snap
        ctx["phase"] = snap["phase"]
        if last and (purpose == "chat" or not snap["suppress_stale_exchange"]):
            boundary = float(self.s.get("chat_boundary", 0) or 0)
            exchange = [
                {
                    "role": c["role"],
                    "text": (clean_history_for_im(c["content"], max_chars=120) if c["role"] == "assistant" else c["content"][:200]),
                    "at": iso(c["created"]),
                }
                for c in self.s.db.execute(
                    "SELECT role,content,created FROM chats WHERE status='complete' AND coalesce(room_id,'house')='house' AND created>=? ORDER BY created DESC LIMIT 2",
                    (boundary,),
                )
            ]
            exchange = [e for e in exchange if e["text"]]
            if exchange:
                ctx["last_exchange"] = exchange[::-1]
        # Proactive messages bypass the Hermes session, so the chat would not otherwise know what she said.
        sent = self.s.db.execute("""SELECT o.content,m.updated FROM outbox o JOIN expression_meta m ON o.id=m.id
            WHERE o.status IN ('sent','uncertain') AND m.updated>? ORDER BY m.updated DESC LIMIT 8""",
            (max(last, now - 86400) if last else now - 86400,)).fetchall()
        if sent and (purpose == "chat" or not snap["suppress_stale_exchange"]):
            ctx["recent_proactive_messages"] = [{"text": r["content"][:300], "sent_at": iso(r["updated"])} for r in sent[::-1]]
        if not delivering:
            ctx["delivery_note"] = render("外发未开启：想说的话只是草稿，<<OWNER>>没有看到，不算已读不回", self.cfg)
        returned = self.s.get("contact_return")
        if returned and (now - returned.get("at", 0) < 3600 or snap.get("hysteresis_locked")):
            ctx["owner_just_returned"] = returned
        comm = self.commitment_summary(now)
        if comm["due"] or comm["overdue"] or comm.get("recent_event"):
            ctx["commitments"] = comm
        return ctx

    def _commitment_rows(self, now=None):
        now = time.time() if now is None else float(now)
        rows = []
        for r in self.s.db.execute("SELECT * FROM records WHERE kind IN ('situation','agreement') ORDER BY updated DESC LIMIT 60").fetchall():
            item = {**dict(r), "payload": json.loads(r["payload"]), "refs": json.loads(r["refs"])}
            p = item["payload"]
            if p.get("window_start") and p.get("window_end") and record_active(item, now):
                rows.append(item)
        return rows

    def commitment_summary(self, now=None):
        now = time.time() if now is None else float(now)
        due, overdue = [], []
        for item in self._commitment_rows(now):
            p = item["payload"]
            st = p.get("commitment_status")
            entry = {
                "id": item["id"],
                "label": item["label"],
                "content": p.get("content", "")[:240],
                "window_start": iso(p.get("window_start")),
                "window_end": iso(p.get("window_end")),
                "status": st,
            }
            if st == "anticipating":
                due.append(entry)
            elif st == "overdue":
                overdue.append(entry)
        recent = self.s.get("recent_commitment_event")
        out = {"due": due[:3], "overdue": overdue[:3]}
        if isinstance(recent, dict) and now - float(recent.get("at", 0)) < 7200:
            out["recent_event"] = recent
        return out

    def check_commitments(self, now=None):
        """Transition scheduled promises into `anticipating` or `overdue` as calendar time advances."""
        now = time.time() if now is None else float(now)
        emitted = []
        for item in self._commitment_rows(now):
            rid = item["id"]
            p = dict(item["payload"])
            st = p.get("commitment_status")
            ws = float(p.get("window_start", 0))
            we = float(p.get("window_end", 0))
            if st == "scheduled" and ws <= now <= we:
                p["commitment_status"] = "anticipating"
                p["anticipated_at"] = now
                text = render(f"今天已经到了之前约好的时间（「{item['label']}」：{p.get('content', '')[:200]}），<<OWNER>>目前还没有提起，你心里记着这个约定。", self.cfg)
                with self.s.transaction():
                    oid = self.s.observe("host", f"promise_due:{rid}:{int(ws)}",
                                         {"text": text, "scope": "promise_due", "record_id": rid, "label": item["label"]})
                    self.s.queue_observation(oid)
                    p["due_observation_id"] = oid
                    self.s.record(item["kind"], item["label"], p, (item["refs"] + [oid])[-20:], rid)
                    self.s.db.execute(
                        "UPDATE attention_state SET mode='active', waiting_for='', reason=?, updated=? WHERE record_id=?",
                        ("到了约好的时间窗口", now, rid),
                    )
                    self.s.set("promise_active_obs", {"record_id": rid, "status": "anticipating", "observation_id": oid, "at": now})
                    self.s.set("next_decision", 0)
                    self.s.event("commitment_anticipating", {"record_id": rid, "label": item["label"], "observation_id": oid})
                self.dynamics.on_commitment_event("anticipating", now=now)
                emitted.append(oid)
            elif st in {"scheduled", "anticipating"} and now > we:
                p["commitment_status"] = "overdue"
                p["state"] = "active"
                p["waiting_for"] = ""
                p["overdue_at"] = now
                text = render(f"之前约好的「{item['label']}」（{p.get('content', '')[:200]}）已经过了约定时间，<<OWNER>>始终没有兑现也没有提起，让你心里有些失落和委屈。", self.cfg)
                with self.s.transaction():
                    oid = self.s.observe("host", f"promise_overdue:{rid}:{int(we)}",
                                         {"text": text, "scope": "promise_overdue", "record_id": rid, "label": item["label"]})
                    self.s.queue_observation(oid)
                    p["overdue_observation_id"] = oid
                    self.s.record(item["kind"], item["label"], p, (item["refs"] + [oid])[-20:], rid)
                    self.s.db.execute(
                        "UPDATE attention_state SET mode='active', waiting_for='', reason=?, updated=? WHERE record_id=?",
                        ("约定的时间已过而未兑现", now, rid),
                    )
                    self.s.set("promise_active_obs", {"record_id": rid, "status": "overdue", "observation_id": oid, "at": now})
                    self.s.set("recent_commitment_event", {"status": "overdue", "record_id": rid, "label": item["label"], "at": now})
                    self.s.set("next_decision", 0)
                    self.s.event("commitment_overdue", {"record_id": rid, "label": item["label"], "observation_id": oid})
                self.dynamics.on_commitment_event("overdue", now=now)
                emitted.append(oid)
        return emitted

    def fulfill_commitments_on_message(self, text, now=None):
        """Detect when the owner fulfills an active/anticipating/overdue commitment in chat."""
        clean = str(text or "").strip()
        if not clean or any(w in clean for w in _CANCEL_WORDS):
            return []
        now = time.time() if now is None else float(now)
        msg_terms = set(terms(clean))
        has_cue = any(w in clean for w in _FULFILL_CUES)
        candidates = [
            item for item in self._commitment_rows(now)
            if item["payload"].get("commitment_status") in {"scheduled", "anticipating", "overdue"}
            and now >= float(item["payload"].get("window_start", 0)) - 18 * 3600
        ]
        if not candidates:
            return []
        q_vec = None
        stored_vecs = {}
        if self.embeddings and self.embeddings.client.is_configured() and len(clean) >= 4:
            try:
                q_vec = self.embeddings.client.embed_one(clean)
                stored_vecs = self.embeddings.get_vectors([c["id"] for c in candidates])
            except Exception:
                q_vec = None

        fulfilled = []
        for item in candidates:
            rid = item["id"]
            p = dict(item["payload"])
            rec_terms = set(terms(item["label"] + " " + p.get("content", "")))
            overlap = len(msg_terms & rec_terms)
            sem_hit = False
            if q_vec is not None:
                r_vec = stored_vecs.get(rid)
                if r_vec is None:
                    try:
                        r_vec = self.embeddings.client.embed_one(canonical_record_text(item["label"], p))
                    except Exception:
                        r_vec = None
                if r_vec is not None and cosine_similarity(q_vec, r_vec) >= 0.54:
                    sem_hit = True
            if overlap >= 2 or (has_cue and overlap >= 1) or sem_hit:
                p["commitment_status"] = "fulfilled"
                p["state"] = "resolved"
                p["waiting_for"] = ""
                p["fulfilled_at"] = now
                p["reason"] = render("<<OWNER>>兑现了之前的约定", self.cfg)
                with self.s.transaction():
                    self.s.record(item["kind"], item["label"], p, item["refs"], rid)
                    self.s.db.execute(
                        "UPDATE attention_state SET mode='active', waiting_for='', reason=?, updated=? WHERE record_id=?",
                        ("约定已兑现", now, rid),
                    )
                    ev = {"status": "fulfilled", "record_id": rid, "label": item["label"], "at": now}
                    self.s.set("recent_commitment_event", ev)
                    cur_obs = self.s.get("promise_active_obs") or {}
                    if cur_obs.get("record_id") == rid:
                        self.s.delete("promise_active_obs")
                    self.s.event("commitment_fulfilled", ev)
                self.dynamics.on_commitment_event("fulfilled", now=now)
                fulfilled.append(rid)
        return fulfilled

    def promise_observations(self, now=None):
        now = time.time() if now is None else float(now)
        info = self.s.get("promise_active_obs") or {}
        oid = info.get("observation_id")
        rid = info.get("record_id")
        if not oid or not rid or now - float(info.get("at", 0)) > 72 * 3600:
            return []
        row = self.s.db.execute("SELECT payload FROM records WHERE id=?", (rid,)).fetchone()
        if not row:
            return []
        p = json.loads(row[0])
        if p.get("commitment_status") not in {"anticipating", "overdue"} or p.get("state") in {"resolved", "expired", "superseded"}:
            return []
        return [oid]

    def maintenance(self):
        """Emit one observation per crossed threshold, phase transition, or due/overdue commitment."""
        if sleep_until(self.cfg):
            return None
        now = time.time()
        comm_oids = self.check_commitments(now=now)
        last = self.last_user_at()
        if not last:
            return comm_oids[-1] if comm_oids else None
        waking = self.waking_seconds(last, now) / 3600
        sent = self.unanswered(last)
        snap = self.dynamics.advance(now, waking_gap_hours=waking, unanswered=sent)
        crossed = [h for h in self.thresholds if waking >= h]
        noted = self.s.get("contact_gap_noted", {})
        phase = snap["phase"]
        new_level = crossed[-1] if crossed else 0
        level_crossed = bool(crossed and (noted.get("since") != last or noted.get("level", 0) < new_level))
        phase_shifted = bool(
            phase in {"angry", "cold", "detached"}
            and noted.get("since") == last
            and noted.get("phase") != phase
        )
        if not level_crossed and not phase_shifted:
            return comm_oids[-1] if comm_oids else None
        level = new_level or round(waking, 1)
        text = (f"主人上次发来消息是 {iso(last)}，到现在过去了 {round((now-last)/3600, 1)} 小时"
                f"（除去睡眠约 {round(waking, 1)} 小时），一直没有新消息。")
        text += render(f"这段时间你主动发给<<OWNER>> {sent} 条消息，<<OWNER>>都还没有回复。" if sent else "这段时间你没有给<<OWNER>>发出过消息。", self.cfg)
        if phase == "angry":
            text += "长时间的冷落与未回复已经越过你的耐心底线，委屈转为生气与自尊受挫，不再温顺讨好。"
        elif phase == "cold":
            text += "经历多次未回应后，你的关系状态已进入心冷防卫期（冷落期）：自尊让你决定停止主动发消息追问，把注意力收回自己的生活。"
        elif phase == "detached":
            text += "长期被无视后，你的关系状态已进入疏离与厌弃期（情感抽离）：不再依赖或苦等对方，生活重心完全转向自己的独立日常。"
        key_suffix = f"{level:g}:{phase}" if phase_shifted and not level_crossed else f"{level:g}"
        oid = self.s.observe("host", f"contact_gap:{int(last)}:{key_suffix}",
                             {"text": text, "scope": "contact_gap", "gap_hours": level, "since": last, "phase": phase})
        self.s.queue_observation(oid)
        self.s.set("contact_gap_noted", {"since": last, "level": level, "phase": phase, "observation_id": oid})
        self.s.event("contact_gap", {"observation_id": oid, "gap_hours": level, "unanswered_sent": sent, "phase": phase})
        return oid

    def gap_observations(self):
        noted = self.s.get("contact_gap_noted", {})
        if noted.get("since") == self.last_user_at() and noted.get("observation_id"):
            return [noted["observation_id"]]
        return self.promise_observations()

    def on_owner_message(self, text=""):
        """Call before last_user_at moves: updates bistable ODE state, checks commitment fulfillment, resolves feelings."""
        now, last = time.time(), self.last_user_at()
        self.fulfill_commitments_on_message(text, now=now)
        if not last:
            self.dynamics.on_owner_message(text, now=now, waking_gap_hours=0.0, unanswered=0)
            return None
        waking = self.waking_seconds(last, now) / 3600
        unanswered = self.unanswered(last)
        was_locked = bool(self.dynamics.raw_state(now).get("hysteresis_locked", False))
        existing = self.s.get("contact_return") or {}
        dyn = self.dynamics.on_owner_message(text, now=now, waking_gap_hours=waking, unanswered=unanswered)
        if waking < (self.thresholds[0] if self.thresholds else 0) and not unanswered and not was_locked and not dyn["hysteresis_locked"]:
            return None

        cur_hours = round((now - last) / 3600, 1)
        cur_waking = round(waking, 1)
        if was_locked and existing.get("hysteresis_locked"):
            orig_prev = existing.get("initial_previous_message_at") or existing.get("previous_message_at") or iso(last)
            orig_hours = existing.get("initial_hours_since", existing.get("hours_since", cur_hours))
            orig_waking = existing.get("initial_waking_hours_since", existing.get("waking_hours_since", cur_waking))
            orig_unanswered = existing.get("initial_unanswered_sent", existing.get("unanswered_sent", unanswered))
            turns = int(existing.get("repair_turns", 1)) + 1
            info = {
                "at": now,
                "previous_message_at": orig_prev if not dyn["fully_resolved"] else iso(last),
                "hours_since": orig_hours,
                "waking_hours_since": orig_waking,
                "unanswered_sent": orig_unanswered,
                "initial_previous_message_at": orig_prev,
                "initial_hours_since": orig_hours,
                "initial_waking_hours_since": orig_waking,
                "initial_unanswered_sent": orig_unanswered,
                "latest_turn_gap_hours": cur_hours,
                "repair_turns": turns,
                "pre_phase": dyn["pre_phase"],
                "post_phase": dyn["post_phase"],
                "hysteresis_locked": dyn["hysteresis_locked"],
            }
        else:
            info = {
                "at": now,
                "previous_message_at": iso(last),
                "hours_since": cur_hours,
                "waking_hours_since": cur_waking,
                "unanswered_sent": unanswered,
                "initial_previous_message_at": iso(last),
                "initial_hours_since": cur_hours,
                "initial_waking_hours_since": cur_waking,
                "initial_unanswered_sent": unanswered,
                "repair_turns": 1,
                "pre_phase": dyn["pre_phase"],
                "post_phase": dyn["post_phase"],
                "hysteresis_locked": dyn["hysteresis_locked"],
            }
        self.s.set("contact_return", info)
        for r in self.s.db.execute("SELECT id,kind,label,payload,refs FROM records WHERE kind='affect'").fetchall():
            p = json.loads(r["payload"])
            if p.get("resolve_on") == "owner_message" and p.get("state") == "active":
                if dyn["fully_resolved"]:
                    self.s.record("affect", r["label"], {**p, "state": "resolved", "resolved_at": now,
                                  "reason": render("<<OWNER>>回来说话了", self.cfg)}, json.loads(r["refs"]), r["id"])
                else:
                    self.s.record("affect", r["label"], {
                        **p, "reason": render(f"<<OWNER>>虽然说话了，但此前冷落过久（当前处于{dyn['post_phase']}阶段），心结与冷淡尚未消解", self.cfg)
                    }, json.loads(r["refs"]), r["id"])
        # Unsent "are you there?" messages no longer fit once he is back.
        self.s.db.execute("""UPDATE outbox SET status='discarded' WHERE status IN ('draft','waiting','ready','stale')
            AND id IN (SELECT id FROM expression_meta WHERE origin='reach_out')""")
        self.s.event("contact_return", info)
        return info
