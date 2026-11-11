"""Attention is a bounded view of memory. Waiting is local to a topic."""
import json
import re
import time
from collections import Counter
from .memory import active, terms
from .prompts import render
from .embedding import canonical_record_text, cosine_similarity

WAIT = re.compile(r'等.{0,12}(?:回[话复音]|确认|回应)|待.{0,8}(?:回应|确认)|只剩等')
RELATIONAL_TOPIC = re.compile(r'男友|老公|伴侣|情侣|同居|领证|结婚|恋爱|婚恋|他回|等他|冷落|亲密|撒娇|约会|和他的联系')


class Attention:
    def __init__(self, store, life, config, embeddings=None):
        self.s, self.life, self.cfg = store, life, config
        self.embeddings = embeddings
        self.s.db.executescript('''
          CREATE TABLE IF NOT EXISTS attention_state(
            record_id TEXT PRIMARY KEY REFERENCES records(id), mode TEXT NOT NULL,
            waiting_for TEXT NOT NULL, reconsider_at REAL, reason TEXT NOT NULL,
            updated REAL NOT NULL, last_selected REAL NOT NULL DEFAULT 0,
            selections INTEGER NOT NULL DEFAULT 0, capability TEXT NOT NULL DEFAULT '');
          CREATE TABLE IF NOT EXISTS activation_checks(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
            outcome TEXT NOT NULL, reason TEXT NOT NULL, next_decision REAL);
        ''')
        if self.s.get('attention_migration') != 1:
            with self.s.transaction():
                waiting_refs = set()
                for r in life.current_situations():
                    p = r['payload']
                    if p.get('state') == 'waiting' or WAIT.search(p.get('content', '')):
                        self.set(r['id'], 'parked', 'user_reply', None, '迁移：原近况明确等待回应，暂放并保留原文')
                        waiting_refs.update(r['refs'])
                for r in life.kind_rows('intention') + life.kind_rows('task'):
                    if active(r) and waiting_refs.intersection(r['refs']):
                        self.set(r['id'], 'parked', 'user_reply', None, '迁移：与等待近况共享来源，相关安排暂放')
                self.s.set('attention_migration', 1)
                self.s.set('schema_version', 3)

    def set(self, rid, mode, waiting_for='', reconsider_at=None, reason='', capability=''):
        if mode not in {'active', 'parked', 'snoozed'} or waiting_for not in {'', 'user_reply', 'new_evidence', 'time', 'capability', 'operator'}:
            raise ValueError('无效注意状态或等待条件')
        if not self.life.get_record(rid):
            raise ValueError('关注记录不存在')
        old = self.state(rid)
        self.s.db.execute('''INSERT INTO attention_state VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(record_id) DO UPDATE SET
             mode=excluded.mode,waiting_for=excluded.waiting_for,reconsider_at=excluded.reconsider_at,
             reason=excluded.reason,updated=excluded.updated,capability=excluded.capability''',
             (rid, mode, waiting_for, reconsider_at, reason[:400], time.time(), 0, 0, capability[:80]))
        if mode != 'active':
            self.s.set('focus_ids', [x for x in self.s.get('focus_ids', []) if x != rid])
        self.s.event('attention_transition', {'record_id':rid, 'before':old, 'mode':mode,
                     'waiting_for':waiting_for, 'reconsider_at':reconsider_at, 'reason':reason[:400]})

    def state(self, rid):
        row = self.s.db.execute('SELECT * FROM attention_state WHERE record_id=?', (rid,)).fetchone()
        return dict(row) if row else None

    def blocked(self, rid):
        row = self.state(rid)
        return bool(row and row['mode'] != 'active')

    def sync(self, rid):
        r = self.life.get_record(rid)
        if r['kind'] not in {'situation', 'intention', 'task'}:
            return
        p = r['payload']; wait = p.get('waiting_for', '')
        if wait or p.get('state') == 'waiting' or WAIT.search(p.get('content', '')):
            self.set(rid, 'parked', wait or 'user_reply', p.get('reconsider_at'),
                     p.get('reason') or '等待条件满足后再考虑', p.get('required_capability', ''))

    def on_observation(self, oid):
        row = self.s.db.execute('SELECT * FROM observations WHERE id=?', (oid,)).fetchone()
        from .evidence import normalized
        obs = normalized(row)
        text = obs.get('text', '') if row['source'] in {'user', 'host'} else json.dumps(obs.get('content', {}), ensure_ascii=False)
        q = set(terms(text))
        if row['source']=='user' and text.strip() in {'好','好的','行','可以','去','同意','没问题'}:
            previous=self.s.db.execute("SELECT content,created FROM chats WHERE role='assistant' AND status='complete' AND created<? ORDER BY created DESC LIMIT 1",(row['created'],)).fetchone()
            if previous and row['created']-previous['created']<600:
                q.update(terms(previous['content']))
        sem_candidates = []
        for entry in self.parked():
            state = entry['attention']; p = entry['payload']
            if state['waiting_for'] not in {'user_reply', 'new_evidence'} or (state['waiting_for']=='user_reply' and row['source']!='user'):
                continue
            if row['created'] <= state['updated']:
                continue
            overlap = q & set(terms(entry['label']+' '+p.get('content','')))
            if len(overlap) >= 2:
                self.set(entry['id'], 'active', reason='收到与该话题相关的新观察 '+oid[:8])
                self.s.set('next_decision', 0)
            else:
                sem_candidates.append(entry)

        if sem_candidates and self.embeddings and self.embeddings.client.is_configured() and len(text.strip()) >= 4:
            try:
                q_vec = self.embeddings.client.embed_one(text)
                stored = self.embeddings.get_vectors([e['id'] for e in sem_candidates])
                for entry in sem_candidates:
                    e_vec = stored.get(entry['id'])
                    if e_vec is None:
                        e_vec = self.embeddings.client.embed_one(canonical_record_text(entry['label'], entry['payload']))
                        with self.s.transaction():
                            self.embeddings.index_from_cache(entry['id'], entry['label'], entry['payload'])
                    if cosine_similarity(q_vec, e_vec) >= 0.50:
                        self.set(entry['id'], 'active', reason='收到与该话题语义相关的新观察 '+oid[:8])
                        self.s.set('next_decision', 0)
            except Exception:
                pass

    def maintenance(self):
        for r in self.parked():
            a = r['attention']
            if a['reconsider_at'] and a['reconsider_at'] <= time.time() and a['waiting_for'] != 'capability':
                self.set(r['id'], 'active', reason='到了重新考虑的时间')
                self.s.set('next_decision', 0)

    def parked(self):
        rows = []
        for a in self.s.db.execute("SELECT * FROM attention_state WHERE mode!='active' ORDER BY updated DESC"):
            r = self.life.get_record(a['record_id'])
            if r and active(r):
                rows.append({**r, 'attention':dict(a)})
        return rows

    def candidates(self, contact=None, dynamics=None):
        now = time.time()
        result, contact_items = [], []
        dyn = dynamics or (contact or {}).get('dynamics') or self.s.get('dynamics_state') or {}
        phase = dyn.get('phase', 'attached')
        curiosity = float(dyn.get('C', 0.48))

        if contact:
            comm = contact.get('commitments') or {}
            if contact.get('gap_level') and contact.get('dynamics', {}).get('allow_reach_out', True):
                c_phase = contact.get('phase', 'missing')
                label = '他很久没消息了，直接表达不满与界限' if c_phase == 'angry' else '他很久没消息了，主动找他'
                contact_items.append({'id':'reach_out','kind':'contact','label':label,
                    'content':render(f"已经约{contact.get('waking_hours_since')}小时（除去睡眠）没收到<<OWNER>>的消息；发出未回{contact.get('unanswered_sent',0)}条（当前状态：{contact.get('dynamics',{}).get('phase_label', c_phase)}）", self.cfg),
                    'selection_basis':'宿主记录的联系间隔与连续动力学状态'})
            elif (comm.get('due') or comm.get('overdue')) and phase not in {'cold', 'detached'}:
                item = (comm.get('overdue') or comm.get('due'))[0]
                is_over = bool(comm.get('overdue'))
                label = render(f"之前的约定「{item['label']}」过了时间没动静，问问<<OWNER>>" if is_over else f"到了约好的「{item['label']}」时间，顺口提一句", self.cfg)
                contact_items.append({'id':'reach_out','kind':'contact','label':label,
                    'content':item.get('content', item['label']),
                    'selection_basis':'宿主记录的跨天约定时间窗口触发'})

        # Discrete Topic Field: seed associative spreading activation from top 2 recently engaged interests
        active_interests = [
            r for r in self.life.kind_rows('interest')
            if active(r) and not self.blocked(r['id']) and r['payload'].get('state') != 'dormant'
        ]
        seeds = sorted(active_interests, key=lambda r: float(r['payload'].get('last_engaged', r['updated'])), reverse=True)[:2]
        seed_ids = {r['id'] for r in seeds}
        seed_terms = set()
        for sr in seeds:
            seed_terms.update(terms(sr['label'] + ' ' + sr['payload'].get('attraction', '')))

        eligible = []
        for kind in ('interest','situation','intention','task'):
            for r in self.life.kind_rows(kind):
                if not active(r) or self.blocked(r['id']):
                    continue
                a = self.state(r['id']) or {}; p = r['payload']
                since = max(0, now - a.get('last_selected', 0))
                if a.get('selections', 0) >= 2 and since < 7200 and p.get('last_engaged', 0) <= a.get('last_selected', 0):
                    continue
                if a.get('selections', 0) >= 4 and since < 7200:
                    continue
                eligible.append((kind, r, a, p, since))

        # Load stored semantic embeddings from SQLite (0 HTTP calls; sub-millisecond)
        vec_map = {}
        rel_anchor = None
        if self.embeddings and self.embeddings.client.is_configured() and eligible:
            all_ids = list(seed_ids) + [r['id'] for _, r, _, _, _ in eligible]
            vec_map = self.embeddings.get_vectors(all_ids)
            rel_anchor = self.embeddings.get_relational_anchor(allow_network=False)
        seed_vecs = [vec_map[sid] for sid in seed_ids if sid in vec_map]

        for kind, r, a, p, since in eligible:
            freshness = max(0, 1 - (now - r['updated']) / 86400)
            satiety = 0.35 * max(0, a.get('selections', 0) - 1) if since < 14400 else 0
            stage_bonus = {'preferred': 0.25, 'exploring': 0.18, 'rekindled': 0.15}.get(p.get('state'), 0.0) if kind == 'interest' else 0.0

            text_blob = r['label'] + ' ' + p.get('content', '') + ' ' + p.get('attraction', '')
            item_terms = set(terms(text_blob))
            assoc_bonus = 0.0
            if r['id'] not in seed_ids:
                if seed_terms and item_terms:
                    overlap_ratio = len(item_terms & seed_terms) / max(1, min(len(item_terms), 12))
                    assoc_bonus = 0.22 * min(1.0, overlap_ratio)
                vec_r = vec_map.get(r['id'])
                if vec_r is not None and seed_vecs:
                    max_sim = max(cosine_similarity(vec_r, sv) for sv in seed_vecs)
                    if max_sim >= 0.50:
                        sem_bonus = 0.30 * min(1.0, (max_sim - 0.50) / 0.30)
                        assoc_bonus = max(assoc_bonus, sem_bonus)

            # Local relational spillover (finite correlation length: lexical + semantic anchor)
            vec_r = vec_map.get(r['id'])
            rel_sim = cosine_similarity(vec_r, rel_anchor) if (vec_r is not None and rel_anchor is not None) else 0.0
            is_rel_topic = bool(RELATIONAL_TOPIC.search(text_blob)) or p.get('slot') == 'relationship' or rel_sim >= 0.48
            if phase in {'cold', 'detached'}:
                field_shift = -0.65 if is_rel_topic else (0.20 + 0.25 * curiosity)
            elif phase in {'missing', 'anxious'} and is_rel_topic:
                field_shift = 0.12
            else:
                field_shift = 0.12 * max(0.0, curiosity - 0.55) if kind == 'interest' else 0.0

            score = (
                1 + freshness + (0.5 if kind == 'interest' else 0) + stage_bonus
                + assoc_bonus + field_shift
                - max(0, 2 - since / 1800) - satiety - (0.6 if p.get('state') == 'dormant' else 0)
            )
            result.append({'id':r['id'],'kind':kind,'label':r['label'], 'content':(p.get('attraction','') if kind=='interest' else p.get('content',''))[:180],
                'state':p.get('state'), 'score':round(score,2),
                'selection_basis':'实际兴趣或未尽问题；近期重复关注降低排序',
                'last_selected':a.get('last_selected'), 'question':p.get('open_question','')})

        discover_basis = '歇够了探索欲正旺，适合主动看看新鲜图文' if curiosity >= 0.65 else '独立的外界接触机会'
        rest_basis = '已休息较久、好奇心回升，除非确实不想动否则可去逛逛' if curiosity >= 0.68 else '可以没有产出'
        return contact_items + sorted(result,key=lambda x:x['score'],reverse=True)[:6] + [
            {'id':'discover','kind':'browse','label':'随意看看新的图文','selection_basis':discover_basis},
            {'id':'rest','kind':'rest','label':'暂时休息','selection_basis':rest_basis}]

    def choose(self, choice, aid, contact=None):
        rid = choice.get('focus_id', '')
        valid = {c['id'] for c in self.candidates(contact)}
        if rid and rid not in valid:
            # Candidate lists can change between prompt construction and validation.
            # Preserve the activity as a deferred discovery instead of manufacturing a failure.
            self.s.step(aid, 'focus', 'complete', focus_id=rid, reason='候选在决策期间已退出，改为随意发现')
            rid = 'discover'
            choice = {**choice, 'focus_id': rid, 'activity': 'browse', 'reason': '原关注已变化，先看看新的图文'}
        if rid and rid not in {'discover','rest','reach_out'}:
            if not self.state(rid):
                self.set(rid,'active',reason='选择关注')
            self.s.db.execute('UPDATE attention_state SET last_selected=?,selections=CASE WHEN last_selected<? THEN 1 ELSE selections+1 END WHERE record_id=?',(time.time(),time.time()-21600,rid))
            self.s.set('focus_ids',[rid])
        else:
            self.s.set('focus_ids',[])
        self.s.set('focus_choice',{'activity':choice.get('activity'),'reason':str(choice.get('reason',''))[:400],
                                  'focus_id':rid,'activity_id':aid,'at':time.time()})
        self.s.step(aid,'focus','complete',focus_id=rid,reason=choice.get('reason',''))
        cap = choice.get('required_capability','')
        if cap and cap not in self.available_capabilities():
            if rid and rid not in {'discover','rest','reach_out'}:
                self.set(rid,'parked','capability',None,'等待可用能力：'+str(cap),str(cap))
            self.s.step(aid,'capability','complete',message='当前能力为小红书图文读取与已开启的互动；此项等待合适来源',capability=cap)
            return False
        return True

    def available_capabilities(self):
        caps = {'xiaohongshu_read', 'xiaohongshu_my_collections'}
        social = self.cfg.get('social') or {}
        if social.get('enabled'):
            from .social import DEFAULTS as SOCIAL_DEFAULTS
            kinds = social.get('kinds') or {}
            for kind, cap_name in (('like', 'xiaohongshu_like'), ('favorite', 'xiaohongshu_favorite')):
                mode = {**SOCIAL_DEFAULTS[kind], **(kinds.get(kind) or {})}.get('mode')
                if mode == 'live':
                    caps.add(cap_name)
        return caps

    def activity_summary(self):
        recent = self.s.rows('activities', 40)
        return {'counts':dict(Counter(r['kind'] for r in recent if r['status']!='running')),
                'window_start':recent[-1]['started'] if recent else None,
                'latest_external_at':self.s.db.execute("SELECT max(created) FROM observations WHERE source='xiaohongshu'").fetchone()[0],
                'last_tool_failure':next(({'at':r['started'],'code':r['checkpoint'].get('error_code')} for r in recent if r['status']=='failed'),None)}

    def due_reason(self, expressions):
        if self.s.get('force_decision',False): return '管理入口请求重新考虑'
        if self.s.pending_observations(): return '有新的待消化观察'
        if self.s.get('last_decision_revision',-1)!=self.s.get('revision'): return '交流或管理状态有变化'
        if expressions.due(): return '表达候选到了复核时间'
        if time.time() >= self.s.get('next_decision',0): return '自主重新考虑时间已到'
        return None

    def check(self, outcome, reason):
        self.s.db.execute('INSERT INTO activation_checks(created,outcome,reason,next_decision) VALUES(?,?,?,?)',
            (time.time(),outcome,reason,self.s.get('next_decision',0)))

    def finish(self, kind):
        rests = self.s.get('rest_streak',0)+1 if kind=='rest' else 0
        self.s.set('rest_streak',rests)
        base = max(self.cfg['interval_seconds'],self.cfg.get('decision_min_seconds',1800))
        delay = min(self.cfg.get('decision_max_seconds',7200),base*(2**min(max(rests-1,0),2))) if rests else base
        self.s.set('next_decision',time.time()+delay)
        self.s.set('last_decision_revision',self.s.get('revision'))
        self.s.set('force_decision',False)
