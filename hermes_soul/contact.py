"""Owner contact gaps. Silence produces no observation by itself, so the host states it as a fact."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import time

from .schedule import sleep_until
from .prompts import render

ZONE = ZoneInfo("Asia/Shanghai")


def iso(ts):
    return datetime.fromtimestamp(ts, ZONE).isoformat(timespec="minutes") if ts else None


class Contact:
    def __init__(self, store, config):
        self.s, self.cfg = store, config
        self.thresholds = sorted(float(h) for h in config.get("contact", {}).get("gap_hours", [3, 8, 24, 48, 72]))

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

    def state(self, now=None):
        now = time.time() if now is None else now
        last = self.last_user_at()
        delivering = self.cfg.get("delivery", {}).get("mode", "draft") == "telegram" and self.cfg.get("delivery", {}).get("authorized") is True
        ctx = {"owner_last_message_at": iso(last), "delivery": "telegram" if delivering else "draft"}
        if last:
            waking = self.waking_seconds(last, now) / 3600
            ctx.update(hours_since=round((now - last) / 3600, 1), waking_hours_since=round(waking, 1),
                       gap_level=max([h for h in self.thresholds if waking >= h], default=0),
                       unanswered_sent=self.unanswered(last),
                       unsent_drafts=self.s.db.execute("SELECT count(*) FROM outbox WHERE created>? AND status IN ('draft','waiting','ready','stale')", (last,)).fetchone()[0])
            exchange = [{"role": c["role"], "text": c["content"][:200], "at": iso(c["created"])}
                        for c in self.s.db.execute("SELECT role,content,created FROM chats WHERE status='complete' ORDER BY created DESC LIMIT 2")]
            if exchange:
                ctx["last_exchange"] = exchange[::-1]
        # Proactive messages bypass the Hermes session, so the chat would not otherwise know what she said.
        sent = self.s.db.execute("""SELECT o.content,m.updated FROM outbox o JOIN expression_meta m ON o.id=m.id
            WHERE o.status IN ('sent','uncertain') AND m.updated>? ORDER BY m.updated DESC LIMIT 8""",
            (max(last, now - 86400) if last else now - 86400,)).fetchall()
        if sent:
            ctx["recent_proactive_messages"] = [{"text": r["content"][:300], "sent_at": iso(r["updated"])} for r in sent[::-1]]
        if not delivering:
            ctx["delivery_note"] = render("外发未开启：想说的话只是草稿，<<OWNER>>没有看到，不算已读不回", self.cfg)
        returned = self.s.get("contact_return")
        if returned and now - returned.get("at", 0) < 3600:
            ctx["owner_just_returned"] = returned
        return ctx

    def maintenance(self):
        """Emit one observation per crossed threshold; returns the new observation id, if any."""
        last = self.last_user_at()
        if not last or sleep_until(self.cfg):
            return None
        waking = self.waking_seconds(last) / 3600
        crossed = [h for h in self.thresholds if waking >= h]
        noted = self.s.get("contact_gap_noted", {})
        if not crossed or noted.get("since") == last and noted.get("level", 0) >= crossed[-1]:
            return None
        level = crossed[-1]
        sent = self.unanswered(last)
        text = (f"主人上次发来消息是 {iso(last)}，到现在过去了 {round((time.time()-last)/3600, 1)} 小时"
                f"（除去睡眠约 {round(waking, 1)} 小时），一直没有新消息。")
        text += render(f"这段时间你主动发给<<OWNER>> {sent} 条消息，<<OWNER>>都还没有回复。" if sent else "这段时间你没有给<<OWNER>>发出过消息。", self.cfg)
        oid = self.s.observe("host", f"contact_gap:{int(last)}:{level:g}",
                             {"text": text, "scope": "contact_gap", "gap_hours": level, "since": last})
        self.s.queue_observation(oid)
        self.s.set("contact_gap_noted", {"since": last, "level": level, "observation_id": oid})
        self.s.event("contact_gap", {"observation_id": oid, "gap_hours": level, "unanswered_sent": sent})
        return oid

    def gap_observations(self):
        noted = self.s.get("contact_gap_noted", {})
        if noted.get("since") != self.last_user_at() or not noted.get("observation_id"):
            return []
        return [noted["observation_id"]]

    def on_owner_message(self):
        """Call before last_user_at moves: remembers the gap he is returning from and ends waiting feelings."""
        now, last = time.time(), self.last_user_at()
        if not last:
            return None
        waking = self.waking_seconds(last, now) / 3600
        unanswered = self.unanswered(last)
        if waking < (self.thresholds[0] if self.thresholds else 0) and not unanswered:
            return None
        info = {"at": now, "previous_message_at": iso(last), "hours_since": round((now-last)/3600, 1),
                "waking_hours_since": round(waking, 1), "unanswered_sent": unanswered}
        self.s.set("contact_return", info)
        for r in self.s.db.execute("SELECT id,kind,label,payload,refs FROM records WHERE kind='affect'").fetchall():
            p = json.loads(r["payload"])
            if p.get("resolve_on") == "owner_message" and p.get("state") == "active":
                self.s.record("affect", r["label"], {**p, "state": "resolved", "resolved_at": now,
                              "reason": render("<<OWNER>>回来说话了", self.cfg)}, json.loads(r["refs"]), r["id"])
        # Unsent "are you there?" messages no longer fit once he is back.
        self.s.db.execute("""UPDATE outbox SET status='discarded' WHERE status IN ('draft','waiting','ready','stale')
            AND id IN (SELECT id FROM expression_meta WHERE origin='reach_out')""")
        self.s.event("contact_return", info)
        return info
