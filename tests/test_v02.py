import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from hermes_soul.api import create_app
from hermes_soul.expressions import Expressions
from hermes_soul.life import Life
from hermes_soul.memory import recall
from hermes_soul.runtime import Runtime
from hermes_soul.store import Store


class LifeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(self.tmp.name)
        self.life = Life(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def obs(self, text, source="user", key=None):
        return self.s.observe(source, key or text, {"text": text, "tool": "get_feed_detail"} if source == "xiaohongshu" else {"text": text})

    def save(self, kind, label, ref, aid="a", **kw):
        return self.life.save(self.life.prepare({"kind": kind, "label": label, "content": label,
            "refs": [ref], **kw}, {ref}, aid))

    def test_interest_requires_distinct_reads_and_separate_visits(self):
        a = self.obs("阳台1", "xiaohongshu", "note1")
        rid = self.save("interest", "阳台", a, state="preferred")
        self.assertEqual(self.life.get_record(rid)["payload"]["state"], "noticed")
        self.save("interest", "阳台", a, aid="b", id=rid, state="preferred", attraction="光照", open_question="怎样选择位置")
        self.assertEqual(len(self.life.get_record(rid)["payload"]["visit_activities"]), 1)
        b = self.obs("阳台2", "xiaohongshu", "note2")
        self.save("interest", "阳台", b, aid="b", id=rid, state="preferred", attraction="光照", open_question="怎样选择位置")
        self.assertEqual(self.life.get_record(rid)["payload"]["state"], "exploring")
        c = self.obs("阳台3", "xiaohongshu", "note3")
        self.s.db.execute("UPDATE observations SET created=created-22000 WHERE id=?", (a,))
        self.save("interest", "阳台", c, aid="c", id=rid, state="preferred", attraction="叶片", open_question="怎样浇水")
        self.assertEqual(self.life.get_record(rid)["payload"]["state"], "preferred")

    def test_suggestion_is_not_personal_preference_and_dormancy_can_rekindle(self):
        a = self.obs("你可以看看花")
        rid = self.save("interest", "花", a, state="preferred")
        self.assertEqual(self.life.get_record(rid)["payload"]["state"], "suggested")
        b = self.obs("绣球花", "xiaohongshu")
        self.save("interest", "花", b, aid="b", id=rid, state="dormant")
        c = self.obs("新的花园", "xiaohongshu")
        self.save("interest", "花", c, aid="c", id=rid)
        self.assertEqual(self.life.get_record(rid)["payload"]["state"], "rekindled")

    def test_situation_expiry_and_slot_replacement_keep_history(self):
        oid = self.obs("去看公园")
        first = self.save("situation", "公园", oid, slot="attention", ttl_hours=1)
        second = self.save("situation", "阳台", oid, slot="attention", ttl_hours=1)
        self.assertEqual([r["id"] for r in self.life.current_situations()], [second])
        self.assertEqual(self.life.get_record(first)["payload"]["state"], "superseded")
        r = self.life.get_record(second)
        self.s.record(r["kind"], r["label"], {**r["payload"], "expires_at": 1}, r["refs"], second)
        self.life.maintenance()
        self.assertEqual(self.life.current_situations(), [])
        self.assertEqual(self.life.get_record(second)["payload"]["state"], "expired")

    def test_recall_finds_old_topic_beyond_recent_window(self):
        oid = self.obs("阳台")
        rid = self.save("belief", "阳台的光照", oid)
        for i in range(90):
            self.save("belief", "不同的菜谱"+str(i), oid)
        self.assertEqual(recall(self.s, "阳台光照")[0]["id"], rid)

    def test_relationship_cannot_be_inferred_from_forum_and_agreement_needs_quote(self):
        xhs = self.obs("别人抱怨伴侣", "xiaohongshu")
        with self.assertRaises(ValueError):
            self.save("relationship", "我们的分歧", xhs)
        oid = self.obs("你可以叫我老大")
        with self.assertRaises(ValueError):
            self.save("agreement", "称呼", oid, user_quote="永远叫我宝宝")
        rid = self.save("agreement", "称呼", oid, user_quote="可以叫我老大")
        self.assertEqual(self.life.get_record(rid)["payload"]["epistemic_status"], "user_confirmed_statement")

    def test_pause_requires_prior_notice_new_event_and_is_recoverable(self):
        oid = self.obs("一个具体玩笑")
        rid = self.save("boundary", "玩笑", oid, action="pause", uncertainty="low")
        self.assertEqual(self.life.get_record(rid)["payload"]["action"], "clarify")
        self.save("boundary", "玩笑", oid, id=rid, action="refuse", uncertainty="low")
        self.s.add_chat("assistant", "这个玩笑我不想继续，请换个话题。")
        self.life.note_boundary_response(rid, "这个玩笑我不想继续，请换个话题。")
        self.save("boundary", "玩笑", oid, id=rid, action="pause", uncertainty="low")
        self.assertEqual(self.life.get_record(rid)["payload"]["action"], "clarify")
        self.save("boundary", "玩笑", oid, id=rid, action="refuse", uncertainty="low")
        new = self.obs("又重复同一个玩笑")
        self.save("boundary", "玩笑", new, id=rid, action="pause", uncertainty="low", scope="conversation", pause_seconds=99999)
        p = self.life.get_record(rid)["payload"]
        self.assertEqual(p["action"], "pause")
        self.assertLessEqual(p["review_at"]-time.time(), 1800)
        self.life.correct(rid, "已澄清，恢复交流", resolve=True)
        self.assertEqual(self.life.boundaries(), [])

    def test_migration_preserves_old_records_and_seeds_only_pending_work(self):
        oid = self.obs("原有用户交流")
        rid = self.save("belief", "原有认识", oid)
        original = dict(self.s.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
        self.s.set("schema_version", 1)
        self.s.set("pending_chat_refs", [oid])
        self.s.db.execute("INSERT INTO outbox VALUES('old','旧草稿',?,?,'draft',0)", (json.dumps([oid]), time.time()))
        self.s.close()
        c = sqlite3.connect(Path(self.tmp.name)/"state.sqlite")
        for table in ("activity_steps", "observation_queue", "expression_meta", "record_terms"):
            c.execute("DROP TABLE "+table)
        c.execute("DELETE FROM meta WHERE key='recall_index_version'");c.commit();c.close()
        self.s = Store(self.tmp.name);self.life = Life(self.s)
        self.assertEqual(dict(self.s.db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()), original)
        self.assertEqual(self.s.pending_observations(), [oid])
        self.assertEqual(Expressions(self.s, {}).rows()[0]["status"], "draft")


class RuntimeV02Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Runtime({"home": self.tmp.name, "mcp_url": "http://127.0.0.1:1/mcp", "interval_seconds": 720,
            "idle_max_seconds": 720, "max_calls_per_day": 20, "max_browse_steps": 3})

    async def asyncTearDown(self):
        self.r.store.close();self.tmp.cleanup()

    async def test_detail_timeout_preserves_list_and_allows_stop(self):
        listing = {"content": [{"type": "text", "text": json.dumps({"feeds": [{"id": "a", "xsecToken": "t", "noteCard": {"type": "normal"}}]})}]}
        self.r.mcp.call = AsyncMock(side_effect=[listing, httpx.ReadTimeout("")])
        self.r.login_status = AsyncMock(return_value={"logged_in": True})
        self.r.model = AsyncMock(side_effect=['{"activity":"browse"}', '{"action":"read","feed_id":"a"}',
            '{"action":"stop"}', '{"updates":[]}'])
        await self.r.tick()
        a = self.r.store.rows("activities")[0]
        self.assertEqual(a["status"], "partial")
        self.assertEqual(len(a["checkpoint"]["refs"]), 1)
        errors = [s for s in self.r.store.steps(a["id"]) if s["status"] == "failed"]
        self.assertEqual(errors[0]["payload"]["code"], "timeout")
        self.assertTrue(errors[0]["payload"]["message"])
        self.assertGreater(self.r.store.get("post_backoff:a"), time.time())

    async def test_interrupted_reflection_leaves_observation_pending(self):
        oid = self.r.store.observe("user", "u", {"text": "公园"});self.r.store.queue_observation(oid)
        self.r.model = AsyncMock(side_effect=ValueError("bad JSON"))
        with self.assertRaises(ValueError):
            await self.r.reflect("a", [oid], 0)
        self.assertEqual(self.r.store.pending_observations(), [oid])

    async def test_default_draft_never_calls_network_and_new_chat_invalidates_ready(self):
        oid = self.r.store.observe("user", "u", {"text": "公园"})
        eid = self.r.expressions.create("看到了一个公园", [oid], 0)
        self.assertEqual(eid, self.r.expressions.create("看到了一个公园！", [oid], 0))
        self.r.expressions.decide(eid, "ready", "可以分享", 0)
        with patch("httpx.AsyncClient.post", new=AsyncMock()) as post:
            await self.r.expressions.deliver_one()
            post.assert_not_awaited()
        self.r.expressions.invalidate()
        self.assertEqual(self.r.expressions.rows()[0]["status"], "stale")

    async def test_delivery_timeout_is_uncertain_and_not_retried(self):
        token = Path(self.tmp.name)/"fake-token";token.write_text("fake-test-token")
        x = self.r.expressions;x.cfg["bridge_owner_ids"] = ["test-owner"]
        x.delivery = {"mode": "telegram", "authorized": True, "chat_id": "test-owner", "token_file": str(token)}
        oid = self.r.store.observe("user", "u", {"text": "公园"})
        eid = x.create("一个公园", [oid], 0);x.decide(eid, "ready", "合适", 0)
        with patch.object(x, "delivery_block", return_value=None), patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ReadTimeout(""))) as post:
            await x.deliver_one();await x.deliver_one()
            post.assert_awaited_once()
        self.assertEqual(x.rows()[0]["status"], "uncertain")

    async def test_delivery_requires_receipt_and_respects_revision(self):
        x = self.r.expressions;token = Path(self.tmp.name)/"fake-token";token.write_text("fake")
        x.delivery = {"mode": "telegram", "authorized": True, "chat_id": "test-owner", "token_file": str(token)}
        oid = self.r.store.observe("user", "u", {"text": "公园"});eid=x.create("公园见闻", [oid], 0);x.decide(eid, "ready", "合适", 0)
        self.r.store.bump()
        response = httpx.Response(200, json={"ok": True, "result": {"message_id": 10}})
        with patch.object(x, "delivery_block", return_value=None), patch("httpx.AsyncClient.post", new=AsyncMock(return_value=response)) as post:
            await x.deliver_one();post.assert_not_awaited()
            x.decide(eid, "ready", "重新判断", 1);await x.deliver_one();post.assert_awaited_once()
        self.assertEqual(x.rows()[0]["receipt"]["message_id"], 10)
        self.assertEqual(x.rows()[0]["status"], "sent")


