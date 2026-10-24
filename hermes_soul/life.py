"""Validated personal state; models propose, the host owns transitions and evidence."""
import json
import time
import uuid

from .memory import active, rebuild_missing
from .evidence import normalized

KINDS = {"interest", "belief", "relationship", "intention", "task", "situation", "boundary", "agreement", "affect"}
SLOTS = {"attention", "open_question", "continuation", "parked", "unsaid", "relationship"}


class Life:
    def __init__(self, store):
        self.s = store
        rebuild_missing(store)

    def get_record(self, rid):
        r = self.s.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        return {**dict(r), "payload": json.loads(r["payload"]), "refs": json.loads(r["refs"])} if r else None

    def prepare(self, update, allowed_refs, aid):
        u = dict(update)
        if u.get("kind") not in KINDS or not isinstance(u.get("label"), str) or not u["label"].strip():
            raise ValueError("无效的记忆类型或主题")
        if not isinstance(u.get("content"), str):
            raise ValueError("记忆内容必须是文字")
        self.s.valid_refs(u.get("refs"))
        if not set(u["refs"]).issubset(allowed_refs):
            raise ValueError("不能借用本轮未提供的观察")
        prior = self.get_record(u["id"]) if u.get("id") else None
        if u.get("id") and (not prior or prior["kind"] != u["kind"]):
            raise ValueError("修改的记录不存在或类型不符")
        if not prior:
            row = self.s.db.execute("SELECT id FROM records WHERE kind=? AND lower(trim(label))=? ORDER BY updated DESC LIMIT 1",
                                    (u["kind"], u["label"].strip().lower())).fetchone()
            prior = self.get_record(row[0]) if row else None
        previous = prior["payload"] if prior else {}
        combined = list(dict.fromkeys((prior["refs"] if prior else []) + u["refs"]))[-20:]
        p = {"content": u["content"][:2400], "state": u.get("state", "active"),
             "epistemic_status": "interpretation", "activity_id": aid,
             "reason": str(u.get("reason", ""))[:500], "uncertainty": str(u.get("uncertainty", "未明确"))[:400]}
        if u["kind"] == "situation":
            slot = u.get("slot", "attention")
            if slot not in SLOTS:
                raise ValueError("无效近况栏目")
            ttl = max(1, min(float(u.get("ttl_hours", 48)), 336))
            p.update(slot=slot, expires_at=time.time()+ttl*3600)
        if u["kind"] in {"situation", "intention", "task"}:
            wait = u.get("waiting_for", "")
            if wait not in {"", "user_reply", "new_evidence", "time", "capability", "operator"}:
                raise ValueError("无效等待条件")
            p.update(waiting_for=wait, required_capability=str(u.get("required_capability", ""))[:80])
            if wait:
                p["state"] = "waiting"
            if u.get("reconsider_at"):
                p["reconsider_at"] = max(time.time()+60, min(float(u["reconsider_at"]),time.time()+30*86400))
        if u["kind"] == "affect":
            quote = str(u.get("trigger_quote", "")).strip()
            scope = str(u.get("scope", "")).strip()
            fresh = [self.s.db.execute("SELECT * FROM observations WHERE id=?", (r,)).fetchone() for r in u["refs"] if r not in (prior["refs"] if prior else [])]
            if not quote or not scope or not any(quote in json.dumps(normalized(r),ensure_ascii=False) for r in fresh):
                raise ValueError("暂态感受需要新观察中的实际触发原文和影响范围")
            if len(quote) < 4 or any(w in quote for w in ("observed_at", "observation_id")):
                raise ValueError("触发引用应为观察内容")
            p.update(scope=scope[:200],trigger_quote=quote[:400],state="active",
                     expires_at=time.time()+min(24,max(0.25,float(u.get("ttl_hours",3))))*3600)
            if any(r["source"] == "host" and json.loads(r["payload"]).get("scope") == "contact_gap" for r in fresh if r):
                # Missing him lasts until he is back, with a week as the outer bound.
                p.update(resolve_on="owner_message", expires_at=time.time()+7*86400)
        if u["kind"] == "interest":
            observations = [self.s.db.execute("SELECT id,source,external_id,created,json_extract(payload,'$.tool') AS tool FROM observations WHERE id=?", (r,)).fetchone() for r in combined]
            external = [r for r in observations if r["source"] == "xiaohongshu"]
            distinct = {(r["source"], r["external_id"]) for r in external if r["tool"] == "get_feed_detail"}
            prior_external = {self.s.db.execute("SELECT external_id FROM observations WHERE id=?", (ref,)).fetchone()[0] for ref in (prior["refs"] if prior else [])}
            new_external = any(r["tool"] == "get_feed_detail" and r["id"] in u["refs"] and r["external_id"] not in prior_external for r in external)
            visits = list(dict.fromkeys(previous.get("visit_activities", []) + ([aid] if new_external else [])))[-30:]
            requested = p["state"]
            state = "noticed"
            if len(distinct) >= 2 and len(visits) >= 2:
                state = "exploring"
            engagement = list(previous.get("engagement", []))
            if new_external and u.get("attraction") and u.get("open_question"):
                engagement.append({"activity_id":aid,"attraction":str(u["attraction"])[:240],"open_question":str(u["open_question"])[:240]})
            if len(engagement) >= 2 and len(distinct) >= 3 and len(visits) >= 3 and max(r["created"] for r in external)-min(r["created"] for r in external) >= 21600:
                state = "preferred"
            if requested == "dormant":
                state = "dormant"
            elif previous.get("state") == "dormant" and new_external:
                state = "rekindled"
            elif requested == "noticed":
                state = "noticed"
            if not external:
                state = "suggested"
            if prior and not new_external:
                state = previous.get("state",state)
            if u.get("cooling_reason") and new_external:
                state = "dormant"
                p["cooling_reason"] = str(u["cooling_reason"])[:300]
            parent = u.get("parent_interest_id")
            if parent and (not self.get_record(parent) or self.get_record(parent)["kind"] != "interest"):
                raise ValueError("兴趣分支的父兴趣不存在")
            p.update(engagement=engagement[-10:], attraction=str(u.get("attraction",previous.get("attraction","")))[:240],
                     open_question=str(u.get("open_question",previous.get("open_question","")))[:240],
                     parent_interest_id=parent or previous.get("parent_interest_id"),
                     state=state, visit_activities=visits, evidence_count=len(distinct),
                     last_engaged=time.time() if new_external else previous.get("last_engaged", time.time()))
        if u["kind"] in {"relationship", "boundary", "agreement"}:
            if not any(self.s.db.execute("SELECT source FROM observations WHERE id=?", (r,)).fetchone()[0] == "user" for r in u["refs"]):
                raise ValueError("关系判断必须引用真实用户交流，不能从论坛帖子推断")
            p["observed_event"] = str(u.get("observed_event", ""))[:1000]
        if u["kind"] == "agreement":
            quote = str(u.get("user_quote", "")).strip()
            if not quote or not any(quote in json.loads(self.s.db.execute("SELECT payload FROM observations WHERE id=? AND source='user'", (r,)).fetchone()[0]).get("text", "")
                                     for r in u["refs"] if self.s.db.execute("SELECT 1 FROM observations WHERE id=? AND source='user'", (r,)).fetchone()):
                raise ValueError("双方约定需要用户实际原话，不可单方面推定")
            p.update(user_quote=quote[:1500], epistemic_status="user_confirmed_statement")
        if u["kind"] == "boundary":
            action = u.get("action", "clarify")
            if action not in {"disagree", "clarify", "refuse", "pause", "repair"}:
                raise ValueError("不支持拉黑或永久中断")
            p.update(action=action, scope="topic", state="active", notice_at=previous.get("notice_at"),
                     notice_text=previous.get("notice_text", ""))
            # Gateway responses can acknowledge an already expressed topic boundary.
            quote = str(u.get("notice_quote", "")).strip()
            if action == "refuse" and len(quote) >= 10:
                row = self.s.db.execute("SELECT created FROM chats WHERE role='assistant' AND status='complete' AND instr(content,?)>0 ORDER BY created DESC LIMIT 1", (quote,)).fetchone()
                if row:
                    p.update(notice_text=quote[:1500], notice_at=row[0], notice_status="response_generated")
            if action == "pause":
                new_user = [self.s.db.execute("SELECT created FROM observations WHERE id=? AND source='user'", (r,)).fetchone() for r in u["refs"] if not prior or r not in prior["refs"]]
                if not prior or previous.get("action") != "refuse" or not previous.get("notice_at") or not any(r and r[0] > previous["notice_at"] for r in new_user):
                    p.update(action="clarify", reason="尚缺先前明确表达边界后再次发生的交流依据")
                else:
                    p.update(scope=u.get("scope", "topic") if u.get("scope") in {"topic", "conversation"} else "topic",
                             review_at=time.time()+min(1800, max(60, int(u.get("pause_seconds", 1200)))))
            if action == "repair":
                p["state"] = "resolved"
            # Initial uncertainty cannot silently turn into a refusal.
            if p["action"] == "refuse" and u.get("uncertainty") != "low":
                p["action"] = "clarify"
        if u["kind"] in {"belief", "relationship", "intention", "task", "situation", "agreement"} and p["state"] not in {
                "active", "dormant", "proposed", "ready", "waiting", "completed", "resolved", "archived"}:
            raise ValueError("无效记录状态")
        evidence = [normalized(self.s.db.execute("SELECT * FROM observations WHERE id=?", (ref,)).fetchone()) for ref in u["refs"]]
        p["evidence_class"] = "user_statement" if all(e["source"] == "user" for e in evidence) else "source_claim"
        if prior and not set(u["refs"])-set(prior["refs"]):
            p["uncertainty"] = previous.get("uncertainty", "high")
        if u.get("time_sensitive") or any(w in u["label"]+u["content"] for w in ("天气", "预报", "降雨", "台风")):
            p.update(time_sensitive=True, verification="unverified", uncertainty="high",
                     applicable_period=str(u.get("applicable_period", "待核对来源日期"))[:200])
        return u["kind"], u["label"], p, combined, prior["id"] if prior else None

    def save(self, prepared):
        kind, label, payload, refs, rid = prepared
        if kind == "situation":
            for r in self.current_situations():
                if r["id"] != rid and r["payload"].get("slot") == payload["slot"]:
                    self.s.record(r["kind"], r["label"], {**r["payload"], "state": "superseded"}, r["refs"], r["id"])
        return self.s.record(kind, label, payload, refs, rid)

    def current_situations(self):
        return [r for r in self.kind_rows("situation") if active(r)]

    def boundaries(self):
        return [r for r in self.kind_rows("boundary") if active(r)]

    def kind_rows(self, kind):
        return [{**dict(r), "payload": json.loads(r["payload"]), "refs": json.loads(r["refs"])}
                for r in self.s.db.execute("SELECT * FROM records WHERE kind=? ORDER BY updated DESC", (kind,))]

    def maintenance(self):
        now = time.time()
        for r in self.kind_rows("situation") + self.kind_rows("interest") + self.kind_rows("boundary") + self.kind_rows("affect"):
            p = r["payload"]; change = None
            if r["kind"] in {"situation", "affect"} and p.get("state") not in {"expired", "resolved", "archived", "superseded"} and p.get("expires_at", now+1) <= now:
                change = {**p, "state": "expired"}
            if r["kind"] == "interest" and p.get("last_engaged") and now-p["last_engaged"] > 14*86400 and p.get("state") not in {"dormant", "suggested"}:
                change = {**p, "state": "dormant", "reason": "两周未继续关注，暂放但保留经历"}
            if r["kind"] == "boundary" and p.get("action") == "pause" and p.get("review_at", now+1) <= now:
                change = {**p, "action": "clarify", "state": "active", "reason": "暂停已到复查时间，可以重新沟通"}
            if change:
                self.s.record(r["kind"], r["label"], change, r["refs"], r["id"])

    def correct(self, rid, text, resolve=False):
        r = self.get_record(rid)
        if not r:
            raise ValueError("记录不存在")
        with self.s.transaction():
            oid = self.s.observe("user", uuid.uuid4().hex, {"text": text, "scope": "memory_correction", "record_id": rid})
            p = {**r["payload"], "content": text, "epistemic_status": "user_correction", "corrected_at": time.time()}
            if "text" in p:
                p["text"] = text
            if resolve:
                p.update(state="resolved", action="repair" if r["kind"] == "boundary" else p.get("action"))
            self.s.record(r["kind"], r["label"], p, [oid], rid)
            self.s.queue_observation(oid)
            self.s.bump()
        return rid

    def note_boundary_response(self, rid, text):
        r = self.get_record(rid)
        if r and r["kind"] == "boundary" and r["payload"].get("action") == "refuse":
            self.s.record(r["kind"], r["label"], {**r["payload"], "notice_at": time.time(), "notice_text": text[:1500],
                           "notice_status": "response_generated"}, r["refs"], rid)
