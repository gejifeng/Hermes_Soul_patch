"""Single-process state owner; external clients use the HTTP API, never SQLite."""
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import fcntl
import json
import sqlite3
import time
import uuid


class BudgetExhausted(RuntimeError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = (self.home / "runtime.lock").open("a")
        try:
            fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lockfile.close()
            raise RuntimeError("此生活目录已有运行实例；请使用 HTTP API 或 CLI 操作现有服务") from None
        for name in ("journal", "artifacts", "notes", "identity"):
            (self.home / name).mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.home / "state.sqlite", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        PRAGMA busy_timeout=5000;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,kind TEXT NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,source TEXT NOT NULL,external_id TEXT,
          fingerprint TEXT UNIQUE NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activities(id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,
          started REAL NOT NULL,ended REAL,reason TEXT NOT NULL,checkpoint TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,label TEXT NOT NULL,
          payload TEXT NOT NULL,refs TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS chats(id TEXT PRIMARY KEY,role TEXT NOT NULL,content TEXT NOT NULL,
          created REAL NOT NULL,status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,purpose TEXT NOT NULL,started REAL NOT NULL,
          day TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS calls_day ON calls(day);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,content TEXT NOT NULL,refs TEXT NOT NULL,
          created REAL NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS records_kind ON records(kind);
        """)
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('revision','0')")
        self.db.execute("INSERT OR IGNORE INTO meta VALUES('paused','false')")
        # Additive, idempotent migration; no invented interpretation of old records.
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS activity_steps(
          id INTEGER PRIMARY KEY AUTOINCREMENT, activity_id TEXT NOT NULL,
          stage TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS steps_activity ON activity_steps(activity_id,id);
        CREATE TABLE IF NOT EXISTS observation_queue(
          observation_id TEXT PRIMARY KEY REFERENCES observations(id),
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS record_terms(term TEXT NOT NULL,record_id TEXT NOT NULL,
          PRIMARY KEY(term,record_id));
        CREATE INDEX IF NOT EXISTS terms_record ON record_terms(record_id);
        CREATE TABLE IF NOT EXISTS expression_meta(
          id TEXT PRIMARY KEY REFERENCES outbox(id),updated REAL NOT NULL,
          not_before REAL NOT NULL,expires REAL NOT NULL,reason TEXT NOT NULL,
          reviewed_revision INTEGER,receipt TEXT,attempt_id TEXT);
        """)
        if self.get("schema_version", 1) < 2:
            with self.transaction():
                for oid in self.get("pending_chat_refs", []):
                    self.db.execute("INSERT OR IGNORE INTO observation_queue(observation_id) SELECT id FROM observations WHERE id=?", (oid,))
                self.db.execute("INSERT OR IGNORE INTO expression_meta SELECT id,created,created,created+604800,'从旧版迁移，待重新判断',NULL,NULL,NULL FROM outbox")
                self.set("schema_version", 2)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def close(self):
        self.db.close()
        self.lockfile.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, encode(value)))

    def bump(self):
        rev = self.get("revision", 0) + 1
        self.set("revision", rev)
        return rev

    def event(self, kind, payload):
        eid = uuid.uuid4().hex
        self.db.execute("INSERT INTO events VALUES(?,?,?,?)", (eid, kind, time.time(), encode(payload)))
        return eid

    def observe(self, source, external_id, payload):
        from .evidence import semantic_fingerprint
        fingerprint = semantic_fingerprint(source, external_id, payload)
        old = self.db.execute("SELECT id FROM observations WHERE fingerprint=?", (fingerprint,)).fetchone()
        if old:
            return old[0]
        if source == "xiaohongshu":
            for prior in self.db.execute("SELECT id,payload FROM observations WHERE source=? AND external_id=?", (source, external_id)):
                if semantic_fingerprint(source, external_id, json.loads(prior['payload'])) == fingerprint:
                    return prior['id']
        oid = uuid.uuid4().hex
        self.db.execute("INSERT INTO observations VALUES(?,?,?,?,?,?)",
                        (oid, source, external_id, fingerprint, time.time(), encode(payload)))
        return oid

    def valid_refs(self, refs):
        if not isinstance(refs, list) or not refs or len(refs) > 20:
            raise ValueError("持久记录必须引用实际观察")
        for ref in refs:
            if not isinstance(ref, str) or not self.db.execute("SELECT 1 FROM observations WHERE id=?", (ref,)).fetchone():
                raise ValueError("引用不存在的外部观察")

    def record(self, kind, label, payload, refs, rid=None):
        if kind not in {"interest", "episode", "belief", "relationship", "journal", "intention", "task", "situation", "boundary", "agreement", "affect"}:
            raise ValueError("未知记录类型")
        self.valid_refs(refs)
        rid = rid or uuid.uuid4().hex
        now = time.time()
        previous = self.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
        if previous and previous["kind"] != kind:
            raise ValueError("记录类型不可更换")
        self.event("record_revision", {"id": rid, "before": dict(previous) if previous else None,
                                       "kind": kind, "payload": payload, "refs": refs})
        self.db.execute("""INSERT INTO records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
          label=excluded.label,payload=excluded.payload,refs=excluded.refs,updated=excluded.updated""",
          (rid, kind, label[:200], encode(payload), encode(refs), now, now))
        from .memory import index_record
        index_record(self, rid, label, payload)
        return rid

    def step(self, aid, stage, status, **payload):
        self.db.execute("INSERT INTO activity_steps(activity_id,stage,status,created,payload) VALUES(?,?,?,?,?)",
                        (aid, stage, status, time.time(), encode(payload)))

    def steps(self, aid):
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in self.db.execute(
            "SELECT * FROM activity_steps WHERE activity_id=? ORDER BY id", (aid,))]

    def queue_observation(self, oid):
        self.db.execute("INSERT OR IGNORE INTO observation_queue(observation_id) VALUES(?)", (oid,))

    def pending_observations(self, limit=6):
        return [r[0] for r in self.db.execute("""SELECT q.observation_id FROM observation_queue q
          JOIN observations o ON o.id=q.observation_id WHERE q.status='pending' AND q.attempts<3
          ORDER BY o.created LIMIT ?""", (limit,))]

    def start_activity(self, kind, reason):
        aid = uuid.uuid4().hex
        self.db.execute("INSERT INTO activities VALUES(?,?, 'running', ?, NULL, ?, '{}')",
                        (aid, kind, time.time(), reason))
        return aid

    def finish_activity(self, aid, status, checkpoint):
        row = self.db.execute("SELECT checkpoint FROM activities WHERE id=?", (aid,)).fetchone()
        checkpoint = {**(json.loads(row[0]) if row else {}), **checkpoint}
        self.db.execute("UPDATE activities SET status=?,ended=?,checkpoint=? WHERE id=?",
                        (status, time.time(), encode(checkpoint), aid))

    def checkpoint(self, aid, value):
        self.db.execute("UPDATE activities SET checkpoint=? WHERE id=?", (encode(value), aid))

    def add_chat(self, role, content, status="complete"):
        cid = uuid.uuid4().hex
        self.db.execute("INSERT INTO chats VALUES(?,?,?,?,?)", (cid, role, content, time.time(), status))
        return cid

    def recover(self):
        self.db.execute("UPDATE activities SET status='interrupted',ended=? WHERE status='running'", (time.time(),))
        self.db.execute("UPDATE calls SET status='interrupted' WHERE status='running'")
        self.db.execute("UPDATE chats SET status='interrupted' WHERE status='pending'")
        self.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE activity_steps SET status='interrupted' WHERE status='running'")

    def budget_usage(self):
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        total = self.db.execute("SELECT count(*) FROM calls WHERE day=? AND purpose!='chat'", (day,)).fetchone()[0]
        baseline = self.get("budget_baseline:"+day, 0)
        return {"day": day, "used": max(0, total-baseline), "total_recorded": total, "reset_baseline": baseline}

    def reset_budget(self):
        with self.transaction():
            usage = self.budget_usage()
            self.set("budget_baseline:"+usage["day"], usage["total_recorded"])
            self.event("budget_reset", usage)
        return self.budget_usage()

    def reserve_call(self, purpose, limit, foreground=False):
        day = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if not foreground and self.budget_usage()["used"] >= limit:
            raise BudgetExhausted("今日后台模型调用预算已用完")
        cid = uuid.uuid4().hex
        self.db.execute("INSERT INTO calls VALUES(?,?,?,?, 'running', '{}')", (cid, purpose, time.time(), day))
        return cid

    def call_done(self, cid, status, detail):
        self.db.execute("UPDATE calls SET status=?,detail=? WHERE id=?", (status, encode(detail), cid))

    def rows(self, table, limit=30):
        order = {"records": "updated", "events": "created", "observations": "created", "activities": "started",
                 "chats": "created", "calls": "started", "outbox": "created"}
        if table not in order:
            raise ValueError("未知表")
        rows = self.db.execute(f"SELECT * FROM {table} ORDER BY {order[table]} DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for key in ("payload", "refs", "checkpoint", "detail"):
                if key in item:
                    item[key] = json.loads(item[key])
            result.append(item)
        return result

    def context(self):
        records = self.rows("records", 60)
        observations = self.rows("observations", 8)
        return {"revision": self.get("revision"), "now": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                "records": records, "recent_activities": self.rows("activities", 5),
                "recent_observations": [{**o, "payload": encode(o["payload"])[:1600]} for o in observations]}
