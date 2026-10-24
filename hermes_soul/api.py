from contextlib import asynccontextmanager, suppress
import asyncio
import hmac
import json
from pathlib import Path
import time

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .config import load_config
from .runtime import Runtime
from .memory import recall, active
from .failures import describe


class Message(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class Interest(BaseModel):
    topic: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=1500)


class Correction(Message):
    resolve: bool = False


def create_app(config=None):
    cfg = config or load_config()
    token = Path(cfg["access_token_file"]).read_text().strip()
    if len(token) < 24:
        raise ValueError("界面访问令牌过短")

    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(cfg)
        app.state.runtime = runtime
        loop = asyncio.create_task(runtime.loop())
        try:
            yield
        finally:
            runtime.stopping = True
            loop.cancel()
            await runtime.stop_worker()
            with suppress(asyncio.CancelledError):
                await loop
            runtime.store.close()

    app = FastAPI(title="Hermes Soul", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    async def auth(request: Request):
        header = request.headers.get("authorization", "")
        if not hmac.compare_digest(header, "Bearer " + token):
            raise HTTPException(401, "需要访问令牌")

    @app.exception_handler(Exception)
    async def error_handler(request, exc):
        error = describe(exc)
        return JSONResponse(status_code=400 if isinstance(exc, ValueError) else 500,
                            content={"detail": error["message"], "error": error})

    @app.get("/")
    async def index():
        return FileResponse(Path(__file__).parent / "web/index.html")

    @app.get("/health")
    async def health():
        return {"ok": True, "version": "0.3.0"}

    @app.get("/api/state", dependencies=[Depends(auth)])
    async def state():
        r = app.state.runtime
        return {"status": r.status(), "records": r.store.rows("records", 100),
                "activities": r.store.rows("activities", 30), "chats": list(reversed(r.store.rows("chats", 60))),
                "outbox": r.expressions.rows(), "calls": r.store.rows("calls", 15),
                "situations": [r.life.get_record(rid) for rid in r.store.get('focus_ids',[]) if r.life.get_record(rid) and not r.attention.blocked(rid)], "boundaries": r.life.boundaries(),
                "attention": {"choice":r.store.get('focus_choice'),"candidates":r.attention.candidates(r.reach_out_contact(r.contact.state())),"parked":r.attention.parked()},
                "feelings": [item for item in r.life.kind_rows('affect') if active(item)],
                "activation_checks": [dict(x) for x in r.store.db.execute('SELECT * FROM activation_checks ORDER BY id DESC LIMIT 30')],
                "pending_observations": r.store.db.execute("SELECT count(*) FROM observation_queue WHERE status='pending'").fetchone()[0]}

    @app.post("/api/attention/{rid}/{action}", dependencies=[Depends(auth)])
    async def attention_control(rid: str, action: str, request: Request):
        r=app.state.runtime
        if action not in {"park", "resume"} or not r.life.get_record(rid):
            raise HTTPException(400,"请选择已有记录并暂放或恢复")
        value=await request.json()
        wait=value.get('waiting_for','operator')
        at=value.get('reconsider_at')
        if at is not None and (not isinstance(at,(int,float)) or not time.time()<at<time.time()+30*86400):
            raise HTTPException(400,"重新考虑时间应在未来30天内")
        with r.store.transaction():
            r.attention.set(rid,'parked' if action=='park' else 'active',wait if action=='park' else '',at if action=='park' else None,'用户在管理页'+('暂放' if action=='park' else '恢复'))
            r.store.bump()
            r.store.set('next_decision',0)
        r.expressions.invalidate()
        return {"ok":True}

    @app.get("/api/activities/{aid}", dependencies=[Depends(auth)])
    async def activity(aid: str):
        r = app.state.runtime
        row = r.store.db.execute("SELECT * FROM activities WHERE id=?", (aid,)).fetchone()
        if not row:
            raise HTTPException(404, "活动不存在")
        return {**dict(row), "checkpoint": json.loads(row["checkpoint"]), "steps": r.store.steps(aid)}

    @app.get("/api/recall", dependencies=[Depends(auth)])
    async def search(q: str = ""):
        return {"records": recall(app.state.runtime.store, q[:2000])}

    @app.get("/api/pending", dependencies=[Depends(auth)])
    async def pending():
        rows = app.state.runtime.store.db.execute("SELECT q.*,o.source,o.created FROM observation_queue q JOIN observations o ON o.id=q.observation_id WHERE q.status='pending' ORDER BY o.created LIMIT 100").fetchall()
        return {"observations": [dict(r) for r in rows]}

    @app.post("/api/observations/{oid}/retry", dependencies=[Depends(auth)])
    async def retry_observation(oid: str):
        r = app.state.runtime
        changed = r.store.db.execute("UPDATE observation_queue SET attempts=0 WHERE observation_id=? AND status='pending'", (oid,)).rowcount
        if not changed:
            raise HTTPException(404, "没有待消化的这条观察")
        r.store.event("operator_retry", {"observation_id": oid})
        return {"ok": True}

    @app.get("/api/records/{rid}/history", dependencies=[Depends(auth)])
    async def history(rid: str):
        r = app.state.runtime
        if not r.life.get_record(rid):
            raise HTTPException(404, "记录不存在")
        rows = r.store.db.execute("SELECT * FROM events WHERE kind='record_revision' AND json_extract(payload,'$.id')=? ORDER BY created DESC LIMIT 100", (rid,)).fetchall()
        return {"history": [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]}

    @app.post("/api/records/{rid}/correct", dependencies=[Depends(auth)])
    async def correct(rid: str, value: Correction):
        r = app.state.runtime
        r.life.correct(rid, value.text, value.resolve)
        r.expressions.invalidate()
        r.export_journals()
        return {"record": r.life.get_record(rid)}

    @app.post("/api/records/{rid}/restore/{eid}", dependencies=[Depends(auth)])
    async def restore(rid: str, eid: str):
        r = app.state.runtime
        row = r.store.db.execute("SELECT payload FROM events WHERE id=? AND kind='record_revision'", (eid,)).fetchone()
        event = json.loads(row[0]) if row else {}
        before = event.get("before")
        if event.get("id") != rid or not before:
            raise HTTPException(400, "该版本没有可恢复的先前记录")
        with r.store.transaction():
            r.store.record(before["kind"], before["label"], json.loads(before["payload"]), json.loads(before["refs"]), rid)
            r.store.bump()
            r.store.event("operator_restore", {"record_id": rid, "revision_event": eid})
        r.expressions.invalidate()
        r.export_journals()
        return {"record": r.life.get_record(rid)}

    @app.post("/api/relationship/resume", dependencies=[Depends(auth)])
    async def resume_relationship():
        r = app.state.runtime
        restored = []
        for item in r.life.boundaries():
            if item["payload"].get("action") in {"pause", "refuse"}:
                r.life.correct(item["id"], "用户通过管理入口恢复交流，此边界已解除。", resolve=True)
                restored.append(item["id"])
        r.expressions.invalidate()
        return {"restored": restored}

    @app.post("/api/expressions/{eid}/{action}", dependencies=[Depends(auth)])
    async def expression(eid: str, action: str):
        if action not in {"wait", "discard"}:
            raise HTTPException(400, "管理端只接受暂放或放弃，不跳过投递检查")
        r = app.state.runtime
        r.expressions.decide(eid, action, "用户在管理页面选择"+action, r.store.get("revision"), 3600)
        return {"ok": True}

    @app.get("/api/social", dependencies=[Depends(auth)])
    async def social():
        r = app.state.runtime
        return {"status": r.social_status(), "actions": r.social.rows(60)}

    @app.post("/api/social/{sid}/{action}", dependencies=[Depends(auth)])
    async def social_decision(sid: str, action: str):
        r = app.state.runtime
        if sid == "control":
            if action == "suspend":
                r.social.s.set("social_suspended_until", time.time() + 30 * 86400)
            elif action == "resume":
                r.social.resume()
            else:
                raise HTTPException(400, "未知操作")
            r.store.event("operator_control", {"action": "social_" + action})
            return r.social_status()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, "只能批准或拒绝")
        try:
            r.social.decide(sid, action)
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        r.wake.set()
        return {"ok": True}

    @app.get("/api/export", dependencies=[Depends(auth)])
    async def export():
        r = app.state.runtime
        tables = ("meta", "records", "observations", "activities", "activity_steps", "events", "chats", "outbox", "expression_meta", "observation_queue", "calls", "attention_state", "activation_checks", "social_actions")
        data = {t: [dict(x) for x in r.store.db.execute("SELECT * FROM "+t)] for t in tables}
        return JSONResponse({"version": "0.3.0", "exported_at": time.time(), "data": data},
                            headers={"Content-Disposition": 'attachment; filename="hermes-soul-export.json"'})

    @app.get("/api/observations/{oid}", dependencies=[Depends(auth)])
    async def observation(oid: str):
        row = app.state.runtime.store.db.execute("SELECT * FROM observations WHERE id=?", (oid,)).fetchone()
        if not row:
            raise HTTPException(404, "观察不存在")
        return {**dict(row), "payload": json.loads(row["payload"])}

    @app.post("/api/chat", dependencies=[Depends(auth)])
    async def chat(message: Message):
        return {"reply": await app.state.runtime.chat(message.text)}

    @app.post("/api/bridge", dependencies=[Depends(auth)])
    async def bridge(request: Request):
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "需要 JSON 对象")
        return await app.state.runtime.bridge_turn(payload)

    @app.post("/api/control/{action}", dependencies=[Depends(auth)])
    async def control(action: str):
        r = app.state.runtime
        if action not in {"pause", "resume", "wake", "stop", "new", "reset-budget"}:
            raise HTTPException(400, "未知操作")
        if action == "reset-budget":
            r.store.reset_budget()
            r.last_error = None
            r.store.set("next_decision", 0)
            r.store.set("next_wake", time.time() + r.cfg["interval_seconds"])
        elif action == "pause":
            r.store.set("paused", True)
            r.store.bump()
            if r.process_purpose != "chat":
                await r.stop_worker()
        elif action == "resume":
            r.store.set("paused", False)
            r.store.set("next_wake", time.time() + r.cfg["interval_seconds"])
        elif action == "wake":
            if r.status()["sleeping"]:
                raise HTTPException(409, "当前处于睡眠时段，结束后自动恢复")
            if r.store.get("paused"):
                raise HTTPException(409, "请先恢复自主活动")
            r.store.set("force_decision",True)
            r.store.set("next_wake", 0)
        elif action == "stop":
            r.store.bump()
            await r.stop_worker()
        else:
            r.store.bump()
            # Retain the actual history while starting a separate bounded chat context.
            r.store.set("chat_boundary", time.time())
        r.store.event("operator_control", {"action": action})
        r.wake.set()
        return r.status()

    @app.post("/api/interests", dependencies=[Depends(auth)])
    async def interest(value: Interest):
        r = app.state.runtime
        r.store.bump()
        oid = r.store.observe("user", "interest_suggestion", value.model_dump())
        rid = r.store.record("interest", value.topic,
             {"content": value.description, "state": "suggested", "origin": "user_suggestion",
              "epistemic_status": "suggestion_not_personal_preference"}, [oid])
        r.store.queue_observation(oid)
        r.expressions.invalidate()
        return {"id": rid}

    @app.get("/api/xhs/status", dependencies=[Depends(auth)])
    async def xhs_status():
        return await app.state.runtime.login_status()

    @app.post("/api/xhs/login", dependencies=[Depends(auth)])
    async def login():
        r = app.state.runtime
        if not r.mcp_ready:
            await r.initialize_mcp()
        result = await r.mcp.call("get_login_qrcode", login=True)
        return result

    return app
