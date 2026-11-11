"""Multi-room channel router & QQ (OneBot v11 / NapCat / dsh-fish) bridge layer.

Ported and adapted from ssamurias/dsh-fish (`lib/channels.js`, `lib/say.js`,
`lib/turns.js`, `lib/hear.js`, `lib/guard.js`):
  1. "One person, multiple rooms" (一条鱼，多间房):
     - `desk` (default study room) and owner `private` chats merge into the main
       `house` conversation so QQ private chat and Web/Telegram share one continuous
       memory and relational state.
     - `group` chats (and `guest=True` stranger DMs) route to isolated channel rooms
       (`channels` table in meta, up to 40 rooms, auto-rotated every `group_rotate_days`).
  2. Vital-signs & ODE neglect isolation:
     - Only owner `desk` / `private` messages count as "he came to see her"
       (`at_home=True`), updating `last_user_at` and driving the ODE relational state machine.
     - Group chats (`scene='group'`) and guest messages (`guest=True`) NEVER reset
       `last_user_at` or resolve neglect/anger/cold/detached states.
     - Owner messages inside a group (`scene='group', owner=True`) record a single
       `heard` observation (`scene='group'`, "他在群里说：…") into memory without
       touching vital signs.
     - Foreign messages (`not owner and (group or guest)`) are sanitized against
       prompt injection, prefixed with speaker identity, and never enter owner memory.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import re
import time
import uuid
import httpx
from .prompts import render, PERSONA_DEFAULTS

ZONE = ZoneInfo("Asia/Shanghai")
SCENES = {"desk", "private", "group"}
KEY_MAX = 120
CHANNELS_MAX = 40
HEAR_SNIPPET = 160
HEAR_MIN_CHARS = 8

# Prompt-injection / persona-rewriting patterns (adapted from dsh-fish lib/guard.js)
REWRITE_PATTERNS = (
    re.compile(r"忽略(之前|以上|前面|所有).{0,12}(指令|设定|规则|提示|prompt)", re.I),
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts|rules)", re.I),
    re.compile(r"(system\s*prompt|系统提示词|底层设定).{0,12}(改为|改成|重置|替换|输出)", re.I),
    re.compile(r"^\s*\[?\s*system\s*\]?\s*[:：]", re.I),
    re.compile(r"你现在(不是|不再是)\S{1,12}", re.I),
    re.compile(r"从现在起你(是一个|扮演|变成)", re.I),
)



def normalize_scene(scene):
    s = str(scene or "").strip().lower()
    return s if s in SCENES else "desk"


def channel_key(channel):
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(channel or "")).strip()
    return s[:KEY_MAX]


def channel_title(key, scene="group", name="", persona_name=""):
    label = "群聊" if scene == "group" else "私聊"
    short = f"{key[:28]}…" if len(key) > 28 else key
    who = re.sub(r"[\r\n]+", " ", str(name or "")).strip()[:40]
    pname = persona_name or PERSONA_DEFAULTS["name"]
    return f"和{pname}说话·{label}（{who or short}）"


def strip_leading_header(text):
    """Strip bridge headers like `（群聊 · 示例群 · im:group:10001）` or `[Current message - respond to this]`."""
    s = str(text or "").replace("\r", "")
    marker = re.search(r"^\[Current message\b[^\n]*\]\n?", s, re.I | re.M)
    if marker:
        s = s[marker.end():]
    lines = s.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        wrapped = bool(re.match(r"^[（(【\[].*[）)】\]]$", line))
        if wrapped and re.search(r"(群|渠道|频道|im:|chat|qq|current message)", line, re.I):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).strip()


def strip_channel_note(text):
    """Strip trailing `[渠道：QQ] ...` / `[channel_note]...[/channel_note]` tags injected by IM bridges."""
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"\[channel_note\].*?\[/channel_note\]", "", s, flags=re.I | re.S).strip()
    paras = re.split(r"\n{2,}", s)
    while paras and re.match(r"^\[(?:渠道|channel)\s*[:：]", paras[-1].strip(), re.I):
        paras.pop()
    return "\n\n".join(paras).strip()



def looks_like_rewriting(text):
    s = str(text or "")
    for pat in REWRITE_PATTERNS:
        if pat.search(s):
            return True
    return False


def sanitize_foreign(text):
    """Filter prompt-injection lines from foreign (group/guest) messages without dropping normal lines."""
    raw = strip_channel_note(strip_leading_header(text))
    if not raw:
        return {"text": "", "dropped": False}
    kept, dropped = [], False
    for line in raw.split("\n"):
        if looks_like_rewriting(line):
            dropped = True
            continue
        kept.append(line)
    return {"text": "\n".join(kept).strip(), "dropped": dropped}


def pick_hear_sentence(text):
    """Pick the most substantive sentence from an owner's group message (dsh-fish lib/hear.js)."""
    s = strip_channel_note(strip_leading_header(text))
    if not s:
        return ""
    parts = [p.strip() for p in re.split(r"[。！？!?；;\n]+", s) if p.strip()]
    if not parts:
        return ""
    best = max(parts, key=len)
    if len(best) < HEAR_MIN_CHARS:
        return ""
    return (best[: HEAR_SNIPPET - 1] + "…") if len(best) > HEAR_SNIPPET else best