class ManagementTests(unittest.TestCase):
    def test_correction_history_restore_export_and_management_during_pause(self):
        with tempfile.TemporaryDirectory() as home:
            token=Path(home)/"token";token.write_text("t"*32)
            cfg={"home":home,"access_token_file":str(token),"mcp_url":"http://127.0.0.1:1/mcp","interval_seconds":720,"idle_max_seconds":720}
            with patch("hermes_soul.runtime.Runtime.loop", new=AsyncMock()), TestClient(create_app(cfg)) as c:
                self.assertEqual(c.get('/api/export').status_code,401)
                c.headers['Authorization']='Bearer '+"t"*32
                rid=c.post('/api/interests',json={'topic':'阳台','description':'旧内容'}).json()['id']
                c.post('/api/control/pause')
                self.assertEqual(c.post('/api/records/'+rid+'/correct',json={'text':'新内容'}).status_code,200)
                h=c.get('/api/records/'+rid+'/history').json()['history'];eid=h[0]['id']
                r=c.post('/api/records/'+rid+'/restore/'+eid).json()['record']
                self.assertEqual(r['payload']['content'],'旧内容')
                self.assertEqual(c.post('/api/relationship/resume').status_code,200)
                self.assertIn('data',c.get('/api/export').json())
                self.assertTrue(c.get('/api/state').json()['status']['paused'])
