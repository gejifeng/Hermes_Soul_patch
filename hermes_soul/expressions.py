"""Durable expression decisions. Delivery is opt-in and never blindly retried."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import re
import time
import uuid
import httpx

from .schedule import sleep_until
from .store import encode


def fingerprint(text):
    return hashlib.sha256(re.sub(r"\W+", "", text.lower()).encode()).hexdigest()


class Expressions:
    def __init__(self, store, config):
        self.s, self.cfg = store, config
        self.delivery = config.get("delivery", {})
        columns = {r[1] for r in self.s.db.execute("PRAGMA table_info(expression_meta)")}
        # Additive: a block is a short burst of messages sent minutes apart.
        for name, decl in (("origin", "TEXT NOT NULL DEFAULT 'reflection'"), ("block_id", "TEXT"), ("block_seq", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.s.db.execute(f"ALTER TABLE expression_meta ADD COLUMN {name} {decl}")

    def create(self, text, refs, revision, *, origin="reflection", block_id=None, seq=0):
        self.s.valid_refs(refs)
        text = text.strip()[:2000]
        if not text:
            return None
        # A repeated "在吗？" in a later burst is a new message, not a duplicate idea.
        for old in (self.s.rows("outbox", 500) if origin != "reach_out" else []):
            if fingerprint(old["content"]) == fingerprint(text):
                return old["id"]
        eid, now = uuid.uuid4().hex, time.time()
        self.s.db.execute("INSERT INTO outbox(id,content,refs,created,status,revision) VALUES(?,?,?,?,?,?)",
                          (eid, text, encode(refs), now, "draft", revision))
        self.s.db.execute("INSERT INTO expression_meta(id,updated,not_before,expires,reason,origin,block_id,block_seq) VALUES(?,?,?,?,?,?,?,?)",
                          (eid, now, now, now+7*86400, "刚产生的表达，尚待判断时机", origin, block_id or eid, seq))
        self.s.event("expression_created", {"id": eid, "refs": refs})
        return eid

    def rows(self):
        result = []
        for r in self.s.db.execute("SELECT o.*,m.updated,m.not_before,m.expires,m.reason,m.reviewed_revision,m.receipt,m.origin,coalesce(m.block_id,o.id) AS block_id,m.block_seq FROM outbox o JOIN expression_meta m ON o.id=m.id ORDER BY o.created DESC LIMIT 100"):
            item = dict(r);item["refs"] = json.loads(item["refs"]);item["receipt"] = json.loads(item["receipt"]) if item["receipt"] else None
            result.append(item)
        return result

    def invalidate(self):
        self.s.db.execute("UPDATE outbox SET status='stale' WHERE status IN ('draft','ready','waiting')")
        self.s.db.execute("UPDATE expression_meta SET reviewed_revision=NULL WHERE id IN (SELECT id FROM outbox WHERE status='stale')")

    def maintenance(self):
        self.s.db.execute("UPDATE outbox SET status='discarded' WHERE status IN ('draft','stale','waiting','ready') AND id IN (SELECT id FROM expression_meta WHERE expires<=?)", (time.time(),))

    def on_observation(self, oid):
        from .evidence import normalized
        from .memory import terms
        observation = normalized(self.s.db.execute("SELECT * FROM observations WHERE id=?",(oid,)).fetchone())
        q=set(terms(json.dumps(observation,ensure_ascii=False)))
        for row in self.rows():
            if row['status']=='waiting' and oid not in row['refs'] and len(q & set(terms(row['content']))) >= 3:
                seen_key='expression_evidence:'+row['id']
                seen=self.s.get(seen_key,[])
                if oid not in seen:
                    self.s.db.execute("UPDATE outbox SET status='stale' WHERE id=?",(row['id'],))
                    self.s.db.execute("UPDATE expression_meta SET reviewed_revision=NULL,not_before=? WHERE id=?",(time.time(),row['id']))
                    self.s.set(seen_key,(seen+[oid])[-30:])

    def due(self):
        now = time.time()
        return [r for r in self.rows() if r["status"] in {"draft", "stale", "waiting"} and r["not_before"] <= now and r["expires"] > now][:2]

    def decide(self, eid, action, reason, revision, delay=1800):
        # Look the row up directly; rows() is a recent window and would hide older drafts from management.
        row = self.s.db.execute("SELECT status FROM outbox WHERE id=?", (eid,)).fetchone()
        if not row or row["status"] in {"sent", "sending", "uncertain", "discarded", "failed"}:
            raise ValueError("表达不存在或已进入不可重复投递状态")
        if action not in {"ready", "wait", "discard"}:
            raise ValueError("无效表达决定")
        status = {"ready": "ready", "wait": "waiting", "discard": "discarded"}[action]
        now = time.time()
        self.s.db.execute("UPDATE outbox SET status=?,revision=? WHERE id=?", (status, revision, eid))
        self.s.db.execute("UPDATE expression_meta SET updated=?,not_before=?,reason=?,reviewed_revision=? WHERE id=?",
                          (now, now+max(self.cfg.get("expression_review_min_seconds",21600),min(86400,int(delay))) if action == "wait" else now, reason[:800], revision, eid))
        self.s.event("expression_decision", {"id": eid, "action": action, "reason": reason[:800]})

    def delivery_block(self, *, foreground=False):
        d = self.delivery
        if sleep_until(self.cfg):
            return "睡眠时段，暂停主动活动与消息"
        if d.get("mode", "draft") != "telegram" or d.get("authorized") is not True:
            return "草稿模式，未开启外发"
        if foreground or self.s.get("paused") or time.time() < self.s.get("foreground_until", 0):
            return "正在聊天或自主活动已暂停"
        if time.time()-self.s.get("last_user_at", 0) < 900:
            return "刚有交流，暂不追加主动消息"
        if self.s.db.execute("SELECT 1 FROM records WHERE kind='boundary' AND json_extract(payload,'$.action')='pause' AND json_extract(payload,'$.state')='active' AND json_extract(payload,'$.review_at')>? LIMIT 1", (time.time(),)).fetchone():
            return "交流暂停期间不主动追加消息"
        hour = datetime.now(ZoneInfo("Asia/Shanghai")).hour
        start, end = d.get("quiet_start", 22), d.get("quiet_end", 8)
        quiet = (start <= hour < end) if start < end else (hour >= start or hour < end)
        if start != end and quiet:
            return "免打扰时段"
        sent = [r for r in self.rows() if r["status"] in {"sent", "sending", "uncertain"}]
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        if sum(datetime.fromtimestamp(r["updated"], ZoneInfo("Asia/Shanghai")).date() == today for r in sent) >= d.get("max_per_day", 32):
            return "今日主动消息额度已用完"
        if str(d.get("chat_id", "")) not in {str(x) for x in self.cfg.get("bridge_owner_ids", [])} or not d.get("token_file"):
            return "尚未配置已授权的主人渠道"
        return None

    def next_ready(self, now=None):
        """Blocks are separated by min_interval_seconds; inside a block, block_gap_seconds suffices."""
        d, now = self.delivery, time.time() if now is None else now
        rows = self.rows()
        ready = sorted((r for r in rows if r["status"] == "ready" and r["reviewed_revision"] == self.s.get("revision")
                        and r["not_before"] <= now < r["expires"]), key=lambda r: (r["created"], r["block_seq"]))
        if not ready:
            return None
        sent = sorted((r for r in rows if r["status"] in {"sent", "sending", "uncertain"}), key=lambda r: r["updated"])
        if not sent:
            return ready[0]
        last = sent[-1]
        in_block = [r for r in sent if r["block_id"] == last["block_id"]]
        same = [r for r in ready if r["block_id"] == last["block_id"]]
        if same and len(in_block) < d.get("max_per_block", 4) and now-last["updated"] >= d.get("block_gap_seconds", 120):
            return same[0]
        if now-last["updated"] >= max(900, d.get("min_interval_seconds", 3600)):
            return next((r for r in ready if r["block_id"] != last["block_id"]), None)
        return None

    async def deliver_one(self, *, foreground=False):
        if self.delivery_block(foreground=foreground):
            return
        row = self.next_ready()
        if not row:
            return
        # Resolve credentials before reserving an attempt. Never log token-bearing URLs.
        token = Path(self.delivery["token_file"]).read_text().strip()
        if not token:
            return
        attempt = uuid.uuid4().hex
        with self.s.transaction():
            changed = self.s.db.execute("UPDATE outbox SET status='sending' WHERE id=? AND status='ready'", (row["id"],)).rowcount
            if not changed:
                return
            self.s.db.execute("UPDATE expression_meta SET attempt_id=?,updated=? WHERE id=?", (attempt, time.time(), row["id"]))
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.post("https://api.telegram.org/bot"+token+"/sendMessage",
                    json={"chat_id": str(self.delivery["chat_id"]), "text": row["content"]})
            data = response.json()
            if response.is_success and data.get("ok") and data.get("result", {}).get("message_id"):
                status, receipt = "sent", {"message_id": data["result"]["message_id"], "channel": "telegram", "read_status": "unknown"}
            elif data.get("ok") is False:
                status, receipt = "failed", {"code": data.get("error_code"), "reason": "渠道明确拒绝投递，不自动重试"}
            else:
                status, receipt = "uncertain", {"reason": "渠道响应无法确认，不自动重试"}
        except Exception as exc:
            status, receipt = "uncertain", {"reason": "请求结果无法确认，不自动重试", "exception_type": type(exc).__name__}
        with self.s.transaction():
            self.s.db.execute("UPDATE outbox SET status=? WHERE id=?", (status, row["id"]))
            self.s.db.execute("UPDATE expression_meta SET receipt=?,updated=? WHERE id=?", (encode(receipt), time.time(), row["id"]))
            self.s.event("expression_delivery", {"id": row["id"], "status": status, "receipt": receipt})