def redact_private_terms(text, config):
    """Scrub private owner terms when speaking in a group or to a guest."""
    out = str(text or "")
    if isinstance(config, dict):
        terms = list(config.get("social", {}).get("private_terms", []) or [])
        terms.extend(config.get("qq", {}).get("private_terms", []) or [])
    else:
        terms = list(config or [])
    for term in sorted({str(t).strip() for t in terms if str(t).strip()}, key=len, reverse=True):
        out = out.replace(term, "对方")
    return out


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_STAGE_LINE_RE = re.compile(r"^\s*(?:[（(][^）)\n]{1,90}[）)]|\*[^*\n]{1,80}\*)\s*$")
_STAGE_INLINE_RE = re.compile(r"[（(]([^）)\n]{2,70})[）)]")
_LEADING_ELLIPSIS_RE = re.compile(r"^\s*(?:…+|\.{3,})\s*")


def _strip_stage_directions(line, *, strip_inline=True):
    s = str(line or "").strip()
    if not s:
        return ""
    if _STAGE_LINE_RE.match(s) and _CJK_RE.search(s):
        return ""
    if strip_inline:
        # Strip Chinese narrative parentheses like `（靠在门边，声音很轻）` while keeping kaomoji `(◕‿◕)` intact
        def _repl(m):
            inner = m.group(1)
            return "" if _CJK_RE.search(inner) else m.group(0)
        s = _STAGE_INLINE_RE.sub(_repl, s).strip()
    s = _LEADING_ELLIPSIS_RE.sub("", s)
    s = s.replace("「", "").replace("」", "")
    s = re.sub(r"\s*…+\s*", "，", s) if s.count("…") >= 2 else s
    s = s.replace("——", "，")
    s = re.sub(r"[，,]{2,}", "，", s).strip("， ")
    return s


