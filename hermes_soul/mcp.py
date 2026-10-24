"""Streamable HTTP MCP client with an explicit read-only exploration surface."""
import asyncio
import json
from pathlib import Path
import httpx

READ_TOOLS = frozenset({"check_login_status", "list_feeds", "search_feeds", "get_feed_detail", "user_profile",
                        "get_unread_count", "list_notifications", "get_my_profile"})
# Only reachable through the social layer (action=True). Publishing, deleting and undoing stay closed.
ACTION_TOOLS = frozenset({"like_feed", "favorite_feed", "post_comment_to_feed", "reply_notification"})


class MCPError(RuntimeError):
    pass


class Xiaohongshu:
    def __init__(self, url, token_file=None):
        self.url = url
        self.headers = {"Accept": "application/json, text/event-stream"}
        if token_file:
            self.headers["Authorization"] = "Bearer " + Path(token_file).read_text().strip()
        self.session = None
        self.counter = 0
        self.lock = asyncio.Lock()

    async def _rpc(self, method, params=None, notify=False):
        self.counter += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            body["id"] = self.counter
        headers = dict(self.headers)
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            resp = await client.post(self.url, json=body, headers=headers)
        if resp.status_code == 404 and self.session:
            self.session = None
            raise MCPError("MCP 会话已失效，请重试")
        resp.raise_for_status()
        if resp.headers.get("mcp-session-id"):
            self.session = resp.headers["mcp-session-id"]
        if notify or not resp.content:
            return {}
        if "text/event-stream" in resp.headers.get("content-type", ""):
            messages = []
            for block in resp.text.replace("\r\n", "\n").split("\n\n"):
                data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
                if data:
                    messages.append(json.loads(data))
            value = next((v for v in messages if v.get("id") == body["id"]), None)
            if value is None:
                raise MCPError("MCP SSE 中缺少响应")
        else:
            value = resp.json()
        if value.get("error"):
            raise MCPError(str(value["error"]))
        return value.get("result", {})

    async def initialize(self):
        async with self.lock:
            info = await self._rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                 "clientInfo": {"name": "hermes-soul", "version": "0.1.0"}})
            self.headers["MCP-Protocol-Version"] = info.get("protocolVersion", "2024-11-05")
            await self._rpc("notifications/initialized", notify=True)
            return await self._rpc("tools/list")

    async def call(self, name, arguments=None, *, login=False, action=False):
        if action:
            if name not in ACTION_TOOLS or any((arguments or {}).get(k) for k in ("unlike", "unfavorite")):
                raise MCPError("只开放点赞、收藏、评论与回复评论；发布、删除和取消操作不开放")
        elif name not in READ_TOOLS and not (login and name == "get_login_qrcode"):
            raise MCPError("没有开放发布、删除登录信息等操作；互动只能经过社交审核层")
        async with self.lock:
            result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise MCPError(self.text(result)[:1000])
        return result

    @staticmethod
    def text(result):
        return "\n".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")

    @classmethod
    def data(cls, result):
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        text = cls.text(result)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}

    async def login_status(self):
        result = await self.call("check_login_status")
        data = self.data(result)
        if isinstance(data, dict) and isinstance(data.get("is_logged_in"), bool):
            return {"logged_in": data["is_logged_in"], "message": self.text(result)}
        text = self.text(result)
        # Upstream tool currently returns a human-readable status, not structured JSON.
        yes = ("已登录" in text or "Logged in" in text) and "未登录" not in text
        return {"logged_in": yes, "message": text}


def feed_cards(value):
    """Normalize only actual feed objects; never invent a detail-read from a cover."""
    cards = []
    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            card = obj.get("noteCard") or obj.get("note_card")
            if card and obj.get("id"):
                cards.append({"id": obj["id"], "xsec_token": obj.get("xsecToken", obj.get("xsec_token", "")),
                              "title": card.get("displayTitle", card.get("title", "")),
                              "type": card.get("type", "unknown"), "user": card.get("user", {}),
                              "media_understood": False})
            else:
                for val in obj.values():
                    if isinstance(val, (list, dict)):
                        walk(val)
    walk(value)
    seen = set()
    return [c for c in cards if not (c["id"] in seen or seen.add(c["id"]))]
