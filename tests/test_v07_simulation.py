from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo
import json
import tempfile
import unittest

from hermes_soul.channels import compute_typing_plan, send_onebot_message
from hermes_soul.runtime import Runtime

ZONE = ZoneInfo("Asia/Shanghai")
# Wednesday 2026-11-11 16:00:00 +08:00
WED_4PM = datetime(2026, 11, 11, 16, 0, tzinfo=ZONE).timestamp()
HOUR = 3600.0


class V07SimulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        token = Path(self.temp.name) / "fake-token"
        token.write_text("fake")
        self.cfg = {
            "home": self.temp.name,
            "mcp_url": "http://127.0.0.1:1/mcp",
            "interval_seconds": 720,
            "idle_max_seconds": 720,
            "max_calls_per_day": 120,
            "max_browse_steps": 3,
            "bridge_owner_ids": ["10001"],
            "sleep_schedule": {"enabled": True, "start": "00:00", "end": "08:00", "timezone": "Asia/Shanghai"},
            "qq": {
                "enabled": True,
                "owner_ids": ["10001"],
                "http_url": "http://127.0.0.1:3000",
                "typing_simulation": False,
            },
            "delivery": {
                "mode": "qq",
                "authorized": True,
                "qq_user_id": "10001",
                "qq_http_url": "http://127.0.0.1:3000",
                "quiet_start": 0,
                "quiet_end": 0,
                "min_interval_seconds": 3600,
                "block_gap_seconds": 120,
                "max_per_block": 4,
                "max_per_day": 32,
            },
        }
        with patch("time.time", return_value=WED_4PM):
            self.r = Runtime(self.cfg)
        self.s = self.r.store

    async def asyncTearDown(self):
        self.s.close()
        self.temp.cleanup()

    async def test_commitment_lifecycle_anticipating_and_fulfilled(self):
        """1. Cross-day weekend promise survives weekday browsing, activates on Saturday, and fulfills on owner action."""
        u_obs = self.s.observe("user", "u_wed", {"text": "这周末带你去原木暖光的老房子小店喝下午茶，顺便买盆薄荷放窗边"})
        with patch("time.time", return_value=WED_4PM):
            self.s.set("last_user_at", WED_4PM)
            rid = self.r.life.save(self.r.life.prepare({
                "kind": "situation",
                "label": "周末下午茶与窗边薄荷",
                "content": "他约这周末去原木暖光的老房子小店喝下午茶，顺便买盆薄荷放窗边。等周末到了再说。",
                "slot": "continuation",
                "waiting_for": "time",
                "refs": [u_obs],
            }, {u_obs}, "act_wed"))
            self.r.attention.sync(rid)

            # Thursday: another normal `continuation` situation must NOT overwrite the weekend commitment!
            other_obs = self.s.observe("xiaohongshu", "n_thu", {"text": "老洋房散步路线", "tool": "get_feed_detail"})
            self.r.life.save(self.r.life.prepare({
                "kind": "situation",
                "label": "顺手记一条散步路线",
                "content": "看到一条衡山路散步路线",
                "slot": "continuation",
                "refs": [other_obs],
            }, {other_obs}, "act_thu"))

        rec = self.r.life.get_record(rid)
        self.assertEqual(rec["payload"]["slot"], "commitment")
        self.assertEqual(rec["payload"]["commitment_status"], "scheduled")
        self.assertNotEqual(rec["payload"]["state"], "superseded")

        # Advance to Saturday 14:00 (2026-11-14 14:00 +08:00), keeping recent chat warm (1h ago) so phase=='attached'
        sat_2pm = datetime(2026, 11, 14, 14, 0, tzinfo=ZONE).timestamp()
        with patch("time.time", return_value=sat_2pm):
            self.s.set("last_user_at", sat_2pm - 3600)
            self.r.contact.maintenance()
            rec_sat = self.r.life.get_record(rid)
            self.assertEqual(rec_sat["payload"]["commitment_status"], "anticipating")
            self.assertFalse(self.r.attention.blocked(rid))

            att_ctx = self.r.background_context(purpose="attention")
            self.assertEqual(att_ctx["dynamics"]["phase"], "attached")
            cand_ids = [c["id"] for c in att_ctx["candidates"]]
            self.assertIn("reach_out", cand_ids)

            # Owner fulfills the promise!
            self.r.contact.on_owner_message("收拾好出门啦，带你去那家老房子小店喝下午茶，顺便挑盆薄荷！")
            rec_done = self.r.life.get_record(rid)
            self.assertEqual(rec_done["payload"]["commitment_status"], "fulfilled")
            self.assertEqual(rec_done["payload"]["state"], "resolved")
            self.assertGreater(self.r.dynamics.raw_state()["T_sec"], 0.05)
            self.assertEqual(self.r.contact.state(sat_2pm)["commitments"]["recent_event"]["status"], "fulfilled")

    async def test_commitment_overdue_increases_grievance_and_erodes_security_reserve(self):
        """Broken/ignored promise past window_end marks overdue, raises G & B_sens, and lowers T_sec."""
        u_obs = self.s.observe("user", "u_prom", {"text": "明天下午带你去看展"})
        with patch("time.time", return_value=WED_4PM):
            self.s.set("last_user_at", WED_4PM)
            rid = self.r.life.save(self.r.life.prepare({
                "kind": "situation",
                "label": "明天下午看展约定",
                "content": "他说好明天下午一起去看展",
                "waiting_for": "time",
                "refs": [u_obs],
            }, {u_obs}, "act_1"))
            self.r.attention.sync(rid)

        # Thursday 15:00: owner sends a cancellation excuse -> must NOT mark fulfilled!
        thu_3pm = datetime(2026, 11, 12, 15, 0, tzinfo=ZONE).timestamp()
        with patch("time.time", return_value=thu_3pm):
            self.r.contact.maintenance()
            self.r.contact.on_owner_message("今天下午改天再去吧，临时去不了看展了")
            self.assertEqual(self.r.life.get_record(rid)["payload"]["commitment_status"], "anticipating")

        # Thursday 21:00 (past 18:30 afternoon window_end): marks overdue!
        thu_9pm = datetime(2026, 11, 12, 21, 0, tzinfo=ZONE).timestamp()
        with patch("time.time", return_value=thu_9pm):
            self.s.set("last_user_at", thu_9pm - 1800)
            self.r.contact.maintenance()
            rec_over = self.r.life.get_record(rid)
            self.assertEqual(rec_over["payload"]["commitment_status"], "overdue")
            st = self.r.dynamics.raw_state()
            self.assertGreaterEqual(st["G"], 0.35)
            self.assertLess(st["T_sec"], -0.10)
            self.assertGreater(st["B_sens"], 0.20)

    async def test_borbely_sleep_pressure_and_late_night_drowsy_bubbles(self):
        """3. Homeostatic sleep pressure H rises across waking hours, clears in sleep, and shortens late-night bubbles."""
        # Advance from Wednesday 16:00 -> Wednesday 23:45 (awake): H builds up -> drowsy_sleepy
        wed_late = datetime(2026, 11, 11, 23, 45, tzinfo=ZONE).timestamp()
        with patch("time.time", return_value=wed_late):
            snap_late = self.r.dynamics.advance(wed_late)
            self.assertGreater(snap_late["H"], 0.52)
            self.assertEqual(snap_late["energy_band"], "drowsy_sleepy")
            self.assertLessEqual(snap_late["max_bubble_chars"], 16)

            oid = self.s.observe("user", "u_late", {"text": "睡了吗"})
            raw_json = json.dumps({
                "replies": [
                    "困得眼皮都快睁不开了，我先钻进被窝眯一会儿，你也别熬太晚早点休息哦",
                    "晚安亲爱的",
                    "明天早上再跟你说今天看到的那家店",
                ],
                "boundary": None,
            }, ensure_ascii=False)
            reply_text, _ = self.r.apply_chat_result(raw_json, oid, user_text="睡了吗")
            bubbles = reply_text.split("\n")
            self.assertLessEqual(len(bubbles), 2)
            self.assertLessEqual(len(bubbles[0]), 22)

        # Advance across 00:00-08:00 sleep window to Thursday 09:00: H clears!
        thu_9am = datetime(2026, 11, 12, 9, 0, tzinfo=ZONE).timestamp()
        with patch("time.time", return_value=thu_9am):
            snap_morn = self.r.dynamics.advance(thu_9am)
            self.assertLess(snap_morn["H"], 0.25)
            self.assertNotEqual(snap_morn["energy_band"], "drowsy_sleepy")

    async def test_three_timescale_structural_plasticity_modulates_grace_hours(self):
        """4. Ultra-slow (T_sec, B_sens) shifts effective_grace_hours and grievance sensitivity."""
        st = self.r.dynamics.raw_state(WED_4PM)
        st.update(T_sec=0.60, B_sens=0.05)
        self.s.set("dynamics_state", st)
        snap_sec = self.r.dynamics.advance(WED_4PM + 3.8 * HOUR, waking_gap_hours=3.8)
        self.assertEqual(snap_sec["attachment_style"], "secure_trusting")
        self.assertGreater(snap_sec["effective_grace_hours"], 4.0)
        self.assertEqual(snap_sec["phase"], "attached")

        st2 = self.r.dynamics.default_state(WED_4PM)
        st2.update(T_sec=-0.50, B_sens=0.55)
        self.s.set("dynamics_state", st2)
        snap_guard = self.r.dynamics.advance(WED_4PM + 2.2 * HOUR, waking_gap_hours=2.2)
        self.assertEqual(snap_guard["attachment_style"], "guarded_sensitive")
        self.assertLess(snap_guard["effective_grace_hours"], 2.0)
        self.assertEqual(snap_guard["phase"], "missing")
        self.assertGreater(snap_guard["G"], 0.06)

    async def test_typing_hesitation_cadence_and_rich_media_share_back(self):
        """2. Hesitation typing plan triggers on intimate/guarded turns, and XHS post shares send OneBot image segments."""
        plan_shy = compute_typing_plan(["亲爱的", "抱紧点 别松手"], dynamics={"A": 0.75, "H": 0.2, "phase": "attached"})
        self.assertEqual([s["action"] for s in plan_shy[0]], ["typing", "pause", "typing"])
        self.assertEqual([s["action"] for s in plan_shy[1]], ["typing"])

        plan_normal = compute_typing_plan(["晚上喝砂锅粥吧"], dynamics={"A": 0.70, "H": 0.2, "phase": "attached"})
        self.assertEqual([s["action"] for s in plan_normal[0]], ["typing"])

        # Rich-media share-back from reading a Xiaohongshu post
        with patch("time.time", return_value=WED_4PM):
            aid = self.s.start_activity("browse", "逛老洋房小店")
            xhs_oid = self.s.observe("xiaohongshu", "feed_cafe_01", {
                "tool": "get_feed_detail",
                "source_url": "https://www.xiaohongshu.com/explore/feed_cafe_01",
                "content": {
                    "note": {
                        "noteId": "feed_cafe_01",
                        "type": "normal",
                        "title": "衡山路原木暖光下午茶小店",
                        "desc": "院子里有薄荷和阳光",
                        "imageList": [{"urlDefault": "https://sns-img.xhscdn.com/cafe01.jpg"}],
                    }
                },
            })
            rev = self.s.get("revision")
            self.r.apply_reflection(aid, {
                "impression": "这家原木风小店很适合周末去",
                "updates": [],
                "expression": "刚刷到这家原木风下午茶小店，周末要不要去这家？",
                "share_feed_id": "feed_cafe_01",
                "refs": [xhs_oid],
            }, [xhs_oid], rev)

            rows = self.r.expressions.rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["media_url"], "https://sns-img.xhscdn.com/cafe01.jpg")
            self.assertEqual(rows[0]["share_url"], "https://www.xiaohongshu.com/explore/feed_cafe_01")

            # Mark ready and verify QQ OneBot delivery sends [image, text] segment array
            self.r.expressions.decide(rows[0]["id"], "ready", "分享给他看", rev)
            self.s.set("last_user_at", WED_4PM - 3600)
        sent_bodies = []

        async def fake_post(client_self, url, json=None, headers=None):
            sent_bodies.append((url, json))
            resp = MagicMock()
            resp.is_success = True
            resp.status_code = 200
            resp.content = b'{"status":"ok","retcode":0,"data":{"message_id":9988}}'
            resp.json.return_value = {"status": "ok", "retcode": 0, "data": {"message_id": 9988}}
            return resp

        with patch("httpx.AsyncClient.post", new=fake_post), patch("time.time", return_value=WED_4PM + 60):
            await self.r.expressions.deliver_one(foreground=False)

        msg_calls = [b for u, b in sent_bodies if u.endswith("/send_private_msg")]
        self.assertEqual(len(msg_calls), 1)
        self.assertIsInstance(msg_calls[0]["message"], list)
        self.assertEqual(msg_calls[0]["message"][0]["type"], "image")
        self.assertEqual(msg_calls[0]["message"][0]["data"]["file"], "https://sns-img.xhscdn.com/cafe01.jpg")
        self.assertIn("刚刷到这家原木风下午茶小店", msg_calls[0]["message"][1]["data"]["text"])

    async def test_v06_superseded_commitment_reconciliation(self):
        """v0.6 -> v0.7 migration rescues unexpired user_statement time-waiting promises that were slot-superseded."""
        with patch("time.time", return_value=WED_4PM):
            u_obs = self.s.observe("user", "u_v06", {"text": "这周末带你去喝下午茶买薄荷"})
            legacy_id = "legacy_weekend_promise_01"
            self.s.record("situation", "周末下午茶与窗边薄荷", {
                "content": "他约这周末去原木暖光的老房子小店喝下午茶，顺便买盆薄荷放窗边。等周末到了再说。",
                "state": "superseded",
                "slot": "continuation",
                "expires_at": WED_4PM + 72 * HOUR,
                "waiting_for": "time",
                "evidence_class": "user_statement",
            }, [u_obs], legacy_id)
            self.s.set("commitment_reconciled_v2", 0)
            self.r.life.reconcile_commitments()
            rec = self.r.life.get_record(legacy_id)
            self.assertEqual(rec["payload"]["state"], "waiting")
            self.assertEqual(rec["payload"]["slot"], "commitment")
            self.assertEqual(rec["payload"]["commitment_status"], "scheduled")
            self.assertGreater(rec["payload"]["window_start"], WED_4PM)
            self.assertTrue(self.r.attention.blocked(legacy_id))


if __name__ == "__main__":
    unittest.main()