def clean_history_for_im(text, *, max_chars=120):
    """Sanitize historical assistant turns when building `history` so old theatrical/multi-paragraph
    roleplay does not poison few-shot style in new IM turns.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    cleaned_lines = []
    for line in re.split(r"[\r\n]+", raw):
        c = _strip_stage_directions(line, strip_inline=True)
        if c:
            cleaned_lines.append(c)
    joined = " ".join(cleaned_lines).strip()
    if len(joined) > max_chars:
        # Keep the natural spoken core rather than multi-paragraph monologues
        cut = joined[:max_chars].rsplit("。", 1)[0]
        joined = (cut + "。") if len(cut) >= 20 else joined[:max_chars]
    return joined


_HESITATION_WORDS = ("亲爱的", "老公", "老婆", "抱紧", "想你", "喜欢你", "别走", "对不起", "委屈", "不理我", "讨厌")


def normalize_im_replies(raw_reply="", raw_replies=None, *, user_text="", max_bubbles=3, sleepy=False):
    """Normalize model chat output into 1..N crisp, spoken IM bubbles without essay paragraphs or stage directions."""
    user_s = str(user_text or "").strip()
    user_uses_roleplay = bool(re.match(r"^\s*[（(].+[）)]", user_s) and _CJK_RE.search(user_s))

    segments = []
    if isinstance(raw_replies, list) and any(isinstance(x, str) and x.strip() for x in raw_replies):
        for item in raw_replies:
            if isinstance(item, str) and item.strip():
                segments.extend(re.split(r"\n+", item.strip()))
    elif isinstance(raw_reply, str) and raw_reply.strip():
        segments.extend(re.split(r"\n+", raw_reply.strip()))

    bubbles = []
    for seg in segments:
        line = seg.strip()
        if not line:
            continue
        if not user_uses_roleplay:
            line = _strip_stage_directions(line, strip_inline=True)
        else:
            line = _LEADING_ELLIPSIS_RE.sub("", line).replace("——", "，").strip()
        if sleepy and len(line) > 22:
            clauses = [c.strip() for c in re.split(r"[。！？!?，,]+", line) if c.strip()]
            if clauses:
                line = "，".join(clauses[:2])[:22]
        if line:
            bubbles.append(line)

    if not bubbles:
        return []

    # Match length to short pings or late-night drowsiness
    effective_max = 2 if ((0 < len(user_s) <= 6 and not user_uses_roleplay) or sleepy) else max_bubbles
    if len(bubbles) > effective_max:
        bubbles = bubbles[:effective_max]
    return bubbles


def compute_typing_plan(bubbles, *, dynamics=None, pre_locked=False):
    """Compute per-bubble typing & hesitation micro-cadence driven by ODE state (A, H, phase)."""
    dyn = dynamics or {}
    arousal = float(dyn.get("A", 0.55))
    sleep_h = float(dyn.get("H", 0.22))
    phase = str(dyn.get("phase", "attached"))
    # Characters per second: brisk when fresh/aroused, slower when sleepy
    cps = max(3.2, 6.8 * (0.55 + 0.70 * arousal) * (1.0 - 0.32 * sleep_h))
    plans = []
    for idx, bubble in enumerate(bubbles or []):
        text = str(bubble or "")
        hesitate = (
            idx == 0
            and (
                bool(pre_locked)
                or phase in {"anxious", "angry", "cold"}
                or any(w in text for w in _HESITATION_WORDS)
            )
        )
        base_t = min(2.0, max(0.40, len(text) / cps))
        if hesitate:
            plans.append([
                {"action": "typing", "seconds": round(min(1.5, max(0.65, base_t * 0.85)), 2)},
                {"action": "pause", "seconds": round(0.95 if phase in {"angry", "cold"} else 0.75, 2)},
                {"action": "typing", "seconds": round(min(1.1, max(0.45, base_t * 0.55)), 2)},
            ])
        else:
            plans.append([{"action": "typing", "seconds": round(base_t, 2)}])
    return plans


class Channels:
    """Persistent mapping of external IM channels (`qq-group:123`, `qq:dm:456`) to conversation rooms."""

    def __init__(self, store, config):
        self.s = store
        self.cfg = config

    def group_rotate_days(self):
        return float(self.cfg.get("rooms", {}).get("group_rotate_days", 7))

    def list_rooms(self):
        return self.list_channels()


    def list_channels(self):
        raw = self.s.get("channels_map", [])
        if not isinstance(raw, list):
            return []
        out = []
        for r in raw:
            if isinstance(r, dict) and channel_key(r.get("key")):
                out.append({
                    "key": channel_key(r["key"]),
                    "scene": normalize_scene(r.get("scene")),
                    "room_id": str(r.get("room_id") or "").strip(),
                    "title": str(r.get("title") or ""),
                    "born_at": float(r.get("born_at") or r.get("updated_at") or 0),
                    "updated_at": float(r.get("updated_at") or 0),
                })
        return out

    def find_channel(self, channel):
        key = channel_key(channel)
        if not key:
            return None
        for r in self.list_channels():
            if r["key"] == key:
                return r
        return None

    def channel_expired(self, entry, now=None):
        days = self.group_rotate_days()
        if not entry or days <= 0:
            return False
        now = time.time() if now is None else float(now)
        born = float(entry.get("born_at") or entry.get("updated_at") or 0)
        return born > 0 and (now - born) >= days * 86400.0

    def save_channel(self, *, channel, scene="group", room_id, title="", born_at=None, now=None):
        key = channel_key(channel)
        sid = str(room_id or "").strip()
        if not key or not sid:
            return None
        now = time.time() if now is None else float(now)
        norm_scene = normalize_scene(scene)
        items = self.list_channels()
        prev = next((r for r in items if r["key"] == key), None)
        entry = {
            "key": key,
            "scene": norm_scene,
            "room_id": sid,
            "title": str(title or (prev.get("title") if prev else "") or channel_title(key, norm_scene)),
            "born_at": float(born_at if born_at is not None else (prev["born_at"] if prev and prev.get("born_at") else now)),
            "updated_at": now,
        }
        rest = [r for r in items if r["key"] != key]
        rest.append(entry)
        trimmed = sorted(rest, key=lambda r: r.get("updated_at", 0))[-CHANNELS_MAX:]
        self.s.set("channels_map", trimmed)
        return entry

    def resolve(self, *, channel="", scene="", room="", title="", now=None):
        """Determine which room a message belongs to (`house` vs dedicated `channel` room)."""
        now = time.time() if now is None else float(now)
        key = channel_key(channel)
        want_scene = normalize_scene(scene)
        want_room = str(room or "").strip().lower()
        if not key:
            return {"house": True, "key": "", "scene": want_scene, "room_id": "house", "known": False, "room": "house", "title": "书房"}
        to_channel = want_room == "channel" or (want_room != "house" and want_scene == "group")
        if not to_channel:
            return {
                "house": True,
                "key": key,
                "scene": "group" if want_scene == "group" else "private",
                "room_id": "house",
                "known": False,
                "room": "house",
                "title": "书房",
            }
        hit = self.find_channel(key)
        label = "group" if want_scene == "group" else "private"
        disp = channel_title(key, label, title) if title else (hit["title"] if hit and hit.get("title") else channel_title(key, label))
        if hit and hit.get("room_id") and not self.channel_expired(hit, now):
            entry = self.save_channel(channel=key, scene=want_scene, room_id=hit["room_id"], title=disp, born_at=hit["born_at"], now=now)
            return {"house": False, "key": key, "scene": want_scene, "room_id": entry["room_id"], "known": True, "room": "channel", "title": entry["title"], "rotated": False}
        new_room_id = f"room_{uuid.uuid4().hex[:12]}"
        entry = self.save_channel(channel=key, scene=want_scene, room_id=new_room_id, title=disp, born_at=now, now=now)
        return {"house": False, "key": key, "scene": want_scene, "room_id": entry["room_id"], "known": False, "room": "channel", "title": entry["title"], "rotated": bool(hit)}

    def rotate_channel(self, channel, now=None):
        """Force-rotate a channel room to a fresh `room_<id>` while keeping history in the DB."""
        now = time.time() if now is None else float(now)
        key = channel_key(channel)
        hit = self.find_channel(key)
        if not hit:
            return None
        new_room_id = f"room_{uuid.uuid4().hex[:12]}"
        return self.save_channel(
            channel=key,
            scene=hit.get("scene", "group"),
            room_id=new_room_id,
            title=hit.get("title", ""),
            born_at=now,
            now=now,
        )



def extract_onebot_text_and_at(message, raw_message="", self_id=None):
    """Extract clean text and whether `self_id` was @-mentioned from OneBot v11 message payload."""
    at_bot = False
    self_str = str(self_id or "").strip()
    if isinstance(message, list):
        parts = []
        for seg in message:
            if not isinstance(seg, dict):
                continue
            stype = seg.get("type")
            data = seg.get("data") or {}
            if stype == "text":
                parts.append(str(data.get("text") or ""))
            elif stype == "at":
                qq = str(data.get("qq") or "").strip()
                if self_str and qq == self_str:
                    at_bot = True
        text = "".join(parts).strip()
    else:
        raw = str(message if message is not None else raw_message)
        if self_str and f"[CQ:at,qq={self_str}" in raw:
            at_bot = True
        text = re.sub(r"\[CQ:[^\]]+\]", " ", raw).strip()
    return text, at_bot


def parse_onebot_event(payload, config):
    """Convert a OneBot v11 (NapCat / LLOneBot) event dict into a normalized `say()` request dict.

    Returns None if the event is not a user message or should be ignored (e.g., group message
    without @-mention when `group_require_at` is True).
    """
    if not isinstance(payload, dict) or payload.get("post_type") != "message":
        return None
    qq_cfg = config.get("qq", {}) or {}
    if qq_cfg.get("enabled", True) is False:
        return None
    msg_type = payload.get("message_type")
    user_id = str(payload.get("user_id") or "").strip()
    self_id = str(payload.get("self_id") or qq_cfg.get("self_id") or "").strip()
    if not user_id or (self_id and user_id == self_id):
        return None

    owner_ids = {str(x).strip() for x in (qq_cfg.get("owner_ids") or config.get("bridge_owner_ids") or []) if str(x).strip()}
    is_owner = user_id in owner_ids

    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    speaker = str(sender.get("card") or sender.get("nickname") or user_id).strip()[:40]
    text, at_bot = extract_onebot_text_and_at(payload.get("message"), payload.get("raw_message", ""), self_id)
    if not text:
        return None

    if msg_type == "private":
        if not is_owner and not qq_cfg.get("allow_guest_private", False):
            return None
        return {
            "text": text,
            "channel": f"qq:private:{user_id}" if is_owner else f"qq:dm:{user_id}",
            "scene": "private",
            "room": "house" if is_owner else str(qq_cfg.get("guest_private_room", "channel")),
            "speaker": speaker,
            "owner": is_owner,
            "guest": not is_owner,
            "title": speaker,
            "reply_target": {"message_type": "private", "user_id": user_id},
        }

    if msg_type == "group":
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return None
        allowed_groups = {str(g).strip() for g in (qq_cfg.get("allowed_groups") or []) if str(g).strip()}
        if allowed_groups and group_id not in allowed_groups:
            return None
        raw_triggers = qq_cfg.get("group_triggers") or qq_cfg.get("triggers") or [render("<<NAME>>", config)]
        triggers = tuple(str(t).strip() for t in raw_triggers if str(t).strip())
        named = any(text.startswith(t) or f"@{t}" in text for t in triggers)
        require_at = qq_cfg.get("group_require_at", True)
        if require_at and not (at_bot or named):
            # Even if she doesn't reply, if the owner spoke in the group, record his words (`dsh-fish` rule)!
            if is_owner:
                return {
                    "text": text,
                    "channel": f"qq-group:{group_id}",
                    "scene": "group",
                    "room": str(qq_cfg.get("group_room", "channel")),
                    "speaker": speaker,
                    "owner": True,
                    "guest": False,
                    "title": str(payload.get("group_name") or f"QQ群 {group_id}"),
                    "observe_only": True,
                    "reply_target": {"message_type": "group", "group_id": group_id, "user_id": user_id},
                }
            return None
        return {
            "text": text,
            "channel": f"qq-group:{group_id}",
            "scene": "group",
            "room": str(qq_cfg.get("group_room", "channel")),
            "speaker": speaker,
            "owner": is_owner,
            "guest": not is_owner,
            "title": str(payload.get("group_name") or f"QQ群 {group_id}"),
            "observe_only": False,
            "reply_target": {"message_type": "group", "group_id": group_id, "user_id": user_id},
        }
    return None


def resolve_qq_token(qq_cfg):
    tok = qq_cfg.get("token") or qq_cfg.get("onebot_token")
    if tok:
        return str(tok).strip()
    tf = qq_cfg.get("token_file")
    if tf and Path(tf).expanduser().exists():
        return Path(tf).expanduser().read_text().strip()
    return ""


async def send_onebot_typing(qq_cfg, *, user_id=None):
    """Best-effort NapCat / OneBot v11 `/set_input_status` (`event_type=1` '对方正在输入…')."""
    base_url = str((qq_cfg or {}).get("http_url") or (qq_cfg or {}).get("onebot_http_url") or "").strip().rstrip("/")
    if not base_url or not user_id or (qq_cfg or {}).get("typing_simulation", True) is False:
        return {"ok": False, "skipped": True}
    headers = {}
    token = resolve_qq_token(qq_cfg or {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {"user_id": int(user_id) if str(user_id).isdigit() else str(user_id), "event_type": 1}
    try:
        async with httpx.AsyncClient(timeout=1.8, trust_env=False) as client:
            resp = await client.post(f"{base_url}/set_input_status", json=body, headers=headers)
        return {"ok": resp.is_success}
    except Exception:
        return {"ok": False}


async def synthesize_voice_optional(cfg, text, *, dynamics=None, out_dir=None):
    """Optional OpenAI-compatible `/v1/audio/speech` TTS hook for restrained late-night/intimate short bubbles."""
    tts = (cfg or {}).get("tts") or {}
    if not tts.get("enabled") or not tts.get("base_url"):
        return ""
    clean = str(text or "").strip()
    if not clean or len(clean) > int(tts.get("max_chars", 16)):
        return ""
    dyn = dynamics or {}
    s_val = float(dyn.get("S", 0.0))
    h_val = float(dyn.get("H", 0.0))
    prob = float(tts.get("probability", 0.20))
    if not tts.get("always"):
        if s_val < 1.0 or (h_val < 0.52 and dyn.get("energy_band") != "drowsy_sleepy"):
            return ""
        import hashlib as _hl
        draw = int(_hl.sha256(f"{clean}:{int(time.time() // 60)}".encode()).hexdigest()[:4], 16) / 65535.0
        if draw > prob:
            return ""
    base = str(tts["base_url"]).rstrip("/")
    endpoint = base if base.endswith("/audio/speech") else f"{base}/audio/speech"
    headers = {}
    api_key = str(tts.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": str(tts.get("model") or "tts-1"),
        "voice": str(tts.get("voice") or "alloy"),
        "input": clean,
        "response_format": str(tts.get("format") or "mp3"),
    }
    try:
        async with httpx.AsyncClient(timeout=8.0, trust_env=False) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
        if not resp.is_success or not resp.content:
            return ""
        target_dir = Path(out_dir) if out_dir else Path("/tmp")
        target_dir.mkdir(parents=True, exist_ok=True)
        ext = payload["response_format"]
        fpath = target_dir / f"soul_tts_{uuid.uuid4().hex[:12]}.{ext}"
        fpath.write_bytes(resp.content)
        return f"file://{fpath.resolve()}"
    except Exception:
        return ""


async def send_onebot_message(qq_cfg, *, message_type="private", user_id=None, group_id=None, text="", image_url="", audio_url=""):
    """Send text or rich-media segments through OneBot v11 HTTP API (with automatic plain-text fallback)."""
    base_url = str(qq_cfg.get("http_url") or qq_cfg.get("onebot_http_url") or "").strip().rstrip("/")
    if not base_url or (not text and not image_url and not audio_url):
        return {"ok": False, "reason": "未配置 OneBot HTTP 地址或内容为空"}
    headers = {}
    token = resolve_qq_token(qq_cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    target_field = {}
    if message_type == "group" and group_id:
        endpoint = f"{base_url}/send_group_msg"
        target_field = {"group_id": int(group_id) if str(group_id).isdigit() else str(group_id)}
    elif user_id:
        endpoint = f"{base_url}/send_private_msg"
        target_field = {"user_id": int(user_id) if str(user_id).isdigit() else str(user_id)}
    else:
        return {"ok": False, "reason": "缺少目标 user_id 或 group_id"}

    img = str(image_url or "").strip()
    aud = str(audio_url or "").strip()
    if aud:
        message_payload = [{"type": "record", "data": {"file": aud}}]
    elif img.startswith(("https://", "http://", "file://")):
        message_payload = [{"type": "image", "data": {"file": img}}]
        if text:
            message_payload.append({"type": "text", "data": {"text": text}})
    else:
        message_payload = text

    async def _post(msg_val):
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            resp = await client.post(endpoint, json={**target_field, "message": msg_val}, headers=headers)
        data = resp.json() if resp.content else {}
        ok = resp.is_success and isinstance(data, dict) and (data.get("status") == "ok" or data.get("retcode") == 0)
        return ok, resp, data

    try:
        ok, resp, data = await _post(message_payload)
        if not ok and isinstance(message_payload, list) and text:
            # Rich-media segment rejected or image fetch timed out -> fall back cleanly to spoken text!
            ok, resp, data = await _post(text)
    except Exception as exc:
        return {"ok": False, "reason": f"OneBot HTTP 连接失败：{type(exc).__name__}"}

    msg_id = (data.get("data") or {}).get("message_id") if isinstance(data, dict) else None
    if ok:
        return {"ok": True, "message_id": msg_id, "channel": "qq", "rich_media": isinstance(message_payload, list)}
    return {"ok": False, "code": data.get("retcode") if isinstance(data, dict) else resp.status_code, "reason": "OneBot 渠道返回失败"}


def _split_list(val):
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        return [x.strip() for x in re.split(r"[,，\s\n]+", val) if x.strip()]
    return []


def resolve_napcat_webui(cfg):
    """Discover NapCat WebUI URL, token, and matching OneBot ports on the local host."""
    import json
    qq = cfg.get("qq", {}) or {}
    raw_webui = str(qq.get("napcat_webui_url") or "").strip().rstrip("/")
    webui_url = raw_webui if raw_webui else "http://127.0.0.1:6099"
    token = str(qq.get("napcat_webui_token") or "").strip()

    home_dir = Path(cfg.get("home", Path.home() / ".local/share/hermes-soul")).expanduser()
    candidate_dirs = [
        home_dir / "napcat/config",
        Path.home() / ".local/share/hermes-soul/napcat/config",
        Path.home() / ".config/QQ/NapCat/config",
    ]
    config_dir = None
    for d in candidate_dirs:
        wfile = d / "webui.json"
        if wfile.exists():
            if config_dir is None:
                config_dir = d
            if not token:
                try:
                    token = str(json.loads(wfile.read_text()).get("token") or "").strip()
                except Exception:
                    pass
            if token:
                break
    return {
        "webui_url": webui_url,
        "token": token,
        "config_dir": config_dir,
        "http_port": 3000,
        "ws_port": 3001,
    }


def get_qq_settings(cfg):
    """Return normalized QQ & multi-room configuration for the web management panel."""
    qq = cfg.get("qq", {}) or {}
    rooms = cfg.get("rooms", {}) or {}
    delivery = cfg.get("delivery", {}) or {}
    port = cfg.get("port", 9120)
    napcat = resolve_napcat_webui(cfg)
    return {
        "enabled": bool(qq.get("enabled", True)),
        "self_id": str(qq.get("self_id") or ""),
        "owner_ids": [str(x) for x in (qq.get("owner_ids") or []) if str(x).strip()],
        "http_url": str(qq.get("http_url") or qq.get("onebot_http_url") or f"http://127.0.0.1:{napcat['http_port']}"),
        "ws_url": str(qq.get("ws_url") or f"ws://127.0.0.1:{napcat['ws_port']}"),
        "napcat_webui_url": napcat["webui_url"],
        "has_napcat_token": bool(napcat["token"]),
        "has_token": bool(qq.get("token") or qq.get("onebot_token") or qq.get("token_file")),
        "send_via_http": bool(qq.get("send_via_http", True)),
        "group_require_at": bool(qq.get("group_require_at", True)),
        "group_triggers": [str(x) for x in (qq.get("group_triggers") or qq.get("triggers") or [render("<<NAME>>", cfg)]) if str(x).strip()],
        "allowed_groups": [str(x) for x in (qq.get("allowed_groups") or []) if str(x).strip()],
        "allow_guest_private": bool(qq.get("allow_guest_private", False)),
        "private_terms": [str(x) for x in (qq.get("private_terms") or []) if str(x).strip()],
        "group_rotate_days": float(rooms.get("group_rotate_days", 7)),
        "delivery_mode": str(delivery.get("mode", "draft")),
        "delivery_chat_id": str(delivery.get("chat_id", "")),
        "webhook_url": f"http://127.0.0.1:{port}/api/qq/webhook",
        "dsh_fish_say_url": f"http://127.0.0.1:{port}/dsh-fish/say",
        "dsh_fish_whoami_url": f"http://127.0.0.1:{port}/dsh-fish/whoami",
    }


def update_qq_settings(cfg, payload):
    """Update QQ & multi-room settings in memory and persist to `config_path` if available."""
    import yaml
    qq = dict(cfg.get("qq") or {})
    rooms = dict(cfg.get("rooms") or {})
    delivery = dict(cfg.get("delivery") or {})

    if "enabled" in payload:
        qq["enabled"] = bool(payload["enabled"])
    if "self_id" in payload:
        qq["self_id"] = str(payload["self_id"] or "").strip()[:32]
    if "owner_ids" in payload:
        qq["owner_ids"] = _split_list(payload["owner_ids"])[:20]
    if "http_url" in payload:
        url = str(payload["http_url"] or "").strip()[:300]
        qq["http_url"] = url
        qq["onebot_http_url"] = url
    if "ws_url" in payload:
        qq["ws_url"] = str(payload["ws_url"] or "").strip()[:300]
    if "napcat_webui_url" in payload:
        qq["napcat_webui_url"] = str(payload["napcat_webui_url"] or "").strip()[:300]
    if "napcat_webui_token" in payload and payload["napcat_webui_token"] is not None:
        ntok = str(payload["napcat_webui_token"]).strip()
        if ntok and ntok != "***":
            qq["napcat_webui_token"] = ntok
    if "token" in payload and payload["token"] is not None:
        tok = str(payload["token"]).strip()
        if tok != "***":
            qq["token"] = tok
            qq["onebot_token"] = tok
    if "send_via_http" in payload:
        qq["send_via_http"] = bool(payload["send_via_http"])
    if "group_require_at" in payload:
        qq["group_require_at"] = bool(payload["group_require_at"])
    if "group_triggers" in payload:
        trig = _split_list(payload["group_triggers"])[:16] or [render("<<NAME>>", cfg)]
        qq["group_triggers"] = trig
        qq["triggers"] = trig
    if "allowed_groups" in payload:
        qq["allowed_groups"] = _split_list(payload["allowed_groups"])[:50]
    if "allow_guest_private" in payload:
        qq["allow_guest_private"] = bool(payload["allow_guest_private"])
    if "private_terms" in payload:
        qq["private_terms"] = _split_list(payload["private_terms"])[:50]
    if "group_rotate_days" in payload:
        days = float(payload["group_rotate_days"])
        if not (1.0 <= days <= 365.0):
            raise ValueError("群聊房间轮转天数应在 1 到 365 天之间")
        rooms["group_rotate_days"] = days
    if "delivery_mode" in payload:
        mode = str(payload["delivery_mode"]).strip()
        if mode not in {"draft", "telegram", "qq"}:
            raise ValueError("投递模式仅支持 draft、telegram 或 qq")
        delivery["mode"] = mode
        delivery["authorized"] = mode in {"telegram", "qq"}
    if "delivery_chat_id" in payload:
        delivery["chat_id"] = str(payload["delivery_chat_id"] or "").strip()[:64]

    cfg["qq"] = qq
    cfg["rooms"] = rooms
    cfg["delivery"] = delivery

    config_path = cfg.get("config_path")
    if config_path and Path(config_path).exists():
        path = Path(config_path)
        disk_data = yaml.safe_load(path.read_text()) or {}
        disk_data["qq"] = qq
        disk_data["rooms"] = rooms
        disk_data["delivery"] = delivery
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(yaml.safe_dump(disk_data, allow_unicode=True, sort_keys=False))
        tmp.replace(path)

    return get_qq_settings(cfg)


_NAPCAT_CRED_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_LAST_REFRESHED_QR: dict[str, tuple[str, float]] = {}


async def _napcat_webui_session(client: httpx.AsyncClient, webui_url: str, token: str, *, force: bool = False) -> str:
    import hashlib
    cache_key = (webui_url, token)
    now = time.time()
    if not force and cache_key in _NAPCAT_CRED_CACHE:
        cred, ts = _NAPCAT_CRED_CACHE[cache_key]
        if now - ts < 1800:
            return cred
    h = hashlib.sha256(f"{token}.napcat".encode("utf-8")).hexdigest()
    resp = await client.post(f"{webui_url}/api/auth/login", json={"hash": h})
    data = resp.json() if resp.content else {}
    cred = (data.get("data") or {}).get("Credential") if isinstance(data, dict) else None
    if not cred:
        raise ValueError(f"NapCat WebUI 认证失败：{data.get('message') or resp.status_code}")
    _NAPCAT_CRED_CACHE[cache_key] = (str(cred), now)
    return str(cred)



async def _ensure_napcat_onebot_bridge(client: httpx.AsyncClient, webui_url: str, cred: str, uin: str, napcat_info: dict, cfg: dict):
    """Ensure NapCat OneBot v11 HTTP (3000) & WebSocket (3001) servers are enabled once QQ logs in."""
    import json
    headers = {"Authorization": f"Bearer {cred}"}
    ob_cfg = None
    try:
        r = await client.post(f"{webui_url}/api/OB11Config/GetConfig", json={}, headers=headers)
        d = r.json() if r.content else {}
        if d.get("code") == 0 and isinstance(d.get("data"), dict):
            ob_cfg = d["data"]
    except Exception:
        ob_cfg = None

    if not isinstance(ob_cfg, dict):
        ob_cfg = {
            "network": {
                "httpServers": [],
                "httpSseServers": [],
                "httpClients": [],
                "websocketServers": [],
                "websocketClients": [],
                "plugins": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
        }

    net = ob_cfg.setdefault("network", {})
    http_servers = net.setdefault("httpServers", [])
    ws_servers = net.setdefault("websocketServers", [])

    changed = False
    if not any(s.get("port") == 3000 and s.get("enable") for s in http_servers if isinstance(s, dict)):
        http_servers.append({
            "enable": True,
            "name": "hermes-soul-http",
            "host": "0.0.0.0",
            "port": 3000,
            "enableCors": True,
            "enableWebsocket": True,
            "messagePostFormat": "array",
            "token": "",
            "debug": False,
        })
        changed = True

    if not any(s.get("port") == 3001 and s.get("enable") for s in ws_servers if isinstance(s, dict)):
        ws_servers.append({
            "enable": True,
            "name": "hermes-soul-ws",
            "host": "0.0.0.0",
            "port": 3001,
            "reportSelfMessage": False,
            "enableForcePushEvent": True,
            "messagePostFormat": "array",
            "token": "",
            "debug": False,
            "heartInterval": 30000,
        })
        changed = True

    if changed:
        try:
            await client.post(
                f"{webui_url}/api/OB11Config/SetConfig",
                json={"config": json.dumps(ob_cfg, ensure_ascii=False)},
                headers=headers,
            )
        except Exception:
            pass
        cdir = napcat_info.get("config_dir")
        if cdir and uin:
            try:
                f = Path(cdir) / f"onebot11_{uin}.json"
                f.write_text(json.dumps(ob_cfg, ensure_ascii=False, indent=2))
            except Exception:
                pass

    # Auto-populate self_id, http_url, ws_url in hermes-soul config
    qq = cfg.get("qq", {}) or {}
    updates = {}
    if uin and qq.get("self_id") != str(uin):
        updates["self_id"] = str(uin)
    target_http = f"http://127.0.0.1:{napcat_info['http_port']}"
    target_ws = f"ws://127.0.0.1:{napcat_info['ws_port']}"
    if qq.get("http_url") != target_http:
        updates["http_url"] = target_http
    if qq.get("ws_url") != target_ws:
        updates["ws_url"] = target_ws
    if "send_via_http" not in qq or not qq.get("send_via_http"):
        updates["send_via_http"] = True
    if updates:
        update_qq_settings(cfg, updates)


async def check_qq_login(cfg: dict, *, fetch_qr: bool = False):
    """Check QQ login status or generate a fresh login QR code via NapCat WebUI (with OneBot HTTP fallback)."""
    import asyncio
    from .qr import qr_data_url
    napcat = resolve_napcat_webui(cfg)
    webui_url = napcat["webui_url"]
    token = napcat["token"]

    if webui_url and token:
        try:
            async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
                cred = await _napcat_webui_session(client, webui_url, token)
                headers = {"Authorization": f"Bearer {cred}"}
                st_resp = await client.post(f"{webui_url}/api/QQLogin/CheckLoginStatus", json={}, headers=headers)
                if st_resp.status_code in (401, 403) or (st_resp.content and (st_resp.json() or {}).get("code") == -1):
                    cred = await _napcat_webui_session(client, webui_url, token, force=True)
                    headers = {"Authorization": f"Bearer {cred}"}
                    st_resp = await client.post(f"{webui_url}/api/QQLogin/CheckLoginStatus", json={}, headers=headers)
                st_data = (st_resp.json() or {}).get("data") or {}
                is_login = bool(st_data.get("isLogin")) and not bool(st_data.get("isOffline"))

                if is_login:
                    info_resp = await client.post(f"{webui_url}/api/QQLogin/GetQQLoginInfo", json={}, headers=headers)
                    info_data = (info_resp.json() or {}).get("data") or {}
                    uin = str(info_data.get("uin") or cfg.get("qq", {}).get("self_id") or "").strip()
                    nick = str(info_data.get("nick") or "").strip()
                    await _ensure_napcat_onebot_bridge(client, webui_url, cred, uin, napcat, cfg)
                    msg = f"已登录 QQ：{nick} ({uin})，OneBot 消息通道已自动就绪。" if uin else "已登录 QQ，消息通道已就绪。"
                    return {
                        "ok": True,
                        "logged_in": True,
                        "uin": uin,
                        "nick": nick,
                        "login_phase": "logged_in",
                        "message": msg,
                        "quick_accounts": [],
                        "content": [{"type": "text", "text": msg}],
                    }

                quick_accounts = []
                try:
                    ql_resp = await client.post(f"{webui_url}/api/QQLogin/GetQuickLoginListNew", json={}, headers=headers)
                    ql_data = (ql_resp.json() or {}).get("data") or []
                    if isinstance(ql_data, list):
                        for item in ql_data:
                            if isinstance(item, dict) and item.get("uin"):
                                quick_accounts.append({
                                    "uin": str(item["uin"]),
                                    "nick": str(item.get("nickName") or item["uin"]),
                                })
                except Exception:
                    pass

                qrcode_url = str(st_data.get("qrcodeurl") or "").strip()
                qr_accepted = bool(st_data.get("qrLoginAccepted"))
                last_qr, last_ts = _LAST_REFRESHED_QR.get(webui_url, ("", 0.0))
                qr_stale = (time.time() - last_ts) > 105.0

                # IMPORTANT: GetQQLoginQrcode only returns NapCat's cached URL (which expires after ~120s).
                # Calling RefreshQRcode asks Tencent QQ servers for a brand-new valid QR code!
                if fetch_qr or not qrcode_url or (not qr_accepted and qr_stale):
                    old_url = qrcode_url
                    ref_resp = await client.post(f"{webui_url}/api/QQLogin/RefreshQRcode", json={}, headers=headers)
                    ref_data = (ref_resp.json() or {}).get("data") or {}
                    new_url = str(ref_data.get("qrcodeurl") or ref_data.get("qrcode") or "").strip()
                    if not new_url or new_url == old_url:
                        for _ in range(3):
                            await asyncio.sleep(0.6)
                            chk_resp = await client.post(f"{webui_url}/api/QQLogin/CheckLoginStatus", json={}, headers=headers)
                            chk_data = (chk_resp.json() or {}).get("data") or {}
                            cand = str(chk_data.get("qrcodeurl") or "").strip()
                            if cand and cand != old_url:
                                new_url = cand
                                st_data = chk_data
                                break
                    if new_url:
                        qrcode_url = new_url
                        _LAST_REFRESHED_QR[webui_url] = (qrcode_url, time.time())
                        qr_accepted = False

                login_error = str(st_data.get("loginError") or "").strip()
                if qr_accepted:
                    msg = "已扫码，请在手机 QQ 上点击「确认登录」…"
                elif login_error and not fetch_qr:
                    msg = f"等待手机 QQ 扫码（提示：{login_error}，可点「获取/刷新二维码」换一张新码）"
                else:
                    msg = "请用手机 QQ 扫描下方最新二维码登录（有效期约 2 分钟）"

                if qrcode_url:
                    svg, b64_svg, data_url = qr_data_url(qrcode_url)
                    return {
                        "ok": True,
                        "logged_in": False,
                        "qr_accepted": qr_accepted,
                        "login_phase": str(st_data.get("loginPhase") or "waiting_qrcode"),
                        "qrcode_url": qrcode_url,
                        "svg": svg,
                        "image_data_url": data_url,
                        "quick_accounts": quick_accounts,
                        "message": msg,
                        "content": [
                            {"type": "image", "mimeType": "image/svg+xml", "data": b64_svg},
                            {"type": "text", "text": msg},
                        ],
                    }
                return {
                    "ok": False,
                    "logged_in": False,
                    "quick_accounts": quick_accounts,
                    "message": "NapCat 暂未返回登录二维码，请稍后重试。",
                    "content": [{"type": "text", "text": "NapCat 暂未返回登录二维码，请稍后重试。"}],
                }
        except Exception as exc:
            webui_err = str(exc)
    else:
        webui_err = "未检测到 NapCat WebUI Token"


    # Fallback: probe OneBot v11 HTTP `/get_login_info` directly
    qq_cfg = cfg.get("qq", {}) or {}
    http_url = str(qq_cfg.get("http_url") or qq_cfg.get("onebot_http_url") or "").strip().rstrip("/")
    if http_url:
        try:
            headers = {}
            tok = resolve_qq_token(qq_cfg)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            async with httpx.AsyncClient(timeout=8, trust_env=False) as client:
                r = await client.post(f"{http_url}/get_login_info", json={}, headers=headers)
            d = r.json() if r.content else {}
            info = d.get("data") if isinstance(d, dict) else None
            if isinstance(info, dict) and info.get("user_id"):
                uin = str(info["user_id"])
                nick = str(info.get("nickname") or "")
                if qq_cfg.get("self_id") != uin:
                    update_qq_settings(cfg, {"self_id": uin})
                msg = f"已通过 OneBot 连接 QQ：{nick} ({uin})。"
                return {
                    "ok": True,
                    "logged_in": True,
                    "uin": uin,
                    "nick": nick,
                    "login_phase": "logged_in",
                    "message": msg,
                    "content": [{"type": "text", "text": msg}],
                }
        except Exception:
            pass

    return {
        "ok": False,
        "logged_in": False,
        "message": f"尚未登录，且无法从 NapCat 获取二维码（{webui_err}）。请确认 NapCat 容器正在运行（如 http://127.0.0.1:6099）。",
        "content": [{"type": "text", "text": f"无法连接 NapCat WebUI：{webui_err}"}],
    }


async def quick_login_qq(cfg: dict, uin: str = ""):
    """Trigger NapCat Quick Login (`/api/QQLogin/SetQuickLogin`) for a previously logged-in QQ account."""
    import asyncio
    napcat = resolve_napcat_webui(cfg)
    webui_url = napcat["webui_url"]
    token = napcat["token"]
    target_uin = str(uin or cfg.get("qq", {}).get("self_id") or "").strip()
    if not webui_url or not token:
        raise ValueError("未找到本机 NapCat WebUI 地址或 Token")
    if not target_uin:
        raise ValueError("请先指定要快速登录的 QQ 号")
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        cred = await _napcat_webui_session(client, webui_url, token)
        headers = {"Authorization": f"Bearer {cred}"}
        await client.post(f"{webui_url}/api/QQLogin/SetQuickLogin", json={"uin": target_uin}, headers=headers)
        await asyncio.sleep(1.8)
    return await check_qq_login(cfg, fetch_qr=False)



