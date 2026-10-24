import asyncio
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from hermes_soul.runtime import Runtime
from hermes_soul.store import Store
from hermes_soul.life import Life
from hermes_soul.attention import Attention
from hermes_soul.evidence import normalized
from hermes_soul.failures import describe
from hermes_soul.mcp import MCPError
from hermes_soul.api import create_app


class V03Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.cfg={'home':self.temp.name,'mcp_url':'http://127.0.0.1:1/mcp','interval_seconds':720,
                  'idle_max_seconds':720,'max_calls_per_day':120,'max_browse_steps':3}
        self.r=Runtime(self.cfg);self.s=self.r.store

    async def asyncTearDown(self):
        self.s.close();self.temp.cleanup()

    def user(self,text):
        return self.s.observe('user',text,{'text':text})

    def post(self,post='a',body='阳台光照',token='secret',comments=None):
        return self.s.observe('xiaohongshu',post,{'tool':'get_feed_detail','arguments':{'xsec_token':token},'content':{
            'data':{'note':{'noteId':post,'type':'normal','title':'阳台植物','desc':body,'time':1787490000000,
                           'user':{'nickname':'作者'},'xsecToken':token,'imageList':[{'url':token}]},'comments':{'list':comments or []}}}})

    def record(self,kind,label,oid,**p):
        return self.s.record(kind,label,{'content':label,'state':'active',**p},[oid])

    async def test_migration_parks_waiting_without_rewriting_original_and_does_not_invent_feeling(self):
        oid=self.user('周末去公园')
        rid=self.record('situation','公园安排',oid,content='等老大回话确认',slot='continuation')
        task=self.record('intention','周末公园',oid)
        before=dict(self.s.db.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone())
        self.s.set('attention_migration',0)
        self.r.attention=Attention(self.s,self.r.life,self.cfg)
        self.assertTrue(self.r.attention.blocked(rid));self.assertTrue(self.r.attention.blocked(task))
        self.assertEqual(before,dict(self.s.db.execute('SELECT * FROM records WHERE id=?',(rid,)).fetchone()))
        self.assertEqual(self.r.life.kind_rows('affect'),[])
        self.assertNotIn('等老大',json.dumps(self.r.background_context(purpose='attention'),ensure_ascii=False))
        self.assertIn('公园安排',json.dumps(self.r.background_context('周末公园'),ensure_ascii=False))

    async def test_waiting_topic_does_not_block_other_interest_and_relevant_reply_releases(self):
        oid=self.user('周末公园安排')
        waiting=self.record('situation','周末公园安排',oid)
        other=self.record('interest','阳台植物',oid)
        self.r.attention.set(waiting,'parked','user_reply',reason='等确认')
        ids=[c['id'] for c in self.r.attention.candidates()]
        self.assertIn(other,ids);self.assertNotIn(waiting,ids)
        with patch('time.time',return_value=time.time()+1):
            self.r.attention.on_observation(self.user('你好呀'))
            self.assertTrue(self.r.attention.blocked(waiting))
            self.r.attention.on_observation(self.user('周末公园我们周日去'))
        self.assertFalse(self.r.attention.blocked(waiting))

    async def test_repeated_rest_has_no_prose_feedback_and_bounded_reconsideration(self):
        self.r.model=AsyncMock(return_value='{"activity":"rest","focus_id":"rest","reason":"安静待会儿"}')
        start=time.time()
        with patch('time.time',return_value=start):
            await self.r.tick()
        self.r.model.assert_awaited_once()
        with patch('time.time',return_value=start+720):
            await self.r.tick()
        self.r.model.assert_awaited_once()
        self.assertEqual(self.s.db.execute('SELECT outcome FROM activation_checks ORDER BY id DESC').fetchone()[0],'skipped')
        with patch('time.time',return_value=start+3600):
            await self.r.tick()
        self.assertEqual(self.r.model.await_count,2)
        self.assertNotIn('安静待会儿',json.dumps(self.r.background_context(purpose='attention'),ensure_ascii=False))
        self.assertLessEqual(self.s.get('next_decision')-(start+3600),7200)

    async def test_timer_releases_parked_but_capability_stays_parked(self):
        oid=self.user('植物');rid=self.record('situation','植物',oid)
        self.r.attention.set(rid,'parked','time',time.time()-1,'到点再看')
        self.r.attention.maintenance();self.assertFalse(self.r.attention.blocked(rid))
        self.r.attention.set(rid,'parked','capability',time.time()-1,'缺工具','official_weather')
        self.r.attention.maintenance();self.assertTrue(self.r.attention.blocked(rid))

    async def test_normalized_evidence_has_dates_speaker_and_no_credentials(self):
        oid=self.post();data=normalized(self.s.db.execute('SELECT * FROM observations WHERE id=?',(oid,)).fetchone())
        text=json.dumps(data,ensure_ascii=False)
        self.assertNotIn('secret',text);self.assertIn('published_at',text);self.assertIn('适用日期',text)
        user=self.user('我喜欢数学');data=normalized(self.s.db.execute('SELECT * FROM observations WHERE id=?',(user,)).fetchone())
        self.assertEqual(data['speaker'],'user')

    async def test_revisit_dedupes_credential_changes_and_does_not_promote_interest(self):
        first=self.post(token='old');second=self.post(token='new')
        self.assertEqual(first,second)
        life=self.r.life
        u={'kind':'interest','label':'植物','content':'植物','refs':[first], 'state':'preferred','attraction':'叶片','open_question':'光照'}
        rid=life.save(life.prepare(u,{first},'a'))
        life.save(life.prepare({**u,'id':rid},{first},'b'))
        self.assertEqual(life.get_record(rid)['payload']['state'],'noticed')
        self.assertEqual(len(life.get_record(rid)['payload']['visit_activities']),1)
        changed=self.post(body='阳台光照改变了')
        self.assertNotEqual(first,changed)
        life.save(life.prepare({**u,'id':rid,'refs':[changed]},{changed},'c'))
        self.assertEqual(len(life.get_record(rid)['payload']['visit_activities']),1)

    async def test_affect_requires_actual_new_trigger_and_expires(self):
        oid=self.user('这个展览太漂亮了')
        u={'kind':'affect','label':'期待展览','content':'想了解展览','refs':[oid],'trigger_quote':'展览太漂亮了','scope':'展览','ttl_hours':1}
        rid=self.r.life.save(self.r.life.prepare(u,{oid},'a'))
        with self.assertRaises(ValueError):self.r.life.prepare({**u,'id':rid},{oid},'b')
        with self.assertRaises(ValueError):self.r.life.prepare({**u,'label':'生气','trigger_quote':'对方没回复'},{oid},'b')
        self.assertNotIn('feelings',self.r.background_context('数学'))
        self.assertIn('feelings',self.r.background_context('展览'))
        with patch('time.time',return_value=time.time()+3601):self.r.life.maintenance()
        self.assertEqual(self.r.life.get_record(rid)['payload']['state'],'expired')

    async def test_weather_stays_unverified_and_uncertain(self):
        oid=self.post(body='本周天气晴朗')
        u={'kind':'belief','label':'周末天气','content':'周末一定晴','refs':[oid],'uncertainty':'low'}
        rid=self.r.life.save(self.r.life.prepare(u,{oid},'a'))
        p=self.r.life.get_record(rid)['payload']
        self.assertEqual(p['verification'],'unverified');self.assertEqual(p['uncertainty'],'high')

    async def test_capability_gap_parks_without_fake_weather_read(self):
        oid=self.user('周末天气');rid=self.record('situation','周末天气',oid)
        self.r.model=AsyncMock(return_value=json.dumps({'activity':'search','focus_id':rid,'required_capability':'official_weather'}))
        self.r.mcp.call=AsyncMock()
        await self.r.tick()
        self.r.mcp.call.assert_not_awaited()
        self.assertTrue(self.r.attention.blocked(rid))

    async def test_search_timeout_backoff_and_precise_classification(self):
        self.assertEqual(describe(MCPError('context deadline exceeded'))['code'],'timeout')
        self.assertEqual(describe(MCPError('[launcher] Failed to get the debug url'))['code'],'browser_start_failed')
        self.r.mcp.call=AsyncMock(side_effect=MCPError('context deadline exceeded'))
        aid=self.s.start_activity('search','测试')
        with self.assertRaises(MCPError):await self.r.mcp_observation(aid,'search_feeds',{'keyword':'花'})
        with self.assertRaises(ValueError):await self.r.mcp_observation(aid,'search_feeds',{'keyword':'花'})
        self.r.mcp.call.assert_awaited_once()

    async def test_expression_waits_for_change_or_six_hour_fallback(self):
        oid=self.user('阳台植物光照');x=self.r.expressions
        eid=x.create('阳台植物的光照很有意思',[oid],0);x.decide(eid,'wait','稍后考虑',0,60)
        with patch('time.time',return_value=time.time()+3600):self.assertEqual(x.due(),[])
        fresh=self.post(body='阳台植物光照的变化')
        x.on_observation(fresh);self.assertEqual(x.due()[0]['id'],eid)

    async def test_hours_without_reply_still_allow_actual_exploration_and_return_to_topic(self):
        oid=self.user('周末公园计划');rid=self.record('situation','周末公园计划',oid)
        self.r.attention.set(rid,'parked','user_reply',reason='等待确认')
        self.r.model=AsyncMock(side_effect=['{"activity":"browse","focus_id":"discover"}', '{"action":"stop"}', '{"updates":[]}'])
        self.r.login_status=AsyncMock(return_value={'logged_in':True})
        self.r.mcp.call=AsyncMock(return_value={'content':[{'type':'text','text':'{"feeds":[]}' }]})
        await self.r.tick()
        self.assertEqual(self.s.rows('activities')[0]['kind'],'browse')
        self.assertTrue(self.r.attention.blocked(rid))
        ctx=self.r.background_context('周末公园计划');self.assertIn(rid,[r['id'] for r in ctx['records']])

    async def test_legacy_interest_prose_cannot_reintroduce_waiting_and_weather_certainty(self):
        oid=self.user('公园')
        rid=self.record('interest','公园摄影',oid,content='等老大回话，周六天气已确定')
        context=json.dumps(self.r.background_context(purpose='attention'),ensure_ascii=False)
        self.assertNotIn('天气已确定',context);self.assertNotIn('等老大',context)
        self.assertNotIn('reflect',self.r.background_context(purpose='attention')['available_activities'])
        waiting=self.record('situation','公园安排',oid,content='已经商量好周六出发，等回复')
        self.r.attention.set(waiting,'parked','user_reply')
        current=json.dumps(self.r.background_context('公园安排'),ensure_ascii=False)
        self.assertIn('待相关交流确认',current);self.assertNotIn('已经商量好',current)

    async def test_selection_fatigue_recovers_without_erasing_interest(self):
        oid=self.user('植物');rid=self.record('interest','植物',oid)
        for i in range(2):
            self.r.attention.choose({'focus_id':rid,'activity':'search'},'a')
        self.assertNotIn(rid,[r['id'] for r in self.r.attention.candidates()])
        with patch('time.time',return_value=time.time()+7201):
            self.assertIn(rid,[r['id'] for r in self.r.attention.candidates()])
        self.assertEqual(self.r.life.get_record(rid)['payload']['state'],'active')

    async def test_short_confirmation_uses_recent_actual_conversation(self):
        oid=self.user('周末公园');rid=self.record('situation','周末公园',oid)
        self.r.attention.set(rid,'parked','user_reply')
        self.s.add_chat('assistant','周末公园安排在周日，可以吗？')
        with patch('time.time',return_value=time.time()+1):self.r.attention.on_observation(self.user('可以'))
        self.assertFalse(self.r.attention.blocked(rid))

    async def test_new_source_keeps_auditable_date_warning(self):
        oid=self.post(body='天气预报：本周末晴朗')
        data=normalized(self.s.db.execute('SELECT * FROM observations WHERE id=?',(oid,)).fetchone())
        self.assertIn('freshness',data['content'])
        self.assertGreater(data['content']['age_days_at_read'],7)


class AttentionApiTests(unittest.TestCase):
    def test_admin_park_resume_export_and_auth(self):
        with tempfile.TemporaryDirectory() as home:
            token=Path(home)/'token';token.write_text('t'*32)
            cfg={'home':home,'access_token_file':str(token),'mcp_url':'http://127.0.0.1:1/mcp','interval_seconds':720}
            with patch('hermes_soul.runtime.Runtime.loop',new=AsyncMock()),TestClient(create_app(cfg)) as c:
                self.assertEqual(c.post('/api/attention/x/park',json={}).status_code,401)
                c.headers['Authorization']='Bearer '+'t'*32
                rid=c.post('/api/interests',json={'topic':'植物'}).json()['id']
                self.assertEqual(c.post('/api/attention/'+rid+'/park',json={}).status_code,200)
                self.assertEqual(c.get('/api/state').json()['attention']['parked'][0]['id'],rid)
                self.assertIn('attention_state',c.get('/api/export').json()['data'])
                self.assertEqual(c.post('/api/attention/'+rid+'/resume',json={}).status_code,200)
                self.assertEqual(c.get('/api/state').json()['attention']['parked'],[])
