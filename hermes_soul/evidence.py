"""Small, attributed observations; original tool responses remain in the audit store."""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import re


def date_text(value):
    try:
        number = float(value)
        if number > 1e11:
            number /= 1000
        return datetime.fromtimestamp(number, ZoneInfo('Asia/Shanghai')).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def compact_content(tool, data):
    if not isinstance(data, dict):
        return {'text': str(data)[:3000]}
    if tool == 'get_feed_detail':
        note = data.get('data', {}).get('note', data.get('note', {}))
        body = str(note.get('desc', ''))
        comments = data.get('data', {}).get('comments', {})
        comments = comments.get('list', []) if isinstance(comments, dict) else comments
        return {'post_id': note.get('noteId', data.get('feed_id')), 'type': note.get('type'),
                'title': note.get('title', ''), 'body': body[:4500],
                'author': note.get('user', {}).get('nickname', ''),
                'published_at': date_text(note.get('time')),
                'source_claim': '作者声称或转载，独立核实状态未知',
                'reposted': any(w in body+str(note.get('title','')) for w in ('转载', '转自', '摘自')),
                'applicable_dates': re.findall(r'\d{1,4}[年./月-]\d{1,2}(?:[月./-]\d{1,2}日?)?|本周末|下周末|双休日|明天|周[一二三四五六日天]', body)[:25],
                'temporal_status': '适用日期需结合发布时间核对',
                'comments': [{'text': str(x.get('content', ''))[:240], 'published_at': date_text(x.get('createTime'))}
                             for x in (comments or [])[:4] if isinstance(x, dict)],
                'media_understood': False}
    feeds = data.get('feeds', data.get('items', [])) or []
    return {'cards': [{'post_id': x.get('id'), 'title': x.get('noteCard', {}).get('displayTitle', ''),
                       'type': x.get('noteCard', {}).get('type'),
                       'author': x.get('noteCard', {}).get('user', {}).get('nickname', '')}
                      for x in feeds[:20] if isinstance(x, dict) and x.get('noteCard', {}).get('type') == 'normal'],
            'evidence_scope': '列表标题，仅代表发现候选；详情需实际读取'}


def normalized(row):
    r = dict(row)
    p = json.loads(r['payload']) if isinstance(r['payload'], str) else r['payload']
    base = {'observation_id': r['id'], 'source': r['source'], 'observed_at': date_text(r['created'])}
    if r['source'] == 'user':
        return {**base, 'speaker': 'user', 'text': p.get('text', p.get('description', ''))[:4000],
                'scope': p.get('scope'), 'record_id': p.get('record_id')}
    if r['source'] in {'host', 'self_action'}:
        # Host-stated facts: how long the owner has been silent, or what she actually did in public.
        return {**base, 'speaker': 'host_clock' if r['source'] == 'host' else 'self_action_receipt',
                'text': p.get('text', '')[:2000], 'scope': p.get('scope')}
    if r['source'] == 'xhs_notification':
        return {**base, 'speaker': 'strangers_untrusted', 'text': json.dumps(p.get('content', {}), ensure_ascii=False)[:2000]}
    content=compact_content(p.get('tool'),p.get('content',{}))
    published=content.get('published_at')
    if published:
        age=(r['created']-datetime.fromisoformat(published).timestamp())/86400
        content['age_days_at_read']=round(age,1)
        if age>7 and any(w in content.get('title','')+content.get('body','') for w in ('天气','预报','台风')):
            content['freshness']='较旧的时效内容；本次读取时间不能替代帖子适用日期'
    return {**base, 'tool': p.get('tool'), 'source_url': p.get('source_url'), 'content':content}


def semantic_fingerprint(source, external_id, payload):
    if source != 'xiaohongshu':
        clean = {k:v for k,v in payload.items() if k != 'artifact'}
    else:
        # URL credentials, counters and new image URLs do not constitute a new experience.
        clean = compact_content(payload.get('tool'), payload.get('content', {}))
    return hashlib.sha256(json.dumps([source, external_id, clean], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
