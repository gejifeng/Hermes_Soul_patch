import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from hermes_soul.runtime import Runtime

# 2026-10-10 12:00 Asia/Shanghai
NOON = 1791604800.0
HOUR = 3600


class V04ContactTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        token = Path(self.temp.name) / "fake-token"
        token.write_text("fake")
        self.cfg = {'home': self.temp.name, 'mcp_url': 'http://127.0.0.1:1/mcp', 'interval_seconds': 720,
                    'idle_max_seconds': 720, 'max_calls_per_day': 120, 'max_browse_steps': 3,
                    'bridge_owner_ids': ['owner'],
                    'sleep_schedule': {'enabled': True, 'start': '00:00', 'end': '08:00', 'timezone': 'Asia/Shanghai'},
                    'delivery': {'mode': 'telegram', 'authorized': True, 'chat_id': 'owner', 'token_file': str(token),
                                 'quiet_start': 0, 'quiet_end': 0, 'min_interval_seconds': 3600,
                                 'block_gap_seconds': 120, 'max_per_block': 4, 'max_per_day': 32}}
        self.r = Runtime(self.cfg)
        self.s = self.r.store

    async def asyncTearDown(self):
        self.s.close()
        self.temp.cleanup()

    async def owner_says(self, text, turn, at):
        with patch('time.time', return_value=at):
            return await self.r.bridge_turn({"phase": "start", "sender_id": "owner", "platform": "telegram",
                                             "turn_id": turn, "user_message": text})

    def gap_at(self, at):
        with patch('time.time', return_value=at):
            return self.r.contact.maintenance()

    async def test_silence_becomes_host_fact_once_per_level_and_sleep_does_not_count(self):
        await self.owner_says('晚安，明天聊', 't1', NOON + 11 * HOUR)  # 23:00
        self.assertIsNone(self.gap_at(NOON + 13 * HOUR))  # 01:00, asleep
        # 09:00 next day: 1h before midnight + 1h after 08:00 awake; not yet 3 waking hours.
        self.assertIsNone(self.gap_at(NOON + 21 * HOUR))
        oid = self.gap_at(NOON + 23 * HOUR)  # 11:00 -> 4 waking hours
        self.assertIsNotNone(oid)
        self.assertIsNone(self.gap_at(NOON + 23.5 * HOUR))
        row = self.s.db.execute('SELECT source,payload FROM observations WHERE id=?', (oid,)).fetchone()
        self.assertEqual(row['source'], 'host')
        self.assertIn('没有新消息', json.loads(row['payload'])['text'])
        self.assertIn(oid, self.s.pending_observations())
        with patch('time.time', return_value=NOON + 29 * HOUR):
            nxt = self.r.contact.maintenance()
            state = self.r.background_context(purpose='attention')
        self.assertNotEqual(nxt, oid)
        self.assertEqual(state['contact']['gap_level'], 8)
        self.assertIn('reach_out', [c['id'] for c in state['candidates']])
        self.assertIn('reach_out', state['available_activities'])

    async def test_no_contact_history_offers_no_reach_out(self):
        ctx = self.r.background_context(purpose='attention')
        self.assertNotIn('reach_out', [c['id'] for c in ctx['candidates']])
        self.assertIsNone(self.r.contact.maintenance())

    async def test_missing_him_lasts_past_a_day_and_ends_when_he_returns(self):
        await self.owner_says('在忙，晚点说', 't1', NOON)
        self.gap_at(NOON + 9 * HOUR)
        oid = self.r.contact.gap_observations()[0]
        text = json.loads(self.s.db.execute('SELECT payload FROM observations WHERE id=?', (oid,)).fetchone()[0])['text']
        with patch('time.time', return_value=NOON + 9 * HOUR):
            prepared = self.r.life.prepare({'kind': 'affect', 'label': '想他', 'content': '他一直不回，有点委屈',
                                            'trigger_quote': text[:20], 'scope': '和他的联系', 'refs': [oid]}, {oid}, 'a')
            rid = self.r.life.save(prepared)
        with patch('time.time', return_value=NOON + 40 * HOUR):
            self.r.life.maintenance()
            ctx = self.r.background_context('阳台')
        self.assertEqual(self.r.life.get_record(rid)['payload']['state'], 'active')
        self.assertEqual(ctx['feelings'][0]['id'], rid)
        await self.owner_says('我回来啦', 't2', NOON + 41 * HOUR)
        self.assertEqual(self.r.life.get_record(rid)['payload']['state'], 'resolved')
        with patch('time.time', return_value=NOON + 41 * HOUR):
            returned = self.r.contact.state()['owner_just_returned']
        self.assertGreaterEqual(returned['hours_since'], 41)

    async def test_reach_out_creates_ready_block_backed_by_gap_and_is_dropped_on_return(self):
        await self.owner_says('出门了', 't1', NOON)
        self.gap_at(NOON + 4 * HOUR)
        self.r.model = AsyncMock(side_effect=[
            '{"impression":"","updates":[]}',
            '{"activity":"reach_out","focus_id":"reach_out","reason":"想他了"}',
            '{"messages":["在吗？","你在干啥呢","都不理我"],"reason":"好久没消息"}'])
        with patch('time.time', return_value=NOON + 4 * HOUR):
            await self.r.tick()
        rows = [r for r in self.r.expressions.rows() if r['origin'] == 'reach_out']
        self.assertEqual(sorted(r['block_seq'] for r in rows), [0, 1, 2])
        self.assertEqual(len({r['block_id'] for r in rows}), 1)
        self.assertTrue(all(r['status'] == 'ready' and r['refs'] == self.r.contact.gap_observations() for r in rows))
        with patch('time.time', return_value=NOON + 4.5 * HOUR):
            self.assertNotIn('reach_out', [c['id'] for c in self.r.background_context(purpose='attention')['candidates']])
            self.assertEqual(await self.r.reach_out('again', self.s.get('revision')), [])
        self.assertTrue(all(r['expires'] <= NOON + 6 * HOUR for r in rows))
        await self.owner_says('刚才在开会', 't2', NOON + 5 * HOUR)
        self.assertTrue(all(r['status'] == 'discarded' for r in self.r.expressions.rows() if r['origin'] == 'reach_out'))

    async def test_reach_out_needs_recorded_gap(self):
        with self.assertRaises(ValueError):
            await self.r.reach_out('a', self.s.get('revision'))

    async def test_burst_pacing_inside_block_and_between_blocks(self):
        x = self.r.expressions
        oid = self.s.observe('host', 'gap', {'text': '很久没消息', 'scope': 'contact_gap'})
        rev = self.s.get('revision')

        def block(texts, at):
            with patch('time.time', return_value=at):
                ids = [x.create(t, [oid], rev, origin='reach_out', block_id='b%d' % at, seq=i) for i, t in enumerate(texts)]
                for eid in ids:
                    x.decide(eid, 'ready', '想他', rev)

        block(['在吗？', '你在干啥呢', '人呢', '理理我', '第五条'], NOON)
        response = httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        sent = []
        with patch.object(x, 'delivery_block', return_value=None), \
             patch('httpx.AsyncClient.post', new=AsyncMock(return_value=response)) as post:
            for minute in (0, 1, 2, 4, 6, 8, 10):
                with patch('time.time', return_value=NOON + minute * 60):
                    await x.deliver_one()
                sent.append(post.await_count)
            # Same-block follow-ups need only 2 minutes; the fifth exceeds the 4-per-block cap.
            self.assertEqual(sent, [1, 1, 2, 3, 4, 4, 4])
            self.assertEqual([c.kwargs['json']['text'] for c in post.await_args_list], ['在吗？', '你在干啥呢', '人呢', '理理我'])
            block(['还是没回我'], NOON + 20 * 60)
            with patch('time.time', return_value=NOON + 30 * 60):
                await x.deliver_one()
            self.assertEqual(post.await_count, 4)
            with patch('time.time', return_value=NOON + 6 * 60 + HOUR + 1):
                await x.deliver_one()
            self.assertEqual(post.await_args.kwargs['json']['text'], '还是没回我')
        with patch('time.time', return_value=NOON + 2 * HOUR):
            recent = self.r.contact.state()['recent_proactive_messages']
        self.assertEqual([m['text'] for m in recent], ['在吗？', '你在干啥呢', '人呢', '理理我', '还是没回我'])

    async def test_draft_mode_says_he_has_not_seen_anything(self):
        self.r.cfg['delivery'] = {'mode': 'draft'}
        await self.owner_says('拜拜', 't1', NOON)
        with patch('time.time', return_value=NOON + 9 * HOUR):
            self.r.contact.maintenance()
            ctx = self.r.contact.state()
        self.assertEqual(ctx['delivery'], 'draft')
        self.assertIn('没有看到', ctx['delivery_note'])
        self.assertEqual(ctx['unanswered_sent'], 0)


if __name__ == '__main__':
    unittest.main()
