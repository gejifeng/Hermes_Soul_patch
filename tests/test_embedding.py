import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest

from hermes_soul.embedding import (
    EmbeddingError,
    EmbeddingStore,
    OpenAIEmbeddingClient,
    cosine_similarity,
    resolve_embeddings_url,
)
from hermes_soul.runtime import Runtime
from hermes_soul.store import Store


def _semantic_vec(text, dim=4):
    """Deterministic toy semantic space:
    axis 0: art/painting, axis 1: coffee/drinks, axis 2: outings/parks, axis 3: relationship
    """
    t = str(text)
    v = [0.02, 0.02, 0.02, 0.02]
    if any(w in t for w in ("水彩", "淡彩", "插画", "风景", "速写", "调色")):
        v[0] += 1.0
    if any(w in t for w in ("咖啡", "手冲", "拿铁", "豆子", "挂耳")):
        v[1] += 1.0
    if any(w in t for w in ("植物园", "出游", "踏青", "郊游", "去哪玩")):
        v[2] += 1.0
    if any(w in t for w in ("恋爱", "纪念日", "想念", "失联", "七夕", "冷战", "亲密")):
        v[3] += 1.0
    return v[:dim]


class _MockOpenAIEmbeddingsServer:
    def __init__(self):
        self.requests = []
        self.fail_next = None
        self.reverse_indices = False
        self.return_base64 = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8")
                body = json.loads(raw)
                outer.requests.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                })
                if outer.fail_next:
                    code, msg = outer.fail_next
                    outer.fail_next = None
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": {"message": msg}}).encode("utf-8"))
                    return

                inputs = body["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                # OpenAI/DashScope reject empty strings
                if any(x == "" for x in inputs):
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":{"message":"empty input"}}')
                    return

                dim = int(body.get("dimensions", 4))
                items = []
                for idx, text in enumerate(inputs):
                    vec = _semantic_vec(text, dim=dim)
                    if outer.return_base64:
                        emb = base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")
                    else:
                        emb = vec
                    items.append({"object": "embedding", "index": idx, "embedding": emb})
                if outer.reverse_indices:
                    items.reverse()
                resp = {
                    "object": "list",
                    "model": body["model"],
                    "data": items,
                    "usage": {"prompt_tokens": sum(len(x) for x in inputs), "total_tokens": sum(len(x) for x in inputs)},
                }
                payload = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt, *args):
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class EmbeddingReservedFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.server = _MockOpenAIEmbeddingsServer()

    async def asyncTearDown(self):
        self.server.close()
        self.store.close()
        self.temp.cleanup()

    def test_url_normalization_and_standby_zero_side_effects(self):
        self.assertEqual(resolve_embeddings_url("https://api.siliconflow.cn/v1/"), "https://api.siliconflow.cn/v1/embeddings")
        self.assertEqual(resolve_embeddings_url("http://127.0.0.1:8000/v1/embeddings"), "http://127.0.0.1:8000/v1/embeddings")

        # Standby mode (`enabled: False`): never touches SQLite schema or network
        client = OpenAIEmbeddingClient({"enabled": False})
        self.assertFalse(client.is_configured())
        estore = EmbeddingStore(self.store, client)
        oid = self.store.observe("xiaohongshu", "n1", {"text": "水彩"})
        rid = self.store.record("interest", "水彩速写", {"content": "喜欢水彩", "state": "noticed"}, [oid])
        self.assertFalse(estore.index_record(rid, "水彩速写", {"content": "喜欢水彩"}))
        self.assertEqual(estore.search("淡彩风景"), [])
        tables = {r[0] for r in self.store.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertNotIn("record_embeddings", tables)
        self.assertEqual(self.server.requests, [])

    async def test_openai_protocol_batching_base64_and_circuit_breaker(self):
        key_file = Path(self.temp.name) / "emb.key"
        key_file.write_text("sk-test-vendor\n", encoding="utf-8")
        client = OpenAIEmbeddingClient({
            "enabled": True,
            "base_url": self.server.base_url,
            "model": "Qwen/Qwen3-Embedding-4B",
            "api_key_file": str(key_file),
            "batch_size": 2,
            "dimensions": 0,  # 0 => omitted from payload for maximum vendor compatibility
            "backoff_seconds": 30,
        })

        # Out-of-order `index` + empty string sanitization + batching (3 items => 2 HTTP requests)
        self.server.reverse_indices = True
        vecs = await client.aembed(["水彩速写", "", "手冲咖啡"])
        self.assertEqual(len(vecs), 3)
        self.assertEqual(len(self.server.requests), 2)
        first_req = self.server.requests[0]
        self.assertEqual(first_req["path"], "/v1/embeddings")
        self.assertEqual(first_req["auth"], "Bearer sk-test-vendor")
        self.assertNotIn("dimensions", first_req["body"])
        # Despite reversed server `data`, index 0 is art ("水彩速写") and index 2 is coffee ("手冲咖啡")
        self.assertLess(cosine_similarity(vecs[0], vecs[2]), 0.3)

        # Base64 float32 response compatibility + explicit Matryoshka `dimensions=3`
        self.server.return_base64 = True
        client.dimensions = 3
        probe_res = client.probe(["水彩速写", "淡彩风景", "手冲咖啡"])
        self.assertTrue(probe_res["ok"])
        self.assertEqual(probe_res["dimension"], 3)
        self.assertEqual(self.server.requests[-1]["body"]["dimensions"], 3)
        self.assertGreater(probe_res["similarities"][0]["cosine"], 0.95)
        self.server.return_base64 = False

        # Error + circuit breaker fast-fail
        self.server.fail_next = (503, "upstream overloaded")
        with self.assertRaises(EmbeddingError) as ctx:
            await client.aembed(["测试"])
        self.assertEqual(ctx.exception.status_code, 503)
        req_count = len(self.server.requests)
        with self.assertRaises(EmbeddingError) as ctx2:
            await client.aembed(["测试2"])
        self.assertEqual(ctx2.exception.code, "circuit_open")
        self.assertEqual(len(self.server.requests), req_count)  # Fast-failed without hitting network!

    async def test_sqlite_vector_store_dedup_search_duplicate_and_graph_adjacency(self):
        client = OpenAIEmbeddingClient({
            "enabled": True,
            "base_url": self.server.base_url,
            "model": "BAAI/bge-m3",
            "backoff_seconds": 0,
        })
        estore = EmbeddingStore(self.store, client)
        oid = self.store.observe("xiaohongshu", "n1", {"text": "素材"})
        r_art1 = self.store.record("interest", "水彩速写", {"content": "水彩风景练习", "state": "exploring"}, [oid])
        r_art2 = self.store.record("interest", "淡彩插画", {"content": "插画调色构图", "state": "noticed"}, [oid])
        r_coffee = self.store.record("interest", "手冲豆子", {"content": "浅烘手冲咖啡", "state": "preferred"}, [oid])
        r_expired = self.store.record("situation", "过期事项", {"content": "水彩展", "state": "expired"}, [oid])

        # Incremental backfill skips expired records and deduplicates by SHA256 on repeat calls
        n1 = await estore.abackfill_records()
        self.assertEqual(n1, 3)
        reqs_after_backfill = len(self.server.requests)
        self.assertEqual(await estore.abackfill_records(), 0)
        self.assertFalse(await estore.aindex_record(r_art1, "水彩速写", {"content": "水彩风景练习"}))
        self.assertEqual(len(self.server.requests), reqs_after_backfill)

        # Semantic search finds "手冲豆子" from "挂耳拿铁" even with zero overlapping characters!
        hits = await estore.asearch("挂耳拿铁", min_score=0.6)
        self.assertEqual([h["id"] for h in hits], [r_coffee])

        # Near-duplicate interest detection finds "水彩速写" from "淡彩风景"
        dup = estore.find_near_duplicate("淡彩风景", {"content": "插画速写"}, kind="interest", threshold=0.90)
        self.assertIsNotNone(dup)
        self.assertIn(dup["id"], {r_art1, r_art2})

        # Graph Laplacian adjacency W_ij connects the two art topics strongly while separating coffee
        adj = estore.topic_adjacency([r_art1, r_art2, r_coffee], threshold=0.5)
        self.assertGreater(adj[r_art1][r_art2], 0.95)
        self.assertNotIn(r_coffee, adj[r_art1])
        self.assertNotIn(r_expired, adj)

        # Changing `model` invalidates old embeddings cleanly on next backfill
        client.model = "Qwen/Qwen3-Embedding-0.6B"
        self.assertIsNone(estore.get_vector(r_art1))
        self.assertEqual(estore.backfill_records(), 3)

    async def test_full_runtime_integration_recall_consolidation_wakeup_and_diffusion(self):
        """End-to-end test of all 4 live Runtime semantic integrations."""
        self.store.close()
        rt = Runtime({
            "home": self.temp.name,
            "mcp_url": "http://127.0.0.1:1/mcp",
            "interval_seconds": 720,
            "idle_max_seconds": 720,
            "max_calls_per_day": 50,
            "max_browse_steps": 3,
            "embedding": {
                "enabled": True,
                "base_url": self.server.base_url,
                "model": "qwen3-embedding-4b",
                "backoff_seconds": 0,
            },
        })
        try:
            # 1. Near-synonym interest consolidation: "水彩速写" + "淡彩插画" merge into 1 exploring interest!
            o1 = rt.store.observe("xiaohongshu", "note_a", {"text": "水彩风景", "tool": "get_feed_detail"})
            o2 = rt.store.observe("xiaohongshu", "note_b", {"text": "淡彩构图", "tool": "get_feed_detail"})
            id1 = rt.life.save(rt.life.prepare({"kind": "interest", "label": "水彩速写", "content": "喜欢水彩速写", "refs": [o1]}, {o1}, "act1"))
            rt.embeddings.index_record_ids([id1])
            id2 = rt.life.save(rt.life.prepare({"kind": "interest", "label": "淡彩插画", "content": "练习风景调色", "refs": [o2]}, {o2}, "act2"))
            self.assertEqual(id1, id2)
            self.assertEqual(rt.life.get_record(id1)["payload"]["state"], "exploring")

            # 2. Hybrid semantic recall in chat background_context with zero shared characters
            o_coffee = rt.store.observe("xiaohongshu", "note_c", {"text": "浅烘手冲豆子", "tool": "get_feed_detail"})
            coffee_id = rt.life.save(rt.life.prepare({"kind": "belief", "label": "手冲豆子水温", "content": "浅烘咖啡适合92度萃取", "refs": [o_coffee]}, {o_coffee}, "act3"))
            await rt.embeddings.abackfill_records()
            chat_ctx = rt.background_context("挂耳拿铁怎么泡", purpose="chat")
            self.assertIn(coffee_id, [r["id"] for r in chat_ctx["records"]])

            # 3. Semantic parked-topic wakeup when owner replies without literal word overlap
            u_plan = rt.store.observe("user", "u_plan", {"text": "周末去哪玩等我确认"})
            plan_id = rt.life.save(rt.life.prepare({"kind": "situation", "label": "出游踏青计划", "content": "等他回复去哪玩", "waiting_for": "user_reply", "refs": [u_plan]}, {u_plan}, "act4"))
            rt.attention.sync(plan_id)
            rt.embeddings.index_record_ids([plan_id])
            self.assertTrue(rt.attention.blocked(plan_id))

            import time
            from unittest.mock import patch
            with patch("time.time", return_value=time.time() + 10):
                u_reply = rt.store.observe("user", "u_reply", {"text": "周日去植物园逛逛"})
                rt.attention.on_observation(u_reply)
            self.assertFalse(rt.attention.blocked(plan_id))

            # 4. Semantic relational anchor inhibits romantic topic (even without literal regex hit) in detached state
            u_gift = rt.store.observe("user", "u_gift", {"text": "七夕礼物"})
            rel_id = rt.life.save(rt.life.prepare({"kind": "intention", "label": "准备七夕惊喜", "content": "七夕想念礼物", "refs": [u_gift]}, {u_gift}, "act5"))
            await rt.embeddings.abackfill_records()
            rt.store.set("dynamics_state", {**rt.dynamics.raw_state(), "phase": "detached", "S": -1.0, "C": 0.75})
            cands = {c["id"]: c for c in rt.attention.candidates(dynamics=rt.store.get("dynamics_state"))}
            self.assertGreater(cands[id1]["score"], cands[rel_id]["score"])
            self.assertTrue(rt.status()["embedding"]["enabled"])
            self.assertGreaterEqual(rt.status()["embedding"]["indexed_records"], 3)
        finally:
            rt.store.close()
            self.store = Store(self.temp.name)


if __name__ == "__main__":
    unittest.main()
