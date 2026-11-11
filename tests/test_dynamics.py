import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_soul.runtime import Runtime

# 2026-10-10 12:00 Asia/Shanghai
NOON = 1791604800.0
HOUR = 3600.0


class DynamicsODETests(unittest.IsolatedAsyncioTestCase):
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
            "bridge_owner_ids": ["owner"],
            "sleep_schedule": {"enabled": True, "start": "00:00", "end": "08:00", "timezone": "Asia/Shanghai"},
            "delivery": {
                "mode": "telegram",
                "authorized": True,
                "chat_id": "owner",
                "token_file": str(token),
                "quiet_start": 0,
                "quiet_end": 0,
                "min_interval_seconds": 3600,
                "block_gap_seconds": 120,
                "max_per_block": 4,
                "max_per_day": 32,
            },
        }
        with patch("time.time", return_value=NOON):
            self.r = Runtime(self.cfg)
        self.s = self.r.store

    async def asyncTearDown(self):
        self.s.close()
        self.temp.cleanup()

    async def owner_says(self, text, turn, at):
        with patch("time.time", return_value=at):
            return await self.r.bridge_turn({
                "phase": "start",
                "sender_id": "owner",
                "platform": "telegram",
                "turn_id": turn,
                "user_message": text,
            })

    async def test_underdamped_mood_oscillator_fluctuates_and_responds_to_reading(self):
        """Solves Meta-Collapse (Problem 1): mood (V, P_V, A) oscillates and reacts to reading impulses."""
        valences = []
        bands = set()
        t = NOON
        for h in range(1, 36):
            t = NOON + h * HOUR
            with patch("time.time", return_value=t):
                if h in (4, 5):
                    self.r.dynamics.on_reading_impulse("春天阳光桂花温暖治愈喜欢开心放松", now=t)
                elif h in (14, 15, 16):
                    self.r.dynamics.on_reading_impulse("失业负债焦虑崩溃难受压力孤独", now=t)
                elif h == 17:
                    self.r.dynamics.on_reading_impulse(tool_failed=True, now=t)
                snap = self.r.dynamics.advance(t)
                valences.append(snap["V"])
                bands.add(snap["mood_band"])

        # Valence must have genuine amplitude (not a flat line) and visit multiple mood bands
        self.assertGreater(max(valences) - min(valences), 0.35)
        self.assertGreaterEqual(len(bands), 2)

    async def test_prolonged_silence_undergoes_phase_transitions_and_stops_reach_out(self):
        """Solves Periodic Outreach Collapse (Problem 2):
        attached -> missing -> anxious -> angry -> cold -> detached, with refractory shutdown of reach_out.
        """
        await self.owner_says("换衣服，我要黑丝旗袍", "t0", NOON)
        self.s.add_chat("assistant", "行，穿，等你数到二十进来。")

        visited_phases = []
        outreach_hours = []

        # Simulate 8 days (192 hours) of complete silence from the owner
        for h in range(1, 193):
            now = NOON + h * HOUR
            with patch("time.time", return_value=now):
                self.r.contact.maintenance()
                ctx = self.r.background_context(purpose="attention")
                phase = ctx["dynamics"]["phase"]
                if not visited_phases or visited_phases[-1] != phase:
                    visited_phases.append(phase)

                # Whenever reach_out is offered in candidates, simulate sending a 2-message burst
                if any(c["id"] == "reach_out" for c in ctx["candidates"]):
                    oid = self.r.contact.gap_observations()[0]
                    rev = self.s.get("revision")
                    for seq in range(2):
                        eid = self.r.expressions.create(f"消息{h}_{seq}", [oid], rev, origin="reach_out", block_id=f"b{h}", seq=seq)
                        self.r.expressions.decide(eid, "ready", "想他", rev)
                        self.s.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (eid,))
                        self.s.db.execute("UPDATE expression_meta SET updated=? WHERE id=?", (now, eid))
                    self.r.dynamics.on_reach_out(2, now=now)
                    outreach_hours.append(h)

        # 1. Must progress through missing -> anxious -> angry -> cold -> detached
        for expected in ("missing", "anxious", "angry", "cold", "detached"):
            self.assertIn(expected, visited_phases, f"Missing phase {expected} in {visited_phases}")
        self.assertEqual(visited_phases[-1], "detached")

        # 2. Total outreach bursts over 8 days must be bounded (<= 6 bursts, NOT 124 bursts!)
        self.assertGreaterEqual(len(outreach_hours), 2)
        self.assertLessEqual(len(outreach_hours), 6)

        # 3. Intervals between bursts must lengthen (FitzHugh-Nagumo refractory effect)
        if len(outreach_hours) >= 3:
            first_gap = outreach_hours[1] - outreach_hours[0]
            last_gap = outreach_hours[-1] - outreach_hours[-2]
            self.assertGreater(last_gap, first_gap)

        # 4. In cold/detached phase at day 8, reach_out is completely shut down and stale exchange is suppressed
        with patch("time.time", return_value=NOON + 192 * HOUR):
            att_ctx = self.r.background_context(purpose="attention")
            browse_ctx = self.r.background_context(purpose="browse")
        self.assertNotIn("reach_out", [c["id"] for c in att_ctx["candidates"]])
        self.assertNotIn("last_exchange", browse_ctx["contact"])
        self.assertLess(att_ctx["dynamics"]["S"], -0.70)

    async def test_bistable_hysteresis_resists_perfunctory_apology_or_demand_after_cold_phase(self):
        """Once in the negative attractor basin (cold/detached), a 1-line apology or demand does NOT immediately resolve."""
        await self.owner_says("晚点聊", "t0", NOON)
        # Advance 6 days (144 hours) with unanswered messages so she falls into cold/detached basin
        for h in (4, 10, 26, 52, 80, 110, 144):
            now = NOON + h * HOUR
            with patch("time.time", return_value=now):
                self.r.contact.maintenance()
                if h in (4, 10, 26, 52):
                    oid = self.r.contact.gap_observations()[0]
                    eid = self.r.expressions.create(f"催促{h}", [oid], 0, origin="reach_out")
                    self.s.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (eid,))
                    self.s.db.execute("UPDATE expression_meta SET updated=? WHERE id=?", (now, eid))
                    self.r.dynamics.on_reach_out(2, now=now)

        oid = self.r.contact.gap_observations()[0]
        obs_text = self.s.db.execute("SELECT json_extract(payload,'$.text') FROM observations WHERE id=?", (oid,)).fetchone()[0]
        with patch("time.time", return_value=NOON + 144 * HOUR):
            rid = self.r.life.save(self.r.life.prepare({
                "kind": "affect",
                "label": "心冷与疏离",
                "content": "这么久不理我，心已经冷了",
                "trigger_quote": obs_text[:16],
                "scope": "和他的联系",
                "refs": [oid],
            }, {oid}, "a1"))
            # Also verify she is allowed to update relationship & boundary from host observation in cold/detached phase!
            rel_id = self.r.life.save(self.r.life.prepare({
                "kind": "relationship",
                "label": "关系降温",
                "content": "他长期无视我的消息，我不再单方面苦等",
                "observed_event": "连续多日失联未回",
                "refs": [oid],
            }, {oid}, "a1"))
            self.assertTrue(rel_id)

        # Owner returns at 145h with a perfunctory 4-char greeting "在的在的"
        await self.owner_says("在的在的", "t1", NOON + 145 * HOUR)
        self.assertEqual(self.r.life.get_record(rid)["payload"]["state"], "active")
        self.assertTrue(self.r.dynamics.raw_state()["hysteresis_locked"])
        self.assertLess(self.r.dynamics.raw_state()["S"], 0.0)

        # Next turn: owner makes an abrupt demand "换衣服，我要黑丝旗袍" -> S drops further, still locked!
        await self.owner_says("换衣服，我要黑丝旗袍", "t2", NOON + 145.1 * HOUR)
        self.assertEqual(self.r.life.get_record(rid)["payload"]["state"], "active")
        self.assertTrue(self.r.dynamics.raw_state()["hysteresis_locked"])

        # Sustained, sincere multi-turn communication gradually crosses the saddle barrier and unlocks
        for idx in range(5):
            await self.owner_says(
                "真的对不起，这几天让你一个人等了这么久，我知道你很委屈也很生气，今晚我好好陪你把话聊开，抱抱你",
                f"t_repair_{idx}",
                NOON + (145.5 + idx * 0.2) * HOUR,
            )
        self.assertFalse(self.r.dynamics.raw_state()["hysteresis_locked"])
        self.assertGreater(self.r.dynamics.raw_state()["S"], 0.35)
        self.assertEqual(self.r.life.get_record(rid)["payload"]["state"], "resolved")

    async def test_compartmental_desensitization_and_curiosity_limit_cycle_in_detached_state(self):
        """Optimization 1: Prolonged detached phase does NOT lock personal mood V into perpetual low_melancholy,
        and rest <-> reading forms a self-sustaining curiosity limit cycle with discrete topic spillover.
        """
        # Seed two personal interests (related by shared bigram '水彩') and one relational topic
        xhs1 = self.s.observe("xiaohongshu", "n1", {"text": "水彩风景速写", "tool": "get_feed_detail"})
        xhs2 = self.s.observe("xiaohongshu", "n2", {"text": "水彩调色盘入门", "tool": "get_feed_detail"})
        rel_obs = self.s.observe("user", "u1", {"text": "我们的恋爱日常"})
        with patch("time.time", return_value=NOON):
            i1 = self.r.life.save(self.r.life.prepare({"kind": "interest", "label": "水彩速写", "content": "喜欢水彩速写", "refs": [xhs1]}, {xhs1}, "a1"))
            i2 = self.r.life.save(self.r.life.prepare({"kind": "interest", "label": "水彩调色", "content": "想试水彩调色", "refs": [xhs2]}, {xhs2}, "a2"))
            # Make i2 slightly older so i1 diffuses associative activation onto i2
            self.s.db.execute("UPDATE records SET updated=? WHERE id=?", (NOON - 3600, i2))
            rel_topic = self.r.life.save(self.r.life.prepare({"kind": "situation", "label": "关于我们的恋爱纪念日", "content": "等他商量纪念日", "ttl_hours": 96, "refs": [rel_obs]}, {rel_obs}, "a3"))

        # Place system into legacy pinned detached + low_melancholy state (simulating Nov 2026 live deadlock)
        self.s.set("rest_streak", 81)
        self.s.set("dynamics_state", {
            "S": -0.95, "G": 3.0, "I": -1.4, "W": 2.3,
            "V": -0.35, "P_V": -0.05, "A": 0.42, "eta": -0.20,
            "phase": "detached", "mood_band": "low_melancholy",
            "bursts_since_reply": 5, "hysteresis_locked": True,
            "updated_at": NOON,
        })

        # 1. One-time compartment migration immediately releases scalar V lock & clears rest_streak
        with patch("time.time", return_value=NOON + HOUR):
            migrated = self.r.dynamics.raw_state(NOON + HOUR)
            self.assertTrue(migrated["compartment_migrated"])
            self.assertEqual(self.s.get("rest_streak"), 0)
            self.assertGreater(migrated["V"], -0.15)

        # 2. Simulate 48 more hours of owner silence in detached state: V oscillates autonomously, NOT pinned <= -0.22
        valences = []
        for h in range(2, 50):
            t = NOON + h * HOUR
            with patch("time.time", return_value=t):
                snap = self.r.dynamics.advance(t)
                valences.append(snap["V"])
        self.assertEqual(snap["phase"], "detached")
        self.assertGreater(max(valences), 0.05)
        self.assertLess(sum(1 for v in valences if v <= -0.22), len(valences) // 2)

        # 3. Rest <-> reading curiosity limit cycle
        t_cycle = NOON + 50 * HOUR
        with patch("time.time", return_value=t_cycle):
            self.r.dynamics.on_rest(now=t_cycle)
            after_rest = self.r.dynamics.on_rest(now=t_cycle + 0.5 * HOUR)
            self.assertGreaterEqual(after_rest["C"], 0.68)
            self.assertEqual(after_rest["curiosity_band"], "eager_curious")

            att_ctx = self.r.background_context(purpose="attention")
            self.assertIn("curiosity_band", att_ctx["dynamics"])
            self.assertIn("S_bar", att_ctx["dynamics"])
            self.assertIn("自己的兴趣", att_ctx["dynamics"]["phase_guidance"])
            chat_ctx = self.r.background_context(purpose="chat")
            self.assertIn("疏离边界", chat_ctx["dynamics"]["phase_guidance"])

            # Discrete topic field: relational topic is inhibited below independent interests, and i2 gets associative boost
            by_id = {c["id"]: c for c in att_ctx["candidates"]}
            self.assertGreater(by_id[i1]["score"], by_id[rel_topic]["score"])
            self.assertGreater(by_id[i2]["score"], by_id[rel_topic]["score"])
            self.assertIn("探索欲", by_id["discover"]["selection_basis"])

            # Reading a post satisfies curiosity C
            after_read = self.r.dynamics.on_reading_impulse("水彩晕染技巧很实用", now=t_cycle + HOUR)
            self.assertLess(after_read["C"], after_rest["C"] - 0.15)

    async def test_hysteresis_protects_contact_return_ledger_and_grievance_across_short_pings(self):
        """Optimization 3: Repeated drive-by short pings during hysteresis_locked preserve initial gap ledger and G."""
        await self.owner_says("晚点说", "t0", NOON)
        # Advance 144h with 3 unanswered outreach messages into cold/detached basin
        for h in (4, 10, 26, 52, 80, 110, 144):
            now = NOON + h * HOUR
            with patch("time.time", return_value=now):
                self.r.contact.maintenance()
                if h in (4, 10, 26):
                    oid = self.r.contact.gap_observations()[0]
                    eid = self.r.expressions.create(f"在吗{h}", [oid], 0, origin="reach_out")
                    self.s.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (eid,))
                    self.s.db.execute("UPDATE expression_meta SET updated=? WHERE id=?", (now, eid))
                    self.r.dynamics.on_reach_out(1, now=now)

        # First short ping at 145h: enters/remains hysteresis_locked
        await self.owner_says("小安", "p1", NOON + 145 * HOUR)
        ret1 = self.s.get("contact_return")
        self.assertTrue(self.r.dynamics.raw_state()["hysteresis_locked"])
        self.assertGreater(ret1["hours_since"], 130.0)
        self.assertEqual(ret1["unanswered_sent"], 3)
        g_after_p1 = self.r.dynamics.raw_state()["G"]

        # Wait 2 hours (inside GRACE_HOURS = 3h): G must NOT passively decay while hysteresis_locked!
        with patch("time.time", return_value=NOON + 147 * HOUR):
            snap_mid = self.r.dynamics.advance(NOON + 147 * HOUR)
        self.assertGreaterEqual(snap_mid["G"], g_after_p1 - 0.01)

        # Second drive-by short ping at 147.5h: contact_return must still preserve original hours_since & unanswered_sent
        await self.owner_says("小安", "p2", NOON + 147.5 * HOUR)
        ret2 = self.s.get("contact_return")
        self.assertTrue(self.r.dynamics.raw_state()["hysteresis_locked"])
        self.assertGreater(ret2["hours_since"], 130.0)
        self.assertEqual(ret2["unanswered_sent"], 3)
        self.assertGreaterEqual(ret2["repair_turns"], 2)


if __name__ == "__main__":
    unittest.main()
