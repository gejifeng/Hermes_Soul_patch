from datetime import datetime
from zoneinfo import ZoneInfo
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from hermes_soul.schedule import sleep_until, next_allowed
from hermes_soul.runtime import Runtime, StaleActivity


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'sleep_schedule': {'enabled': True, 'start': '00:00', 'end': '08:00', 'timezone': 'Asia/Shanghai'}}

    def test_midnight_inclusive_and_morning_exclusive(self):
        end = stamp('2026-09-16T08:00')
        for hour in ('00:00', '04:30', '07:59:59'):
            self.assertEqual(sleep_until(self.cfg, stamp('2026-09-16T'+hour)), end)
        for hour in ('08:00', '12:00', '23:59:59'):
            self.assertIsNone(sleep_until(self.cfg, stamp('2026-09-16T'+hour)))

    def test_cross_midnight_and_next_day(self):
        self.cfg['sleep_schedule']['start'] = '23:00'
        self.assertEqual(sleep_until(self.cfg, stamp('2026-12-31T23:30')), stamp('2027-01-01T08:00'))
        self.assertEqual(sleep_until(self.cfg, stamp('2027-01-01T07:00')), stamp('2027-01-01T08:00'))

    def test_disabled_and_scheduled_wake_adjustment(self):
        target = stamp('2026-09-16T00:05')
        self.assertEqual(next_allowed(self.cfg, target), stamp('2026-09-16T08:00'))
        self.assertEqual(next_allowed({}, target), target)
        self.cfg['sleep_schedule']['enabled'] = False
        self.assertIsNone(sleep_until(self.cfg, target))

    def test_invalid_enabled_configuration(self):
        self.cfg['sleep_schedule']['end'] = '00:00'
        with self.assertRaises(ValueError):
            sleep_until(self.cfg)


class RuntimeSleepTests(unittest.IsolatedAsyncioTestCase):
    async def test_sleep_skips_activity_and_delivery_but_keeps_chat_available(self):
        with tempfile.TemporaryDirectory() as home:
            cfg={'home':home,'mcp_url':'http://127.0.0.1:1/mcp','interval_seconds':720,
                 'sleep_schedule':{'enabled':True,'start':'00:00','end':'08:00','timezone':'Asia/Shanghai'}}
            r=Runtime(cfg)
            try:
                r.model=AsyncMock(return_value='{"reply":"我在，宝宝。","boundary":null}')
                with patch('time.time',return_value=stamp('2026-09-16T03:00')):
                    await r.tick()
                    r.model.assert_not_awaited()
                    self.assertEqual(r.store.rows('activities'),[])
                    self.assertEqual(r.status()['next_wake'],stamp('2026-09-16T08:00'))
                    self.assertTrue(r.status()['sleeping'])
                    self.assertIn('睡眠',r.expressions.delivery_block())
                    with self.assertRaises(StaleActivity):
                        r.check_revision(r.store.get('revision'))
                    self.assertEqual(await r.chat('在吗？'),'我在，宝宝。')
                with patch('time.time',return_value=stamp('2026-09-16T08:00')):
                    self.assertFalse(r.status()['sleeping'])
                    r.check_revision(r.store.get('revision'))
            finally:r.store.close()
