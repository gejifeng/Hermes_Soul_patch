"""Daily local-time sleep windows; start inclusive, end exclusive."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time


def sleep_until(config, now=None):
    rule = config.get('sleep_schedule', {})
    if not rule.get('enabled', False):
        return None
    zone = ZoneInfo(rule.get('timezone', 'Asia/Shanghai'))
    start = datetime.strptime(rule['start'], '%H:%M').time()
    end = datetime.strptime(rule['end'], '%H:%M').time()
    if start == end:
        raise ValueError('睡眠开始和结束时间必须不同')
    local = datetime.fromtimestamp(time.time() if now is None else now, zone)
    current = local.time().replace(tzinfo=None)
    inside = start <= current < end if start < end else current >= start or current < end
    if not inside:
        return None
    end_date = local.date() + timedelta(days=int(start > end and current >= start))
    return datetime.combine(end_date, end, zone).timestamp()


def next_allowed(config, timestamp):
    return sleep_until(config, timestamp) or timestamp
