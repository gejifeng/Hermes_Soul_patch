import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock

from hermes_soul.runtime import Runtime, StaleActivity
from hermes_soul.mcp import Xiaohongshu, MCPError, feed_cards
from hermes_soul.store import Store


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.s = Store(self.temp.name)

    def tearDown(self):
        self.s.close()
        self.temp.cleanup()

    def test_second_state_owner_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "已有运行实例"):
            Store(self.temp.name)

    def test_evidence_required_and_recovery_preserves_read_progress(self):
        with self.assertRaises(ValueError):
            self.s.record("belief", "claim", {}, ["invented"])
        oid = self.s.observe("xiaohongshu", "a", {"text": "作者声称有效"})
        self.assertEqual(oid, self.s.observe("xiaohongshu", "a", {"text": "作者声称有效"}))
        aid = self.s.start_activity("browse", "喜欢")
        self.s.checkpoint(aid, {"refs": [oid], "opened": ["a"]})
        self.s.finish_activity(aid, "interrupted", {"reason": "新消息"})
        self.s.recover()
        self.assertEqual(self.s.rows("activities")[0]["checkpoint"]["refs"], [oid])
        self.s.record("interest", "猫", {"state": "active"}, [oid])
        self.s.close()
        self.s = Store(self.temp.name)
        self.assertEqual(self.s.rows("records")[0]["label"], "猫")

    def test_background_budget_survives_restart_and_does_not_block_chat(self):
        self.s.reserve_call("attention", 1)
        self.s.close()
        self.s = Store(self.temp.name)
        with self.assertRaises(RuntimeError):
            self.s.reserve_call("attention", 1)
        self.s.reserve_call("chat", 1, foreground=True)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.r = Runtime({"home": self.temp.name, "mcp_url": "http://127.0.0.1:1/mcp", "interval_seconds": 900,
                          "idle_max_seconds": 3600, "max_calls_per_day": 10, "max_browse_steps": 3,
                          "bridge_owner_ids": ["owner"]})

    async def asyncTearDown(self):
        self.r.store.close()
        self.temp.cleanup()

    async def test_interest_browse_reads_actual_card_can_stop_without_output(self):
        outputs = [json.dumps({"activity": "browse", "reason": "随意看看"}),
                   json.dumps({"action": "read", "feed_id": "cat"}),
                   json.dumps({"action": "stop", "reason": "看够了"}),
                   json.dumps({"updates": [], "impression": "", "journal": "", "expression": ""})]
        self.r.model = AsyncMock(side_effect=outputs)
        self.r.login_status = AsyncMock(return_value={"logged_in": True})
        def result(obj):
            return {"content": [{"type": "text", "text": json.dumps(obj)}]}
        self.r.mcp.call = AsyncMock(side_effect=[result({"feeds": [{"id": "cat", "xsecToken": "t", "noteCard": {"displayTitle": "猫", "type": "normal"}}]}),
                                                result({"note": {"text": "这是一只猫"}, "comments": []})])
        await self.r.tick()
        self.assertEqual([c.args[0] for c in self.r.mcp.call.call_args_list], ["list_feeds", "get_feed_detail"])
        self.assertEqual(self.r.store.rows("activities")[0]["status"], "ended")
        self.assertEqual(self.r.store.rows("records"), [])
        self.assertEqual(len(self.r.store.rows("observations")), 2)
        self.assertFalse(self.r.store.rows("observations")[0]["payload"]["media_understood"])

    async def test_user_correction_fences_old_updates_atomically(self):
        oid = self.r.store.observe("user", "x", {"text": "先看看"})
        revision = self.r.store.get("revision")
        self.r.store.bump()
        with self.assertRaises(StaleActivity):
            self.r.apply_reflection("a", {"updates": [], "journal": "旧判断"}, [oid], revision)
        self.assertEqual(self.r.store.rows("records"), [])
        with self.assertRaises(ValueError):
            self.r.apply_reflection("a", {"updates": [{"kind": "interest", "label": "猫", "content": "喜欢", "refs": [oid]},
                                      {"kind": "belief", "label": "假", "content": "假", "refs": ["bad"]}]},
                                      [oid], self.r.store.get("revision"))
        self.assertEqual(self.r.store.rows("records"), [])

    async def test_recommendations_and_search_only_offer_image_text_posts(self):
        def listing(prefix):
            return {"content": [{"type": "text", "text": json.dumps({"feeds": [
                {"id": prefix + kind, "xsecToken": "token", "noteCard": {"type": kind}}
                for kind in ("video", "unknown", "normal")
            ]})}]}
        self.r.mcp.call = AsyncMock(side_effect=[listing("feed-"), listing("search-")])
        self.r.model = AsyncMock(side_effect=['{"action":"search","query":"公园"}', '{"action":"stop"}'])
        aid = self.r.store.start_activity("browse", "看看")
        await self.r.browse(aid, {}, 0)
        offered = [c.args[2]["cards"] for c in self.r.model.call_args_list]
        self.assertEqual([[c["id"] for c in cards] for cards in offered], [["feed-normal"], ["search-normal"]])
        self.assertEqual([c.args[0] for c in self.r.mcp.call.call_args_list], ["list_feeds", "search_feeds"])

    async def test_model_cannot_open_filtered_video_or_unknown_post(self):
        for kind in ("video", "unknown"):
            with self.subTest(kind=kind):
                self.r.mcp.call = AsyncMock(return_value={"content": [{"type": "text", "text": json.dumps({
                    "feeds": [{"id": "blocked", "xsecToken": "token", "noteCard": {"type": kind}}]
                })}]})
                self.r.model = AsyncMock(return_value='{"action":"read","feed_id":"blocked"}')
                aid = self.r.store.start_activity("browse", "看看")
                with self.assertRaises(ValueError):
                    await self.r.browse(aid, {}, 0)
                self.assertEqual(self.r.model.call_args.args[2]["cards"], [])
                self.r.mcp.call.assert_awaited_once_with("list_feeds", {})
    async def test_bridge_owner_scope_idempotence_and_context(self):
        payload = {"phase": "start", "sender_id": "other", "platform": "telegram", "turn_id": "t", "user_message": "hi"}
        with self.assertRaises(ValueError):
            await self.r.bridge_turn(payload)
        payload["sender_id"] = "owner"
        first = await self.r.bridge_turn(payload)
        await self.r.bridge_turn(payload)
        self.assertEqual(len(self.r.store.rows("observations")), 1)
        self.assertIn("context", first)
        await self.r.bridge_turn({**payload, "phase": "finish", "assistant_response": "hello"})
        await self.r.bridge_turn({**payload, "phase": "finish", "assistant_response": "hello"})
        self.assertEqual(len(self.r.store.rows("chats")), 2)
        self.assertFalse(self.r.bridge_busy())

    async def test_login_failure_never_becomes_a_browsing_experience(self):
        self.r.model = AsyncMock(return_value='{"activity":"browse"}')
        self.r.login_status = AsyncMock(return_value={"logged_in": False})
        self.r.mcp.call = AsyncMock()
        await self.r.tick()
        self.assertEqual(self.r.store.rows("activities")[0]["status"], "waiting_login")
        self.assertEqual(self.r.store.rows("observations"), [])
        self.r.mcp.call.assert_not_called()

    async def test_publishing_denied_before_network(self):
        mcp = Xiaohongshu("http://127.0.0.1:1/mcp")
        for name in ("publish_content", "post_comment_to_feed", "like_feed", "delete_cookies"):
            with self.assertRaises(MCPError):
                await mcp.call(name)

    async def test_same_evidence_does_not_drive_repeated_reflection(self):
        oid = self.r.store.observe("xiaohongshu", "cat", {"text": "猫", "artifact": "a.json"})
        self.assertEqual(oid, self.r.store.observe("xiaohongshu", "cat", {"text": "猫", "artifact": "b.json"}))
        self.r.model = AsyncMock(return_value='{"updates":[],"journal":"","expression":""}')
        await self.r.reflect("a", [oid], 0)
        await self.r.reflect("b", [oid], 0)
        self.r.model.assert_awaited_once()
        self.assertEqual(self.r.store.rows("records"), [])

    async def test_apply_reflection_repairs_bad_id_and_isolates_quote_mismatch(self):
        """Optimization 2: Stale/wrong-kind id falls back to label matching, and paraphrased affect quote is skipped
        individually without aborting valid updates or journal.
        """
        oid = self.r.store.observe("xiaohongshu", "n1", {"text": "桂花拿铁配方很香", "tool": "get_feed_detail"})
        belief_id = self.r.store.record("belief", "咖啡", {"content": "喜欢热饮", "state": "active"}, [oid])
        aid = self.r.store.start_activity("browse", "看看")
        self.r.apply_reflection(aid, {
            "impression": "闻到桂花香的感觉",
            "journal": "今天记下了桂花拿铁的做法。",
            "updates": [
                # Wrong-kind id (points to a belief instead of interest) -> repaired & saved!
                {"id": belief_id, "kind": "interest", "label": "桂花拿铁", "content": "想试试桂花拿铁", "refs": [oid]},
                # Paraphrased trigger_quote not verbatim in observation -> skipped individually!
                {"kind": "affect", "label": "嘴馋", "content": "想喝咖啡", "trigger_quote": "不存在的改写原文", "scope": "咖啡", "refs": [oid]},
            ],
        }, [oid], self.r.store.get("revision"))
        kinds = {r["kind"]: r for r in self.r.store.rows("records")}
        self.assertIn("interest", kinds)
        self.assertIn("journal", kinds)
        self.assertIn("episode", kinds)
        self.assertNotIn("affect", kinds)
        step_statuses = [(s["stage"], s["status"]) for s in self.r.store.steps(aid)]
        self.assertIn(("update", "repaired"), step_statuses)
        self.assertIn(("update", "skipped"), step_statuses)

    async def test_failed_tick_does_not_inflate_rest_streak_and_reconciles_interests_and_storage(self):
        """Optimizations 4 & 5: Failed wakeups do not inflate rest_streak; corrupted preferred interests are reconciled;
        prune_storage cleans backoff/bridge/call-dir bloat while preserving observation JSON artifacts.
        """
        # 1. Failed tick does not increment rest_streak
        self.r.model = AsyncMock(side_effect=RuntimeError("model timeout"))
        await self.r.tick()
        self.assertEqual(self.r.store.rows("activities")[0]["status"], "failed")
        self.assertEqual(self.r.store.get("rest_streak", 0), 0)

        # 2. Reconcile bulk-inflated preferred interest (1 ref, no visit_activities) back to noticed
        oid = self.r.store.observe("xiaohongshu", "n2", {"text": "阳台薄荷", "tool": "get_feed_detail"})
        rid = self.r.store.record("interest", "薄荷", {"content": "薄荷", "state": "preferred", "evidence_count": 1}, [oid])
        self.r.store.set("interest_reconciled_v1", 0)
        self.r.life.reconcile_interests()
        self.assertEqual(self.r.life.get_record(rid)["payload"]["state"], "noticed")

        # 3. Storage hygiene: tool_backoff deleted on success; old call scratch dirs & zombie keys pruned, JSON kept
        aid = self.r.store.start_activity("browse", "清理测试")
        self.r.mcp.call = AsyncMock(side_effect=[MCPError("context deadline exceeded"),
                                                 {"content": [{"type": "text", "text": '{"feeds":[]}'}]}])
        with self.assertRaises(MCPError):
            await self.r.mcp_observation(aid, "list_feeds", {})
        backoff_keys = [r[0] for r in self.r.store.db.execute("SELECT key FROM meta WHERE key LIKE 'tool_backoff:%'").fetchall()]
        self.assertEqual(len(backoff_keys), 1)
        # Expire backoff and succeed -> key is deleted completely
        self.r.store.set(backoff_keys[0], {"attempts": 1, "until": 1})
        await self.r.mcp_observation(aid, "list_feeds", {})
        self.assertEqual(self.r.store.db.execute("SELECT count(*) FROM meta WHERE key LIKE 'tool_backoff:%'").fetchone()[0], 0)

        # Create old scratch dir vs JSON evidence file + 205 finished bridge keys
        art_dir = self.r.store.home / "artifacts"
        old_scratch = art_dir / "deadbeef1234"
        old_scratch.mkdir(parents=True, exist_ok=True)
        evidence_json = art_dir / "keep_evidence.json"
        evidence_json.write_text('{"ok":true}')
        import os
        old_ts = 1700000000
        os.utime(old_scratch, (old_ts, old_ts))
        os.utime(evidence_json, (old_ts, old_ts))
        self.r.store.set("tool_backoff:empty", {})
        for idx in range(205):
            self.r.store.set(f"bridge:t:{idx:04d}", {"done": True, "observation_id": f"o{idx}"})
        stats = self.r.store.prune_storage(artifact_retention_days=14, now=1800000000)
        self.assertFalse(old_scratch.exists())
        self.assertTrue(evidence_json.exists())
        self.assertGreaterEqual(stats["removed_meta"], 6)
        self.assertEqual(stats["removed_artifact_dirs"], 1)


if __name__ == "__main__":
    unittest.main()
