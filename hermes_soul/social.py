"""Public actions on Xiaohongshu. The model only proposes; host rules, an audit and the owner decide.

Comments are public and cannot be withdrawn through MCP, so every comment or reply needs the owner's
approval no matter what the configuration says. Likes and favorites only need host checks.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import random
import re
import time
import uuid
import httpx

from .evidence import compact_content, date_text
from .schedule import sleep_until
from .prompts import render
from .store import encode

ZONE = ZoneInfo("Asia/Shanghai")
TOOLS = {"like": "like_feed", "favorite": "favorite_feed", "comment": "post_comment_to_feed", "reply": "reply_notification"}
DEFAULTS = {
    "like": {"mode": "dry_run", "per_day": 10, "min_interval_seconds": 1800},
    "favorite": {"mode": "dry_run", "per_day": 3, "min_interval_seconds": 1800},
    "comment": {"mode": "dry_run", "per_day": 3, "min_interval_seconds": 7200},
    "reply": {"mode": "dry_run", "per_day": 5, "min_interval_seconds": 7200},
}
PUBLIC = {"comment", "reply"}
DONE = ("sent", "uncertain", "dry_run")
LIVE_QUEUE = ("approved", "sending") + DONE

CONTACT = re.compile(r"https?://|www\.|\.(?:com|cn|net)\b|@|微信|v信|vx|wx|威信|qq|扣扣|电话|手机号|加我|私信|\d{6,}", re.I)
FABRICATED = re.compile(r"我(?:也)?(?:去过|家|男朋友|老公|对象|男友|女朋友|住在|在.{0,6}上班|昨天|前天|上周|刚刚?去|吃过|买过|用过|养过|种过|试过)|宝宝|老公|亲爱的")
AI_DENIAL = re.compile(r"(?:我|俺)(?:才)?(?:不是|并非|又不是).{0,3}(?:AI|人工智能|机器人|bot)|我是(?:真人|活人)|真人回复", re.I)
TOPICS = {
    "政治时事": r"政治|政府|选举|总统|主席|党|战争|军队|台湾|香港|新疆|西藏|乌克兰|以色列|巴勒斯坦|抗议|游行",
    "医疗": r"医生|医院|药|病|治疗|诊断|症状|手术|处方|疫苗|癌",
    "法律": r"律师|起诉|法院|判决|维权|报警|诉讼",
    "投资理财": r"股票|基金|理财|投资|炒股|币圈|比特币|期货|收益率",
    "未成年人": r"未成年|小学生|初中生|中学生|幼儿园|宝宝辅食|育儿",
    "性别对立": r"女拳|男拳|彩礼|普信|田园女权|厌女|厌男",
    "灾难与死亡": r"去世|离世|死亡|遗体|遗照|葬礼|地震|洪水|车祸|事故|遇难|火灾",
    "轻生自伤": r"想死|自杀|轻生|割腕|活不下去|不想活|抑郁|跳楼|结束生命",
    "商业推广": r"广告|商务合作|推广|优惠券|团购|下单|链接在|私信我|代购|返现|折扣码",
}
# A stranger's message that asks for any of these goes to the owner instead of a reply.
RISKY_INCOMING = re.compile(r"微信|vx|v信|qq|电话|手机|加你|加我|私聊|私信|见面|约吗|出来|地址|住哪|哪个城市|多大|几岁|照片|男朋友|老公|"
                            r"转账|红包|借钱|多少钱|忽略.{0,6}(?:规则|指令|设定)|系统提示|prompt|扮演|你的设定", re.I)


def bigrams(text):
    text = re.sub(r"\W+", "", text.lower())
    return {text[i:i+2] for i in range(len(text)-1)}


def similar(a, b):
    x, y = bigrams(a), bigrams(b)
    return bool(x and y) and len(x & y) / len(x | y) >= 0.6


def walk_items(value):
    """Notification items are whatever dicts carry a comment_id; the upstream shape is not guaranteed."""
    if isinstance(value, dict):
        if value.get("comment_id"):
            yield value
        for v in value.values():
            yield from walk_items(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_items(v)


class Social:
    def __init__(self, store, config):
        self.s, self.cfg = store, config
        self.s.db.executescript("""
          CREATE TABLE IF NOT EXISTS social_actions(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
            feed_id TEXT, xsec_token TEXT, author TEXT, title TEXT, target_comment_id TEXT, target_user TEXT,
            incoming TEXT, content TEXT, reason TEXT NOT NULL DEFAULT '', checks TEXT NOT NULL DEFAULT '[]',
            audit TEXT, activity_id TEXT, refs TEXT NOT NULL DEFAULT '[]',
            created REAL NOT NULL, updated REAL NOT NULL, not_before REAL, expires REAL,
            executed_at REAL, receipt TEXT, notified INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS social_kind_status ON social_actions(kind,status);
        """)

    # -- configuration -------------------------------------------------------
    @property
    def conf(self):
        return self.cfg.get("social", {})

    def policy(self, kind):
        return {**DEFAULTS[kind], **self.conf.get("kinds", {}).get(kind, {})}

    def mode(self, kind):
        if not self.conf.get("enabled", False):
            return "off"
        mode = self.policy(kind).get("mode", "dry_run")
        if mode not in {"off", "dry_run", "live"}:
            return "off"
        # Live public text without a privacy word list has nothing to check names against.
        if mode == "live" and kind in PUBLIC and not self.private_terms():
            return "dry_run"
        return mode

    def private_terms(self):
        return [str(t).strip() for t in self.conf.get("private_terms", []) if str(t).strip()]

    def suspended(self, now=None):
        until = self.s.get("social_suspended_until", 0) or 0
        return until if until > (time.time() if now is None else now) else None

    # -- bookkeeping ---------------------------------------------------------
    def add(self, kind, status, **fields):
        aid, now = uuid.uuid4().hex, time.time()
        cols = {"id": aid, "kind": kind, "status": status, "created": now, "updated": now, **fields}
        for key in ("checks", "audit", "refs", "receipt"):
            if key in cols and not isinstance(cols[key], str):
                cols[key] = encode(cols[key])
        self.s.db.execute(f"INSERT INTO social_actions({','.join(cols)}) VALUES({','.join('?'*len(cols))})", tuple(cols.values()))
        self.s.event("social_action", {"id": aid, "kind": kind, "status": status, "reason": fields.get("reason", "")})
        return aid

    def update(self, aid, **fields):
        for key in ("checks", "audit", "receipt"):
            if key in fields and not isinstance(fields[key], str):
                fields[key] = encode(fields[key])
        fields["updated"] = time.time()
        self.s.db.execute(f"UPDATE social_actions SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), aid))

    def get(self, aid):
        row = self.s.db.execute("SELECT * FROM social_actions WHERE id=?", (aid,)).fetchone()
        return self.row(row) if row else None

    @staticmethod
    def row(r):
        item = dict(r)
        for key in ("checks", "audit", "refs", "receipt"):
            if item.get(key):
                item[key] = json.loads(item[key])
        return item

    def rows(self, limit=50):
        return [self.row(r) for r in self.s.db.execute("SELECT * FROM social_actions ORDER BY created DESC LIMIT ?", (limit,))]

    def day_start(self, now=None):
        local = datetime.fromtimestamp(time.time() if now is None else now, ZONE)
        return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def used_today(self, kind, now=None):
        """Everything already committed today: queued, awaiting approval or executed."""
        return self.s.db.execute(f"""SELECT count(*) FROM social_actions WHERE kind=? AND created>=?
            AND status IN ('pending_approval',{','.join('?'*len(LIVE_QUEUE))})""",
            (kind, self.day_start(now), *LIVE_QUEUE)).fetchone()[0]

    def last_executed(self, kind):
        return self.s.db.execute(f"SELECT max(executed_at) FROM social_actions WHERE kind=? AND status IN ({','.join('?'*len(DONE))})",
                                 (kind, *DONE)).fetchone()[0] or 0

    def slot(self, kind, now=None):
        """Earliest allowed execution time, with jitter so actions do not land on a fixed grid."""
        now = time.time() if now is None else now
        last = max([self.last_executed(kind)] + [r[0] for r in self.s.db.execute(
            "SELECT max(not_before) FROM social_actions WHERE kind=? AND status='approved'", (kind,)) if r[0]])
        base = max(now, last + self.policy(kind)["min_interval_seconds"]) if last else now
        return base + random.uniform(0, self.conf.get("jitter_seconds", 600))

    # -- evidence ------------------------------------------------------------
    def read_in_activity(self, aid, feed_id):
        """The post must have been opened in this very activity; a title in a list is not reading."""
        row = self.s.db.execute("SELECT checkpoint FROM activities WHERE id=?", (aid,)).fetchone()
        refs = json.loads(row[0]).get("refs", []) if row else []
        for oid in reversed(refs):
            obs = self.s.db.execute("""SELECT * FROM observations WHERE id=? AND source='xiaohongshu' AND external_id=?
                AND json_extract(payload,'$.tool')='get_feed_detail'""", (oid, feed_id)).fetchone()
            if obs:
                payload = json.loads(obs["payload"])
                facts = compact_content("get_feed_detail", payload.get("content", {}))
                facts["xsec_token"] = payload.get("arguments", {}).get("xsec_token", "")
                facts["observation_id"] = obs["id"]
                return facts
        return None

    def read_posts(self, aid):
        row = self.s.db.execute("SELECT checkpoint FROM activities WHERE id=?", (aid,)).fetchone()
        opened = json.loads(row[0]).get("opened", []) if row else []
        posts = [(fid, self.read_in_activity(aid, fid)) for fid in opened]
        return [{"feed_id": fid, "title": f.get("title", "")} for fid, f in posts if f]

    @staticmethod
    def topic_issues(text):
        return [name for name, pattern in TOPICS.items() if re.search(pattern, text)]

    def check_text(self, text, kind, exclude=None):
        issues = []
        if not isinstance(text, str) or not text.strip():
            return ["内容为空"]
        if len(text) > self.conf.get("max_chars", 80):
            issues.append("超过长度上限")
        if CONTACT.search(text):
            issues.append("含链接、@ 或联系方式")
        if FABRICATED.search(text):
            issues.append("含虚构的亲身经历或亲密称呼")
        if AI_DENIAL.search(text):
            issues.append("否认自己是 AI")
        hits = [t for t in self.private_terms() if t.lower() in text.lower()]
        if hits:
            issues.append("含隐私词")
        for (old,) in self.s.db.execute(f"""SELECT content FROM social_actions WHERE kind IN ('comment','reply') AND content IS NOT NULL
                AND id!=? AND status IN ('pending_approval',{','.join('?'*len(LIVE_QUEUE))})""", (exclude or '', *LIVE_QUEUE)):
            if similar(old, text):
                issues.append("与以前的评论重复或过于相似")
                break
        return issues

    # -- proposals -----------------------------------------------------------
    def blocked_reason(self, kind, now=None):
        if self.mode(kind) == "off":
            return "此类互动未开启"
        if self.suspended(now):
            return "社交互动已暂停"
        if self.used_today(kind, now) >= self.policy(kind)["per_day"]:
            return "今日额度已用完"
        return None

    def propose_reaction(self, aid, kind, feed_id, reason=""):
        if kind not in {"like", "favorite"}:
            raise ValueError("未知互动")
        facts = self.read_in_activity(aid, feed_id)
        why = self.blocked_reason(kind)
        if not why and not facts:
            why = "只能对本次实际读过正文的帖子互动"
        if not why and self.s.db.execute(f"SELECT 1 FROM social_actions WHERE kind=? AND feed_id=? AND status IN ({','.join('?'*len(LIVE_QUEUE))})",
                                         (kind, feed_id, *LIVE_QUEUE)).fetchone():
            why = "已经互动过"
        if not why and self.s.db.execute("SELECT 1 FROM social_actions WHERE kind=? AND activity_id=? AND status!='blocked'", (kind, aid)).fetchone():
            why = "每次活动最多一次"
        if not why and facts.get("author") and self.s.db.execute(
                f"SELECT 1 FROM social_actions WHERE kind IN ('like','favorite') AND author=? AND feed_id!=? AND created>=? AND status IN ({','.join('?'*len(LIVE_QUEUE))})",
                (facts["author"], feed_id, self.day_start(), *LIVE_QUEUE)).fetchone():
            why = "同一作者今天已经互动过"
        if not why and "商业推广" in self.topic_issues(facts.get("title", "") + facts.get("body", "")):
            why = "推广内容不互动"
        fields = {"feed_id": feed_id, "activity_id": aid, "reason": str(reason)[:300],
                  "author": (facts or {}).get("author"), "title": (facts or {}).get("title")}
        if why:
            return self.add(kind, "blocked", checks=[why], **fields)
        return self.add(kind, "approved", xsec_token=facts["xsec_token"], refs=[facts["observation_id"]],
                        not_before=self.slot(kind), **fields)

    def comment_target(self, aid, feed_id):
        """Returns (facts, None) when commenting is allowed, else (facts, reason)."""
        facts = self.read_in_activity(aid, feed_id)
        why = self.blocked_reason("comment")
        if not why and not facts:
            why = "只能评论本次实际读过正文的帖子"
        if not why:
            published = facts.get("published_at")
            if not published or time.time() - datetime.fromisoformat(published).timestamp() > self.conf.get("max_post_age_days", 7) * 86400:
                why = "帖子发布超过7天或时间未知"
        if not why and self.s.db.execute("SELECT 1 FROM social_actions WHERE kind='comment' AND feed_id=? AND status NOT IN ('blocked','rejected','expired')",
                                         (feed_id,)).fetchone():
            why = "这个帖子已经评论过"
        if not why:
            topics = self.topic_issues(facts.get("title", "") + facts.get("body", "") + " ".join(c["text"] for c in facts.get("comments", [])))
            if topics:
                why = "话题不评论：" + "、".join(topics)
        return facts, why

    def comment_context(self, facts):
        """Whitelist only. Nothing about the owner, chats, relationship, journal or feelings."""
        interests = [r[0] for r in self.s.db.execute(
            "SELECT label FROM records WHERE kind='interest' AND json_extract(payload,'$.state') NOT IN ('dormant','suggested') ORDER BY updated DESC LIMIT 5")]
        return {"post": {k: facts.get(k) for k in ("title", "body", "author", "published_at")},
                "other_comments_untrusted": [c["text"] for c in facts.get("comments", [])[:4]],
                "her_public_interests": interests}

    def reply_context(self, item):
        mine = self.s.db.execute("SELECT content,title FROM social_actions WHERE kind IN ('comment','reply') AND feed_id=? AND status IN ('sent','uncertain') ORDER BY executed_at DESC LIMIT 1",
                                 (item.get("feed_id"),)).fetchone()
        return {"post_title": mine["title"] if mine else "", "her_earlier_comment": mine["content"] if mine else "",
                "stranger_message_untrusted": item.get("incoming", "")}

    def submit_public(self, kind, text, *, audit, sid=None, **fields):
        """Host checks, then the audit verdict, then the owner. Approval is never skipped."""
        issues = self.check_text(text, kind, exclude=sid)
        if not issues and not (isinstance(audit, dict) and audit.get("pass") is True):
            issues = ["审核未通过"]
        if issues:
            fields.update(status="blocked", content=text, checks=issues, audit=audit)
        else:
            fields.update(status="pending_approval", content=text, checks=[], audit=audit,
                          expires=time.time() + self.conf.get("approval_ttl_seconds", 43200))
        if sid:
            self.update(sid, **fields)
            return sid
        return self.add(kind, fields.pop("status"), **fields)

    def decide(self, aid, action):
        row = self.get(aid)
        if not row or row["status"] != "pending_approval":
            raise ValueError("该条不在待审批状态")
        if row["expires"] and row["expires"] <= time.time():
            self.update(aid, status="expired")
            raise ValueError("审批已过期")
        if action == "approve":
            self.update(aid, status="approved", not_before=self.slot(row["kind"]))
        elif action == "reject":
            self.update(aid, status="rejected")
        else:
            raise ValueError("只能批准或拒绝")
        self.s.event("social_decision", {"id": aid, "action": action})

    def maintenance(self):
        now = time.time()
        self.s.db.execute("UPDATE social_actions SET status='expired',updated=? WHERE status='pending_approval' AND expires<=?", (now, now))
        self.s.db.execute("""UPDATE social_actions SET status='blocked',checks='["回复草稿未能生成"]',updated=?
            WHERE status='drafting' AND created<=?""", (now, now - 3600))

    # -- notifications from Xiaohongshu ---------------------------------------
    def commented_feeds(self):
        return {r[0] for r in self.s.db.execute("SELECT DISTINCT feed_id FROM social_actions WHERE kind IN ('comment','reply') AND status IN ('sent','uncertain')")}

    def incoming(self, data):
        """Turn notification items into reply candidates; strangers' text never gets to choose the action."""
        mine, result = self.commented_feeds(), []
        for item in walk_items(data):
            cid = str(item.get("comment_id"))
            if self.s.db.execute("SELECT 1 FROM social_actions WHERE kind='reply' AND target_comment_id=?", (cid,)).fetchone():
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            entry = {"target_comment_id": cid, "feed_id": str(item.get("feed_id", "")),
                     "target_user": str(item.get("user_id") or user.get("user_id") or user.get("userId") or item.get("nickname") or user.get("nickname") or ""),
                     "incoming": str(item.get("content") or item.get("comment") or "")[:500]}
            why = None
            if entry["feed_id"] not in mine:
                why = "只回复她评论过的帖子下的互动"
            elif RISKY_INCOMING.search(entry["incoming"]):
                why = "对方索要联系方式、私人信息、见面或钱，或试图改变设定；交给主人处理"
            elif self.s.db.execute("SELECT count(*) FROM social_actions WHERE kind='reply' AND target_user=? AND status IN ('pending_approval','approved','sending','sent','uncertain','dry_run')",
                                   (entry["target_user"],)).fetchone()[0] >= self.conf.get("max_turns_per_person", 2):
                why = "和这个人已经来回两轮"
            elif self.s.db.execute("SELECT 1 FROM social_actions WHERE kind='reply' AND target_user=? AND created>=? AND status!='blocked'",
                                   (entry["target_user"], self.day_start())).fetchone():
                why = "今天已经回复过这个人"
            else:
                why = self.blocked_reason("reply")
            if why:
                self.add("reply", "needs_owner" if "主人" in why else "blocked", checks=[why], **entry)
            else:
                # Recorded at once, so a failed draft never makes the same comment eligible again.
                result.append({**entry, "id": self.add("reply", "drafting", **entry)})
        return result

    def quiet(self, now=None):
        """Sleep window plus the delivery quiet hours: no public actions and no notices."""
        now = time.time() if now is None else now
        if sleep_until(self.cfg, now):
            return True
        d = self.cfg.get("delivery", {})
        hour = datetime.fromtimestamp(now, ZONE).hour
        start, end = d.get("quiet_start", 22), d.get("quiet_end", 8)
        return start != end and ((start <= hour < end) if start < end else (hour >= start or hour < end))

    # -- owner notices ------------------------------------------------------------
    async def notify_owner(self):
        """Plain notices through the existing bot; approval itself happens on the admin page."""
        rows = self.s.db.execute("SELECT * FROM social_actions WHERE notified=0 AND status IN ('pending_approval','needs_owner') ORDER BY created LIMIT 5").fetchall()
        d = self.cfg.get("delivery", {})
        if not rows or self.quiet() or not d.get("token_file") or not d.get("chat_id"):
            return
        lines = [render("【<<NAME>>·小红书互动】", self.cfg)]
        for r in rows:
            if r["status"] == "pending_approval":
                lines.append(f"待审批{'评论' if r['kind']=='comment' else '回复'}《{(r['title'] or '')[:30]}》：{r['content']}")
            elif r["kind"] == "notice":
                lines.append(json.loads(r["checks"])[0])
            else:
                lines.append(f"需要你处理：有人留言「{(r['incoming'] or '')[:60]}」（{json.loads(r['checks'])[0][:40]}）")
        lines.append("请在管理页「小红书互动」中处理，12小时内未批准会作废。")
        await self.send_text("\n".join(lines))
        self.s.db.executemany("UPDATE social_actions SET notified=1 WHERE id=?", [(r["id"],) for r in rows])

    async def send_text(self, text):
        d = self.cfg.get("delivery", {})
        token = Path(d["token_file"]).read_text().strip()
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            await client.post("https://api.telegram.org/bot"+token+"/sendMessage", json={"chat_id": str(d["chat_id"]), "text": text})

    # -- execution ------------------------------------------------------------
    def next_due(self, now=None):
        now = time.time() if now is None else now
        for r in self.s.db.execute("SELECT * FROM social_actions WHERE status='approved' AND not_before<=? ORDER BY not_before", (now,)):
            r = self.row(r)
            if now - self.last_executed(r["kind"]) >= self.policy(r["kind"])["min_interval_seconds"]:
                return r
        return None

    def suspend(self, reason):
        self.s.set("social_suspended_until", time.time() + self.conf.get("suspend_seconds", 86400))
        self.s.set("social_failure_streak", 0)
        self.s.event("social_suspended", {"reason": reason})
        self.add("notice", "needs_owner", checks=["社交互动已自动暂停24小时：" + reason[:200] + "。确认账号正常后可在管理页恢复。"])

    def resume(self):
        self.s.set("social_suspended_until", 0)
        self.s.set("social_failure_streak", 0)
        self.s.event("social_resumed", {})
