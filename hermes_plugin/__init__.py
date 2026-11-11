"""Hermes lifecycle bridge. Admitted owner Telegram & QQ turns are shared.

No inference in hooks; no system prompt changes; no dependence on Cognitive.
"""
import json
import logging
import os
from pathlib import Path
import threading
import urllib.request
import yaml

log = logging.getLogger(__name__)


def register(ctx):
    path = Path(os.environ.get("HERMES_SOUL_CONFIG", Path.home() / ".config/hermes-soul/config.yaml"))
    if not path.exists():
        return
    config = yaml.safe_load(path.read_text())
    # The autonomous worker profile must never feed its own generated prompts back as user facts.
    if getattr(ctx, "profile_name", "default") not in ("default", "", None):
        return
    owners = {str(v) for v in config.get("bridge_owner_ids", [])} | {
        str(v) for v in (config.get("qq") or {}).get("owner_ids", [])
    }
    token = Path(config["access_token_file"]).expanduser().read_text().strip()
    url = f"http://127.0.0.1:{config.get('port', 9120)}/api/bridge"
    turns = {}
    lock = threading.Lock()

    def call(payload):
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def before(**kw):
        sender = str(kw.get("sender_id") or "")
        platform = str(kw.get("platform") or "")
        if platform not in {"telegram", "qq", "qqbot"} or sender not in owners:
            return None
        tid = kw.get("turn_id")
        if not tid:
            return None
        chat_type = str(kw.get("chat_type") or kw.get("scene") or "private").lower()
        scene = "group" if chat_type in {"group", "guild", "channel"} else "private"
        payload = {
            "phase": "start",
            "turn_id": tid,
            "sender_id": sender,
            "platform": platform,
            "scene": scene,
            "user_message": str(kw.get("user_message", "")),
        }
        try:
            result = call(payload)
        except Exception as exc:
            log.warning("hermes-soul background unavailable: %s", exc)
            return None
        with lock:
            if len(turns) > 256:
                turns.pop(next(iter(turns)))
            turns[tid] = (sender, platform, scene)
        return {"context": result["context"], "target": "user_message"}

    def after(**kw):
        tid = kw.get("turn_id")
        with lock:
            info = turns.pop(tid, None)
        if not info:
            return
        sender, platform, scene = info if isinstance(info, tuple) else (info, "telegram", "private")
        try:
            call({
                "phase": "finish",
                "turn_id": tid,
                "sender_id": sender,
                "platform": platform,
                "scene": scene,
                "assistant_response": str(kw.get("assistant_response", "")),
            })
        except Exception as exc:
            log.warning("hermes-soul turn completion not recorded: %s", exc)

    ctx.register_hook("pre_llm_call", before)
    ctx.register_hook("post_llm_call", after)
