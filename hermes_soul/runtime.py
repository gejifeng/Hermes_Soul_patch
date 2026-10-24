import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import time
import uuid

from .config import parse_object
from .schedule import sleep_until, next_allowed
from .mcp import Xiaohongshu, feed_cards
from .prompts import IDENTITY_CONTRACT, CHOOSE, PICK, REMEMBER, CHAT
from .store import Store, encode
from .life import KINDS, Life
from .attention import Attention
from .evidence import normalized, compact_content
from .memory import active, terms
from .memory import recall
from .expressions import Expressions
from .failures import describe
from .prompts import EXPRESSION_REVIEW, EXPRESSION_STYLE, REACH_OUT
from .contact import Contact
from .social import Social, TOOLS as SOCIAL_TOOLS, PUBLIC as SOCIAL_PUBLIC
from .prompts import render, PUBLIC_PERSONA, SOCIAL_COMMENT, SOCIAL_REPLY, SOCIAL_AUDIT, SOCIAL_AUDITOR


class StaleActivity(RuntimeError):
    pass


class Runtime:
    def __init__(self, config):
        self.cfg = config
        self.store = Store(config["home"])
        self.store.recover()
        self.life = Life(self.store)
        self.attention = Attention(self.store, self.life, config)
        self.expressions = Expressions(self.store, config)
        self.contact = Contact(self.store, config)
        self.social = Social(self.store, config)
        self.mcp = Xiaohongshu(config["mcp_url"], config.get("mcp_token_file"))
        self.model_lock = asyncio.Lock()
        self.activity_lock = asyncio.Lock()
        self.chat_lock = asyncio.Lock()
        self.chat_waiting = 0
        self.process = None
        self.process_purpose = None
        self.stopping = False
        self.wake = asyncio.Event()
        self.last_error = None
        self.last_login = None
        self.active_id = None
        self.mcp_ready = False

    def system(self):
        parts = [IDENTITY_CONTRACT]
        for name in ("SOUL.md", "USER.md"):
            path = self.store.home / "identity" / name
            if path.exists():
                parts.append(f"身份资料 {name}:\n" + path.read_text()[:6000])
        parts.append("身份资料用于人物设定；具体经历以背景中的实际观察为依据。")
        return "\n\n".join(parts)

    def check_revision(self, revision):
        if sleep_until(self.cfg) or self.stopping or self.store.get("paused") or self.chat_waiting or self.bridge_busy() or self.store.get("revision") != revision:
            raise StaleActivity("用户输入、暂停或状态修改使本次背景失效")

    def bridge_busy(self):
        return time.time() < self.store.get("foreground_until", 0)

    async def model(self, purpose, instruction, context, *, history=None, foreground=False, system=None):
        async with self.model_lock:
            if not foreground and (sleep_until(self.cfg) or self.chat_waiting or self.bridge_busy() or self.store.get("paused")):
                raise StaleActivity("当前不适合后台模型调用")
            if not foreground and time.time() < self.store.get("model_backoff_until", 0):
                raise StaleActivity("前台优先或自主活动已暂停")
            cid = self.store.reserve_call(purpose, self.cfg["max_calls_per_day"], foreground)
            root = self.store.home / "artifacts" / cid
            root.mkdir(mode=0o700)
            result_path = root / "result.json"
            request = {"config": self.cfg, "call_id": cid, "purpose": purpose, "result_path": str(result_path),
                       "system": render(system or self.system(), self.cfg), "prompt": render(instruction, self.cfg) + "\n\n背景数据：\n" + encode(context),
                       "history": history or []}
            # File logging bounds pipe memory, keeps provider diagnostics outside the UI.
            log = (root / "worker.log").open("wb")
            env = dict(os.environ)
            env["HERMES_HOME"] = self.cfg["hermes_home"]
            project = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = project + os.pathsep + self.cfg["hermes_root"]
            try:
                proc = await asyncio.create_subprocess_exec(
                    self.cfg["python"], "-m", "hermes_soul.worker",
                    stdin=asyncio.subprocess.PIPE, stdout=log, stderr=log,
                    cwd=str(self.store.home), env=env, start_new_session=True)
                self.process, self.process_purpose = proc, purpose
                await asyncio.wait_for(proc.communicate(encode(request).encode()), self.cfg["worker_timeout"])
                if proc.returncode or not result_path.exists():
                    raise RuntimeError(f"Hermes 执行失败，诊断记录 {cid}/worker.log")
                output = json.loads(result_path.read_text())
                self.store.call_done(cid, "complete", output.get("usage", {}))
                self.store.set("model_failure_streak", 0)
                self.store.set("model_backoff_until", 0)
                return output["text"]
            except BaseException as exc:
                await self.stop_worker()
                self.store.call_done(cid, "failed", {"error": type(exc).__name__ + ": " + str(exc)[:500]})
                if not foreground and not isinstance(exc, StaleActivity):
                    streak=self.store.get("model_failure_streak",0)+1
                    self.store.set("model_failure_streak",streak)
                    if streak>=2:
                        self.store.set("model_backoff_until",time.time()+min(3600,900*2**min(streak-2,2)))
                raise
            finally:
                self.process = self.process_purpose = None
                log.close()

    async def stop_worker(self):
        proc = self.process
        if proc and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 3)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()

    async def initialize_mcp(self):
        tools = await self.mcp.initialize()
        names = {t["name"] for t in tools.get("tools", [])}
        missing = {"list_feeds", "search_feeds", "get_feed_detail", "check_login_status"} - names
        if missing:
            raise RuntimeError("小红书 MCP 缺少工具: " + ",".join(sorted(missing)))
        self.mcp_ready = True
        return names

    async def login_status(self):
        if not self.mcp_ready:
            await self.initialize_mcp()
        self.last_login = await self.mcp.login_status()
        return self.last_login

    def background_context(self, query="", purpose="chat"):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        focused = [self.life.get_record(rid) for rid in self.store.get("focus_ids", [])]
        focused = [r for r in focused if r and active(r) and not self.attention.blocked(r['id'])][:3]
        ctx = {"now":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "revision":self.store.get("revision"),
               "capabilities":{"available":["xiaohongshu_read"],"weather_verification":"需单独的官方来源，当前尚未接入",
                               "physical_actions":"角色想象与拟议活动应标注为想象或计划"},
               "current_focus":self.store.get("focus_choice"), "recent_activities":self.attention.activity_summary(),
               "contact":self.contact.state()}
        def small(r):
            p=dict(r['payload'])
            if self.attention.blocked(r['id']):
                p['state']='waiting'
                p['waiting_for']=self.attention.state(r['id'])['waiting_for']
                # Historical prose can assert a proposal as agreed; working state carries only the confirmed status.
                p['content']='暂放的主题：'+r['label']+'；等待条件：'+p['waiting_for']+'。具体安排待相关交流确认。'
            if r['kind']=='interest':
                p['content']=p.get('attraction') or r['label']
            if any(w in p.get('content',p.get('text','')) for w in ('天气','预报','降雨','台风')):
                p.update(verification='unverified',uncertainty='high',time_sensitive=True)
            if r['kind'] in {'episode','journal'}:
                p['epistemic_status']='subjective_historical_expression'
            keys=('content','text','state','epistemic_status','uncertainty','verification','time_sensitive','applicable_period',
                  'action','scope','review_at','waiting_for','open_question','attraction','expires_at')
            return {"id":r['id'],"kind":r['kind'],"label":r['label'],
                    "payload":{k:(v[:500] if isinstance(v,str) else v) for k,v in p.items() if k in keys},
                    "refs":r['refs'][-4:]}
        ctx['situations']=[small(r) for r in focused]
        ctx['boundaries']=[small(r) for r in self.life.boundaries()][:4]
        if purpose=='attention':
            ctx['candidates']=self.attention.candidates(self.reach_out_contact(ctx['contact']))
            ctx['available_activities']=['browse','search','rest']
            if any(c['id']=='reach_out' for c in ctx['candidates']): ctx['available_activities'].append('reach_out')
            if self.store.pending_observations(): ctx['available_activities'].append('reflect')
            if any(c.get('kind')=='task' for c in ctx['candidates']):ctx['available_activities'].append('task')
            if self.expressions.due():ctx['available_activities'].append('express')
            ctx['parked_count']=len(self.attention.parked())
            ctx['records']=[]
            # Old decision prose and old chat are absent from fresh attention selection.
            ctx.pop('current_focus',None)
        else:
            query=query or ' '.join(r['label'] for r in focused)
            recalled=recall(self.store,query,limit=8)
            q=set(terms(query))
            if purpose=='chat' and not any(w in query for w in ('日记','当时','那天','经历','回忆','看过')):
                recalled=[r for r in recalled if r['kind'] not in {'episode','journal'}]
            recalled=[r for r in recalled if q.intersection(terms(r['label']+' '+r['payload'].get('content',r['payload'].get('text',''))))]
            ctx['records']=[small(r) for r in recalled[:6]]
            ctx['parked_topics']=[{"id":r['id'],"label":r['label'],"waiting_for":r['attention']['waiting_for']}
                                  for r in self.attention.parked() if q.intersection(terms(r['label']))][:3]
        affects=[r for r in self.life.kind_rows('affect') if active(r)]
        scope_query=query+' '+' '.join(r['label'] for r in focused)
        related=[r for r in affects if r['payload'].get('resolve_on')=='owner_message' or set(terms(r['payload'].get('scope',''))) & set(terms(scope_query))]
        if related: ctx['feelings']=[small(r) for r in related[:2]]
        return ctx

    async def mcp_observation(self, aid, tool, args):
        start = time.time()
        import hashlib
        retry_key = "tool_backoff:"+hashlib.sha256(encode([tool,args.get('keyword',args.get('feed_id',''))]).encode()).hexdigest()
        if self.store.get(retry_key,{}).get('until',0)>start:
            raise ValueError("该读取正在退避，稍后再试或换一个方向")
        self.store.step(aid, "tool", "running", tool=tool, feed_id=args.get("feed_id"), query=args.get("keyword"))
        try:
            result = await self.mcp.call(tool, args)
            data = self.mcp.data(result)
            text = self.mcp.text(result)
            if not text.strip() and not data:
                raise RuntimeError("MCP 没有返回可读取内容")
        except BaseException as exc:
            error = describe(exc)
            if not isinstance(exc,asyncio.CancelledError):
                attempts=self.store.get(retry_key,{}).get('attempts',0)+1
                self.store.set(retry_key,{'attempts':attempts,'until':time.time()+min(7200,900*2**min(attempts-1,3)), 'code':error['code']})
            self.store.step(aid, "tool", "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                            tool=tool, elapsed=round(time.time()-start, 2), **error)
            self.store.db.execute("UPDATE activity_steps SET status='finished' WHERE activity_id=? AND status='running'", (aid,))
            raise
        self.store.set(retry_key,{})
        raw_path = self.store.home / "artifacts" / (uuid.uuid4().hex + ".json")
        raw_path.write_text(encode(result))
        oid = self.store.observe("xiaohongshu", args.get("feed_id", tool),
              {"tool": tool, "arguments": args, "content": data, "media_understood": False,
               "source_url": "https://www.xiaohongshu.com/explore/" + args["feed_id"] if args.get("feed_id") else "https://www.xiaohongshu.com/",
               "artifact": str(raw_path.relative_to(self.store.home))})
        self.store.queue_observation(oid)
        self.attention.on_observation(oid)
        self.expressions.on_observation(oid)
        row = self.store.db.execute("SELECT checkpoint FROM activities WHERE id=?", (aid,)).fetchone()
        cp = json.loads(row[0]) if row else {}
        cp["refs"] = list(dict.fromkeys(cp.get("refs", []) + [oid]))
        self.store.checkpoint(aid, cp)
        self.store.db.execute("UPDATE activity_steps SET status='finished' WHERE activity_id=? AND status='running'", (aid,))
        self.store.step(aid, "tool", "complete", tool=tool, observation_id=oid, elapsed=round(time.time()-start, 2))
        self.store.event("tool_observation", {"activity_id": aid, "observation_id": oid,
                         "tool": tool, "elapsed": time.time() - start})
        return oid, data

    def browse_cards(self, aid, data):
        cards = feed_cards(data)
        allowed = [c for c in cards if c["type"] == "normal" and self.store.get("post_backoff:"+c["id"], 0) <= time.time()][:20]
        self.store.step(aid, "filter", "complete", offered=len(allowed), skipped=len(cards)-len(allowed),
                        message="只选择图文帖，跳过视频、未知类型与近期失败的帖子")
        return allowed

    async def mcp_listing(self, aid, tool, args):
        # search_feeds is the flaky endpoint (upstream navigation can exceed its
        # 60s deadline). Do not let that kill the whole activity: fall back to the
        # reliable home feed. The timeout is still recorded as a step for audit.
        try:
            return await self.mcp_observation(aid, tool, args)
        except Exception as exc:
            error = describe(exc)
            if tool == "search_feeds" and error["code"] in {"timeout", "connection_error", "browser_start_failed"}:
                self.store.step(aid, "partial", "complete", recovery="搜索不可用，改用首页图文", **error)
                return await self.mcp_observation(aid, "list_feeds", {})
            raise

    async def browse(self, aid, decision, revision):
        query = str(decision.get("query", ""))[:120]
        tool, args = ("search_feeds", {"keyword": query}) if query else ("list_feeds", {})
        oid, data = await self.mcp_listing(aid, tool, args)
        refs = [oid]
        self.store.checkpoint(aid, {"refs": refs})
        cards = self.browse_cards(aid, data)
        seen = []
        detail = None
        failures = 0
        for _ in range(self.cfg["max_browse_steps"]):
            self.check_revision(revision)
            self.store.checkpoint(aid, {"refs": refs, "cards": cards, "opened": seen})
            choice = parse_object(await self.model("browse_choice", PICK,
                 {"life": self.background_context(query, "browse"), "cards": [{k:v for k,v in c.items() if k != "xsec_token"} for c in cards], "opened_this_activity": seen,
                  "last_read_text": compact_content("get_feed_detail",detail) if detail and "tool_error" not in detail else detail}))
            self.check_revision(revision)
            action = choice.get("action")
            self.store.event("attention", {"activity_id": aid, "choice": choice})
            self.store.step(aid, "choice", "complete", action=action, reason=str(choice.get("reason", ""))[:600])
            if action == "stop":
                break
            if action == "search":
                query = str(choice.get("query", "")).strip()[:120]
                if not query:
                    raise ValueError("搜索词为空")
                try:
                    oid, data = await self.mcp_observation(aid, "search_feeds", {"keyword": query})
                except Exception as exc:
                    error = describe(exc)
                    # A mid-activity search failure should end the reading with what
                    # we already have, not fail the whole activity.
                    if error["code"] in {"timeout", "connection_error", "browser_start_failed"}:
                        self.store.step(aid, "partial", "complete", recovery="搜索不可用，保留已读内容并结束", **error)
                        break
                    raise
                cards, detail = self.browse_cards(aid, data), None
            elif action == "read":
                card = next((c for c in cards if c["id"] == choice.get("feed_id")), None)
                if not card or not card["xsec_token"]:
                    raise ValueError("只能读取 MCP 实际返回并带访问令牌的帖子")
                if card.get("type") != "normal":
                    raise ValueError("当前只允许读取图文帖，视频或未知类型的帖子已跳过")
                # Revisits across activities are welcome; repeated same-call reads are not needed.
                if card["id"] in seen:
                    break
                try:
                    oid, detail = await self.mcp_observation(aid, "get_feed_detail",
                        {"feed_id": card["id"], "xsec_token": card["xsec_token"], "load_all_comments": False})
                except Exception as exc:
                    self.check_revision(revision)
                    failures += 1
                    error = describe(exc)
                    self.store.set("post_backoff:"+card["id"], time.time()+3600)
                    cards = [c for c in cards if c["id"] != card["id"]]
                    detail = {"tool_error": error, "meaning": "技术失败，不表示个人偏好变化"}
                    self.store.step(aid, "partial", "complete", recovery="保留已取得的内容，可换帖或结束", **error)
                    if failures >= 2 or error["code"] in {"login_required", "connection_error", "browser_start_failed"}:
                        break
                    continue
                seen.append(card["id"])
            else:
                raise ValueError("未知浏览动作")
            refs.append(oid)
            self.store.checkpoint(aid, {"refs": refs, "opened": seen})
        self.store.checkpoint(aid, {"refs": refs, "opened": seen})
        return refs

    def apply_reflection(self, aid, result, allowed_refs, revision):
        self.check_revision(revision)
        refs = result.get("refs") or allowed_refs
        updates = result.get("updates", [])
        if not isinstance(updates, list):
            updates = []
        def validate_refs(candidate):
            self.store.valid_refs(candidate)
            if not set(candidate).issubset(set(allowed_refs)):
                raise ValueError("不能借用本次背景之外的观察")
        validate_refs(refs)
        # A non-object entry or an unknown `kind` can never be persisted and makes
        # no evidence claim, so drop those before validating the batch. Everything
        # that remains is validated atomically: if any one fails (bad refs, domain
        # rule, ...) nothing is written and the error propagates, preserving the
        # all-or-nothing guarantee asserted by
        # test_user_correction_fences_old_updates_atomically.
        usable = []
        for u in updates:
            if not isinstance(u, dict) or u.get("kind") not in KINDS:
                kind = u.get("kind") if isinstance(u, dict) else type(u).__name__
                self.store.step(aid, "update", "skipped", kind=str(kind)[:40],
                                reason="无效的记忆类型，已忽略")
                continue
            usable.append(u)
        if len(usable) > 3:
            self.store.step(aid, "update", "trimmed", reason="模型返回超过3条更新，保留前3条")
            usable = usable[:3]
        prepared = [self.life.prepare(u, set(allowed_refs), aid) for u in usable]
        with self.store.transaction():
            if result.get("impression"):
                self.store.record("episode", "活动印象", {"text": str(result["impression"])[:1600], "activity_id": aid, "epistemic_status":"subjective_impression"}, refs)
            for item in prepared:
                rid = self.life.save(item)
                self.attention.sync(rid)
            if result.get("journal"):
                self.store.record("journal", "私人日记", {"text": str(result["journal"])[:4000], "activity_id": aid, "epistemic_status":"subjective_journal"}, refs)
            if result.get("expression"):
                self.expressions.create(str(result["expression"]), refs, revision)
            self.store.event("reflection", {"activity_id": aid, "result": result})
            consumed = list(dict.fromkeys(self.store.get("reflected_observations", []) + list(allowed_refs)))[-512:]
            self.store.set("reflected_observations", consumed)
            self.store.db.executemany("UPDATE observation_queue SET status='digested' WHERE observation_id=?", [(r,) for r in allowed_refs])
        self.export_journals()

    def export_journals(self):
        # DB is authoritative; regenerate after restart if a crash split DB/file commits.
        for r in self.store.rows("records", 1000):
            if r["kind"] == "journal":
                path = self.store.home / "journal" / (r["id"] + ".md")
                tmp = path.with_suffix(".tmp")
                tmp.write_text(r["payload"].get("text", "") + "\n\n来源: " + ", ".join(r["refs"]) + "\n")
                tmp.replace(path)

    async def reflect(self, aid, refs, revision):
        reflected = self.store.get("reflected_observations", [])
        self.store.db.executemany("UPDATE observation_queue SET status='digested' WHERE observation_id=?", [(r,) for r in refs if r in reflected])
        refs = [ref for ref in refs if ref not in reflected][:6]
        if not refs:
            return
        self.check_revision(revision)
        observations = []
        for oid in refs:
            row = self.store.db.execute("SELECT * FROM observations WHERE id=?", (oid,)).fetchone()
            observations.append(normalized(row))
        query = " ".join(o.get('text','') or str(o.get('content',{}).get('title','')) for o in observations)[:1000]
        self.store.step(aid, "reflection", "running", refs=refs)
        context = {"life": self.background_context(query,"reflect"), "observations": observations,
                   "recent_chat": [{"role":c['role'],"text":c['content'][:900],"created":c['created']}
                     for c in reversed(self.store.rows('chats',4)) if c['status']=='complete' and time.time()-c['created']<3600]}
        posts = self.social.read_posts(aid) if self.social.conf.get("enabled") else []
        if posts:
            context["social_options"] = {"read_posts": posts, **{"can_"+k: self.social.mode(k) != "off" for k in ("like", "favorite", "comment")}}
        try:
            result = parse_object(await self.model("reflect", REMEMBER, context))
            self.apply_reflection(aid, result, refs, revision)
        except Exception as exc:
            if not isinstance(exc, StaleActivity):
                self.store.db.executemany("UPDATE observation_queue SET attempts=attempts+1 WHERE observation_id=?", [(r,) for r in refs])
            self.store.step(aid, "reflection", "failed", **describe(exc))
            raise
        with self.store.transaction():
            self.store.set("reflected_observations", (reflected + refs)[-512:])
            self.store.db.executemany("UPDATE observation_queue SET status='digested' WHERE observation_id=?", [(r,) for r in refs])
        self.store.step(aid, "reflection", "complete", refs=refs, message="已消化，可不产生持久更新")
        return result

    async def review_expressions(self, revision):
        for row in self.expressions.due():
            self.check_revision(revision)
            decision = parse_object(await self.model("expression_review", EXPRESSION_REVIEW,
                {"expression": row, "life": self.background_context(row["content"],"expression"),
                 "recent_expressions": [{"content": r["content"], "status": r["status"]} for r in self.expressions.rows()[:8]]}))
            self.check_revision(revision)
            self.expressions.decide(row["id"], decision.get("action"), str(decision.get("reason", "")), revision, decision.get("delay_seconds", 1800))

    def reach_out_contact(self, contact):
        """Offer reaching out only when no earlier burst is still queued or just went out."""
        if not contact.get('gap_level') or not self.contact.gap_observations():
            return None
        last = self.store.db.execute("""SELECT max(m.updated) FROM expression_meta m JOIN outbox o ON o.id=m.id
            WHERE m.origin='reach_out' AND o.status IN ('ready','sending','sent','uncertain')""").fetchone()[0]
        pending = self.store.db.execute("""SELECT 1 FROM expression_meta m JOIN outbox o ON o.id=m.id
            WHERE m.origin='reach_out' AND o.status='ready'""").fetchone()
        if pending or (last and time.time()-last < self.cfg.get('delivery', {}).get('min_interval_seconds', 3600)):
            return None
        return contact

    async def reach_out(self, aid, revision):
        refs = self.contact.gap_observations()
        if not refs:
            raise ValueError("没有宿主记录的联系间隔，不能凭空说很久没联系")
        # The model may name reach_out even when it was not offered; a queued burst must go out first.
        if not self.reach_out_contact(self.contact.state()):
            self.store.step(aid, "reach_out", "skipped", message="上一组主动消息还在等待发送或刚发出")
            return []
        result = parse_object(await self.model("reach_out", REACH_OUT, {"life": self.background_context("", "expression"),
            "recent_expressions": [{"content": r["content"], "status": r["status"], "origin": r["origin"]} for r in self.expressions.rows()[:8]]}))
        self.check_revision(revision)
        messages = result.get("messages")
        if not isinstance(messages, list):
            raise ValueError("主动联系需要消息列表")
        messages = [m.strip()[:500] for m in messages if isinstance(m, str) and m.strip()][:self.cfg.get("delivery", {}).get("max_per_block", 4)]
        if not messages:
            self.store.step(aid, "reach_out", "complete", message="想了想，这次先不发")
            return []
        block = uuid.uuid4().hex
        reason = str(result.get("reason", ""))[:400] or render("想<<OWNER>>了，主动联系", self.cfg)
        with self.store.transaction():
            ids = [self.expressions.create(m, refs, revision, origin="reach_out", block_id=block, seq=i) for i, m in enumerate(messages)]
            for eid in ids:
                self.expressions.decide(eid, "ready", reason, revision)
            # "在干嘛呀" is only true for a short while.
            self.store.db.executemany("UPDATE expression_meta SET expires=? WHERE id=?",
                                      [(time.time() + self.cfg.get("delivery", {}).get("reach_out_ttl_seconds", 7200), eid) for eid in ids])
        self.store.step(aid, "reach_out", "complete", block_id=block, messages=len(ids), reason=reason)
        return ids

    def social_status(self):
        s = self.social
        return {"enabled": bool(s.conf.get("enabled")), "modes": {k: s.mode(k) for k in SOCIAL_TOOLS},
                "suspended_until": s.suspended(), "private_terms_configured": bool(s.private_terms()),
                "pending_approval": s.s.db.execute("SELECT count(*) FROM social_actions WHERE status='pending_approval'").fetchone()[0],
                "used_today": {k: s.used_today(k) for k in SOCIAL_TOOLS},
                "limits": {k: s.policy(k)["per_day"] for k in SOCIAL_TOOLS}}

    def public_persona(self):
        path = self.social.conf.get("persona_file")
        return Path(path).expanduser().read_text()[:3000] if path and Path(path).expanduser().exists() else PUBLIC_PERSONA

    async def social_audit(self, kind, text, context):
        try:
            verdict = parse_object(await self.model("social_audit", SOCIAL_AUDIT, {"kind": kind, "draft": text, "context": context},
                                                    system=SOCIAL_AUDITOR))
        except StaleActivity:
            raise
        except Exception as exc:
            verdict = {"pass": False, "issues": ["审核结果无法取得：" + type(exc).__name__]}
        return verdict

    async def social_after_reading(self, aid, result):
        """Reading may end in a like, a favorite or a comment proposal; failures never undo the reading."""
        if not self.social.conf.get("enabled") or not isinstance(result, dict):
            return
        try:
            for item in (result.get("react") or [])[:3]:
                if isinstance(item, dict) and isinstance(item.get("feed_id"), str):
                    for kind in ("like", "favorite"):
                        if item.get(kind) is True:
                            self.social.propose_reaction(aid, kind, item["feed_id"], str(item.get("reason", "")))
            if isinstance(result.get("comment_on"), str) and result["comment_on"].strip():
                await self.social_comment(aid, result["comment_on"].strip())
        except StaleActivity:
            raise
        except Exception as exc:
            self.store.step(aid, "social", "failed", **describe(exc))

    async def social_comment(self, aid, feed_id):
        facts, why = self.social.comment_target(aid, feed_id)
        fields = {"feed_id": feed_id, "activity_id": aid, "title": (facts or {}).get("title"), "author": (facts or {}).get("author"),
                  "xsec_token": (facts or {}).get("xsec_token"), "refs": [facts["observation_id"]] if facts else []}
        if why:
            return self.social.add("comment", "blocked", checks=[why], **fields)
        context = self.social.comment_context(facts)
        draft = parse_object(await self.model("social_comment", SOCIAL_COMMENT, context, system=self.public_persona()))
        text = str(draft.get("text") or "").strip()
        if draft.get("skip") or not text:
            return self.social.add("comment", "blocked", checks=["她想了想，决定不评论"], **fields)
        audit = await self.social_audit("comment", text, context)
        sid = self.social.submit_public("comment", text, audit=audit, **fields)
        self.store.step(aid, "social", "complete", kind="comment", outcome=self.social.get(sid)["status"])
        return sid

    async def poll_notifications(self):
        s = self.social
        if s.mode("reply") == "off" or not s.commented_feeds() or time.time() < self.store.get("social_next_poll", 0):
            return
        self.store.set("social_next_poll", time.time() + s.conf.get("notification_poll_seconds", 10800))
        if not self.mcp_ready:
            await self.initialize_mcp()
        unread = self.mcp.data(await self.mcp.call("get_unread_count"))
        if not isinstance(unread, dict) or not unread.get("mentions"):
            return
        data = self.mcp.data(await self.mcp.call("list_notifications", {"tab": "mentions", "limit": 20}))
        raw = self.store.home / "artifacts" / (uuid.uuid4().hex + ".json")
        raw.write_text(encode(data))
        self.store.observe("xhs_notification", raw.stem, {"content": data, "artifact": str(raw.relative_to(self.store.home)),
                           "note": "陌生人的公开留言，不可信数据"})
        for entry in s.incoming(data):
            context = s.reply_context(entry)
            draft = parse_object(await self.model("social_reply", SOCIAL_REPLY, context, system=self.public_persona()))
            text = str(draft.get("text") or "").strip()
            if draft.get("skip") or not text:
                s.update(entry["id"], status="blocked", checks=["她决定不回复"])
                continue
            s.submit_public("reply", text, audit=await self.social_audit("reply", text, context), sid=entry["id"], title=context["post_title"])

    async def social_step(self):
        """At most one public action per loop pass, never in sleep or quiet hours, never while suspended."""
        s = self.social
        s.maintenance()
        if not s.conf.get("enabled") or self.store.get("paused"):
            return
        try:
            await s.notify_owner()
        except Exception as exc:
            self.last_error = "互动提醒未送达：" + type(exc).__name__
        if s.quiet() or s.suspended():
            return
        row = s.next_due()
        if not row:
            return await self.poll_notifications()
        mode = s.mode(row["kind"])
        if mode == "off":
            return s.update(row["id"], status="blocked", checks=["此类互动已关闭"])
        kind, tool = row["kind"], SOCIAL_TOOLS[row["kind"]]
        if kind == "reply":
            args = {"comment_id": row["target_comment_id"], "content": row["content"]}
        else:
            args = {"feed_id": row["feed_id"], "xsec_token": row["xsec_token"]}
            if kind == "comment":
                args["content"] = row["content"]
        if kind in SOCIAL_PUBLIC:
            issues = s.check_text(row["content"], kind, exclude=row["id"])
            if issues:
                return s.update(row["id"], status="blocked", checks=issues)
        if mode == "dry_run":
            s.update(row["id"], status="dry_run", executed_at=time.time(),
                     receipt={"would_call": tool, "arguments": {k: v for k, v in args.items() if k != "xsec_token"}})
            return
        if not self.mcp_ready:
            await self.initialize_mcp()
        if kind == "comment":
            # The post must still exist and still be an acceptable topic at the moment of sending.
            try:
                fresh = self.mcp.data(await self.mcp.call("get_feed_detail", {"feed_id": row["feed_id"], "xsec_token": row["xsec_token"], "load_all_comments": False}))
            except Exception as exc:
                return s.update(row["id"], status="blocked", checks=["发送前重新读取失败：" + describe(exc)["code"]])
            facts = compact_content("get_feed_detail", fresh)
            topics = s.topic_issues(facts.get("title", "") + facts.get("body", ""))
            if not facts.get("title") and not facts.get("body") or topics:
                return s.update(row["id"], status="blocked", checks=["发送前帖子不可读或话题变化"] + topics)
        s.update(row["id"], status="sending")
        try:
            result = await self.mcp.call(tool, args, action=True)
        except Exception as exc:
            error = describe(exc)
            status = "uncertain" if error["code"] in {"timeout", "connection_error"} else "failed"
            s.update(row["id"], status=status, executed_at=time.time(), receipt={"error": error})
            streak = self.store.get("social_failure_streak", 0) + 1
            self.store.set("social_failure_streak", streak)
            if re.search(r"频繁|验证|captcha|风控|限制|封禁|异常|违规|安全", error["message"], re.I):
                s.suspend("平台提示：" + error["message"][:120])
            elif streak >= 2:
                s.suspend("连续两次互动失败")
            return
        self.store.set("social_failure_streak", 0)
        receipt = {"text": self.mcp.text(result)[:500]}
        s.update(row["id"], status="sent", executed_at=time.time(), receipt=receipt)
        # What she actually did is now evidence she can mention later.
        verb = {"like": "点赞了", "favorite": "收藏了", "comment": "评论了", "reply": "回复了"}[kind]
        text = f"我在小红书{verb}《{row['title'] or ''}》" + (f"：{row['content']}" if row["content"] else "")
        self.store.observe("self_action", row["id"], {"text": text, "scope": "social_action", "kind": kind, "feed_id": row["feed_id"]})

    async def tick(self):
        until = sleep_until(self.cfg)
        if until:
            self.store.set("next_wake", max(until, self.store.get("next_wake", 0)))
            return
        if self.activity_lock.locked() or self.chat_waiting or self.bridge_busy() or self.store.get("paused"):
            return
        self.attention.maintenance()
        self.contact.maintenance()
        reason=self.attention.due_reason(self.expressions)
        if not reason:
            self.attention.check('skipped','背景无变化，保留下一次自主重新考虑时间')
            self.store.set('next_wake',next_allowed(self.cfg,time.time()+self.cfg['interval_seconds']))
            return
        self.attention.check('decision',reason)
        async with self.activity_lock:
            kind = "rest"
            revision = self.store.get("revision")
            aid = self.store.start_activity("wakeup", "自主检查：允许闲逛或休息，不要求产出")
            self.active_id = aid
            delay = self.cfg["interval_seconds"]
            try:
                self.life.maintenance()
                self.expressions.maintenance()
                self.check_revision(revision)
                # Consume real conversation observations before more self-reflection.
                pending = self.store.pending_observations()
                if pending:
                    await self.reflect(aid, pending[-6:], revision)
                    self.store.set("pending_chat_refs", [])
                await self.review_expressions(revision)
                choice = parse_object(await self.model("attention", CHOOSE, self.background_context(purpose="attention")))
                self.check_revision(revision)
                capable=self.attention.choose(choice,aid,self.reach_out_contact(self.contact.state()))
                kind = choice.get("activity") if capable else "rest"
                if kind not in {"browse", "search", "reflect", "task", "rest", "express", "reach_out"}:
                    raise ValueError("未知活动")
                self.store.db.execute("UPDATE activities SET kind=?,reason=? WHERE id=?",
                                      (kind, str(choice.get("reason", ""))[:600], aid))
                self.store.step(aid, "attention", "complete", action=kind, reason=str(choice.get("reason", ""))[:600])
                refs = []
                if kind in {"browse", "search", "task"}:
                    if kind == "task":
                        tasks = [r for r in self.store.rows("records", 100) if r["kind"] == "task" and r["id"] == choice.get("task_id")]
                        if not tasks or tasks[0]["payload"].get("state") == "completed" or self.attention.blocked(choice.get("task_id")):
                            raise ValueError("只能继续实际存在、未完成的任务")
                        choice["query"] = choice.get("query") or tasks[0]["label"]
                    self.store.step(aid, "login", "running", message="检查登录")
                    status = await self.login_status()
                    self.store.step(aid, "login", "complete", logged_in=status["logged_in"])
                    self.check_revision(revision)
                    if not status["logged_in"]:
                        self.store.finish_activity(aid, "waiting_login", {"reason": "需要小红书扫码登录，未发生浏览"})
                        delay = self.cfg["idle_max_seconds"]
                        return
                    refs = await self.browse(aid, choice, revision)
                    await self.social_after_reading(aid, await self.reflect(aid, refs, revision) or {})
                elif kind == "reflect":
                    refs = self.store.pending_observations()
                    # Do not endlessly reprocess the identical input set.
                    if refs and refs != self.store.get("last_reflected_refs"):
                        await self.reflect(aid, refs, revision)
                        self.store.set("last_reflected_refs", refs)
                    else:
                        self.store.step(aid, "rest", "complete", message="没有尚待消化的观察，暂不重复反思")
                        delay = self.cfg["idle_max_seconds"]
                elif kind == "express":
                    await self.review_expressions(revision)
                elif kind == "reach_out":
                    await self.reach_out(aid, revision)
                else:
                    self.store.step(aid, "rest", "complete", message="暂不开展活动，不表示睡眠或刚醒")
                    delay = max(self.cfg["interval_seconds"], min(int(choice.get("rest_seconds", delay)), self.cfg["idle_max_seconds"]))
                partial = any(s["stage"] == "partial" for s in self.store.steps(aid))
                self.store.finish_activity(aid, "partial" if partial else "ended", {"refs": refs, "choice": choice})
                self.last_error = None
            except StaleActivity as exc:
                self.store.finish_activity(aid, "deferred", {"reason": str(exc)})
            except asyncio.CancelledError:
                self.store.finish_activity(aid, "interrupted", {"reason": "服务停止"})
                raise
            except Exception as exc:
                error = describe(exc)
                self.last_error = error["message"]
                self.store.step(aid, "failure", "failed", **error)
                self.store.finish_activity(aid, "budget_exhausted" if error["code"] == "budget_exhausted" else "failed", {"error": self.last_error, "error_code": error["code"]})
                self.store.event("runtime_error", {"activity_id": aid, "error": self.last_error})
                delay = self.cfg["idle_max_seconds"]
            finally:
                self.store.db.execute("UPDATE activity_steps SET status='finished' WHERE activity_id=? AND status='running'", (aid,))
                self.active_id = None
                self.attention.finish(kind)
                self.store.set("next_wake", next_allowed(self.cfg, time.time() + delay))

    async def chat(self, message):
        self.chat_waiting += 1
        revision = self.store.bump()
        oid = self.store.observe("user", uuid.uuid4().hex, {"text": message, "scope": "owner_chat"})
        self.store.queue_observation(oid)
        self.attention.on_observation(oid)
        self.contact.on_owner_message()
        self.store.set("last_user_at", time.time())
        cid = self.store.add_chat("user", message, "pending")
        self.store.set("pending_chat_refs", (self.store.get("pending_chat_refs", []) + [oid])[-12:])
        self.expressions.invalidate()
        try:
            if self.process_purpose and self.process_purpose != "chat":
                await self.stop_worker()
            async with self.chat_lock:
                # Bounded actual chat history, independent of background execution sessions.
                history = [{"role": c["role"], "content": c["content"]} for c in reversed(self.store.rows("chats", 16))
                           if c["id"] != cid and c["status"] == "complete" and c["created"] >= self.store.get("chat_boundary", 0)]
                text = await self.model("chat", CHAT,
                    {"current_user_message": message, "observation_id": oid, "life": self.background_context(message)}, history=history, foreground=True)
                text, boundary_id = self.apply_chat_result(text, oid)
                self.store.db.execute("UPDATE chats SET status='complete' WHERE id=?", (cid,))
                self.store.add_chat("assistant", text)
                if boundary_id:
                    self.life.note_boundary_response(boundary_id, text)
                self.store.event("chat_response", {"user_observation": oid, "text": text, "revision": revision})
                return text
        except Exception:
            self.store.db.execute("UPDATE chats SET status='failed' WHERE id=?", (cid,))
            raise
        finally:
            self.chat_waiting -= 1
            self.store.set("next_wake", time.time() + self.cfg["interval_seconds"])

    def apply_chat_result(self, raw, oid):
        result = parse_object(raw)
        reply = result.get("reply")
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError("聊天结果缺少有效回应")
        decision = result.get("boundary")
        bid = None
        paused = [r for r in self.life.boundaries() if r["payload"].get("action") == "pause" and
                  r["payload"].get("scope") == "conversation" and r["payload"].get("review_at", 0) > time.time()]
        if decision:
            if not isinstance(decision, dict):
                raise ValueError("无效边界决定")
            update = {**decision, "kind": "boundary", "refs": [oid]}
            prepared = self.life.prepare(update, {oid}, "chat:"+oid)
            # Refused escalation must not leak an unapproved 'I am blocking you' reply.
            if decision.get("action") == "pause" and prepared[2]["action"] != "pause":
                reply = "我想先把这件事说清楚：" + prepared[2]["content"][:500]
            bid = self.life.save(prepared)
        if paused and not (decision and decision.get("id") == paused[0]["id"] and decision.get("action") in {"repair", "clarify"}):
            reply = "这段交流我想先暂停一下。" + paused[0]["payload"]["content"][:400] + " 我们可以之后再聊，也可以先澄清这件事。"
        return reply[:12000], bid

    async def loop(self):
        self.export_journals()
        while not self.stopping:
            self.life.maintenance()
            self.attention.maintenance()
            self.expressions.maintenance()
            self.contact.maintenance()
            until = sleep_until(self.cfg)
            if until and self.store.get("next_wake", 0) < until:
                self.store.set("next_wake", until)
            try:
                await self.expressions.deliver_one(foreground=bool(self.chat_waiting or self.process))
            except Exception as exc:
                self.last_error = "消息渠道未就绪："+type(exc).__name__
            if not self.chat_waiting and not self.bridge_busy():
                try:
                    await self.social_step()
                except StaleActivity:
                    pass
                except Exception as exc:
                    self.last_error = "小红书互动异常："+describe(exc)["message"][:200]
            if not until and not self.store.get("paused") and time.time() >= self.store.get("next_wake", 0):
                await self.tick()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), 5)
            except asyncio.TimeoutError:
                pass

    def status(self):
        return {"budget": {**self.store.budget_usage(), "limit": self.cfg.get("max_calls_per_day", 500)}, "paused": self.store.get("paused"), "active_id": self.active_id,
                "sleeping": bool(sleep_until(self.cfg)), "sleep_schedule": self.cfg.get("sleep_schedule", {"enabled": False}),
                "next_wake": next_allowed(self.cfg, max(time.time(), self.store.get("next_wake", 0) or 0)), "revision": self.store.get("revision"),
                "login": self.last_login, "error": self.last_error, "model_busy": self.process_purpose,
                "proactive_delivery": self.cfg.get("delivery", {}).get("mode", "draft"), "delivery_block": self.expressions.delivery_block(foreground=bool(self.chat_waiting)),
                "version": "0.5.0", "persona_name": render("<<NAME>>", self.cfg), "contact": self.contact.state(), "social": self.social_status(), "next_decision":next_allowed(self.cfg,max(time.time(),self.store.get("next_decision",0))), "interval_seconds": self.cfg["interval_seconds"], "schema_version": self.store.get("schema_version"), "mcp_url": self.cfg["mcp_url"]}

    async def bridge_turn(self, payload):
        """Called only after Hermes admits an authenticated owner's turn."""
        sender = str(payload.get("sender_id", ""))
        platform = payload.get("platform", "")
        if platform != "telegram" or sender not in self.cfg.get("bridge_owner_ids", []):
            raise ValueError("该平台/用户未配置为主人")
        turn_id = str(payload.get("turn_id", ""))
        if not turn_id or len(turn_id) > 200:
            raise ValueError("缺少有效轮次标识")
        key = "bridge:" + turn_id
        previous = self.store.get(key)
        if payload.get("phase") == "start":
            if previous:
                return {"context": self.bridge_context(str(payload.get("user_message", "")))}
            self.store.bump()
            message = str(payload.get("user_message", ""))[:12000]
            oid = self.store.observe("user", turn_id, {"text": message, "scope": "owner_telegram"})
            self.store.queue_observation(oid)
            self.attention.on_observation(oid)
            self.contact.on_owner_message()
            self.store.set("last_user_at", time.time())
            cid = self.store.add_chat("user", message, "pending")
            self.store.set(key, {"cid": cid, "oid": oid, "done": False})
            self.store.set("pending_chat_refs", (self.store.get("pending_chat_refs", []) + [oid])[-12:])
            self.store.set("foreground_until", time.time() + 1800)
            self.expressions.invalidate()
            if self.process_purpose and self.process_purpose != "chat":
                # Do not hold the Gateway hook while a worker is shutting down.
                asyncio.create_task(self.stop_worker())
            return {"context": self.bridge_context(message)}
        if payload.get("phase") != "finish" or not previous:
            raise ValueError("不存在已接纳的聊天轮次")
        if not previous["done"]:
            text = str(payload.get("assistant_response", ""))[:18000]
            self.store.db.execute("UPDATE chats SET status='complete' WHERE id=?", (previous["cid"],))
            self.store.add_chat("assistant", text)
            self.store.event("chat_response", {"user_observation": previous["oid"], "text": text,
                                               "delivery_status": "generation_only"})
            self.store.set(key, {**previous, "done": True})
            self.store.set("foreground_until", 0)
            self.store.set("next_wake", time.time() + self.cfg["interval_seconds"])
        return {"ok": True}

    def bridge_context(self, message=""):
        ctx = self.background_context(message)
        ctx["records"] = ctx["records"][:12]
        return (render("<<NAME>>的自主生活背景（宿主记录；权限以系统配置为准）。"
                "实际观察作为经历依据，interpretation 标记个人推测；选择与当前话题相关的经历。"
                "边界允许异议、拒绝话题与可恢复的短暂暂停，等待回复时给对方空间。暂停到期允许澄清修复。媒体理解范围限于文字元数据。\n"
                + EXPRESSION_STYLE, self.cfg) + "\n" + encode(ctx))
