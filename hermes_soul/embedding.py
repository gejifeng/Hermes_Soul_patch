"""OpenAI-compatible Embedding client and SQLite vector index for semantic recall,
near-synonym interest consolidation, and Graph-Laplacian topic field diffusion.

Compatible with any provider exposing OpenAI `POST /v1/embeddings`:
  - Self-hosted: llama.cpp (`llama-server`), vLLM, SGLang, HuggingFace TEI, Ollama (`/v1`)
  - Cloud APIs: OpenAI, SiliconFlow, DashScope (compatible-mode), Volcengine Ark, Zhipu, Jina, OpenRouter

Quick connectivity & similarity self-test:
  python -m hermes_soul.embedding --config ~/.config/hermes-soul/config.yaml --probe "水彩速写" "淡彩风景" "手冲咖啡"
"""
from array import array
import argparse
import base64
from collections import OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
import httpx

from .memory import active

RELATIONAL_ANCHOR_TEXT = "和他的恋爱关系与等待回复 — 亲密关系沟通、想念、约会与失联冷战"


class EmbeddingError(RuntimeError):
    """Raised when an OpenAI-compatible embedding request fails or returns invalid data."""

    def __init__(self, message, *, status_code=None, code="embedding_error"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def resolve_embeddings_url(base_url):
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        raise EmbeddingError("未配置 embedding.base_url", code="missing_config")
    if url.endswith("/embeddings"):
        return url
    return url + "/embeddings"


def resolve_api_key(ecfg):
    key_file = str(ecfg.get("api_key_file") or "").strip()
    if key_file:
        p = Path(key_file).expanduser()
        if not p.exists():
            raise EmbeddingError(f"embedding.api_key_file 不存在: {p}", code="missing_key_file")
        return p.read_text(encoding="utf-8").strip()
    key_env = str(ecfg.get("api_key_env") or "").strip()
    if key_env:
        return os.environ.get(key_env, "").strip()
    raw = str(ecfg.get("api_key") or "").strip()
    return os.path.expandvars(raw).strip() if raw else ""


def l2_normalize(vec):
    arr = array("f", (float(x) for x in vec))
    norm_sq = sum(x * x for x in arr)
    if norm_sq <= 1e-24:
        return arr
    inv = 1.0 / math.sqrt(norm_sq)
    return array("f", (x * inv for x in arr))


def pack_vector(vec, *, normalize=True):
    arr = l2_normalize(vec) if normalize else array("f", (float(x) for x in vec))
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes(), len(arr)


def unpack_vector(blob):
    arr = array("f")
    arr.frombytes(memoryview(blob))
    if sys.byteorder != "little":
        arr.byteswap()
    return arr


def cosine_similarity(vec_a, vec_b):
    """Cosine similarity for two L2-normalized float sequences."""
    if len(vec_a) != len(vec_b) or not vec_a:
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    return max(-1.0, min(1.0, float(dot)))


def canonical_record_text(label, payload):
    p = payload if isinstance(payload, dict) else {}
    body = str(p.get("attraction") or p.get("content") or p.get("text") or "").strip()
    extra = str(p.get("open_question") or p.get("scope") or "").strip()
    parts = [str(label or "").strip()]
    if body and body != parts[0]:
        parts.append(body)
    if extra:
        parts.append(extra)
    return " — ".join(x for x in parts if x)[:4000]


def text_sha256(text):
    return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:32]


def _decode_embedding_item(raw_vec):
    if isinstance(raw_vec, list):
        if not raw_vec:
            raise EmbeddingError("Embedding 返回了空向量", code="invalid_response")
        return l2_normalize(raw_vec)
    if isinstance(raw_vec, str):
        try:
            buf = base64.b64decode(raw_vec)
        except Exception as exc:
            raise EmbeddingError("无法解码 base64 embedding 向量", code="invalid_response") from exc
        if not buf or len(buf) % 4 != 0:
            raise EmbeddingError("base64 embedding 字节长度非法", code="invalid_response")
        count = len(buf) // 4
        return l2_normalize(struct.unpack(f"<{count}f", buf))
    raise EmbeddingError(f"不支持的 embedding 数据类型: {type(raw_vec).__name__}", code="invalid_response")


class OpenAIEmbeddingClient:
    """Standard OpenAI `/v1/embeddings` client supporting sync/async execution and LRU caching."""

    def __init__(self, config):
        ecfg = config.get("embedding", config) if isinstance(config, dict) else {}
        self.cfg = dict(ecfg or {})
        self.enabled = bool(self.cfg.get("enabled", False))
        self.base_url = str(self.cfg.get("base_url") or "").strip()
        self.model = str(self.cfg.get("model") or "").strip()
        dims = self.cfg.get("dimensions")
        self.dimensions = int(dims) if dims not in (None, "", 0, "0") else None
        self.batch_size = max(1, min(int(self.cfg.get("batch_size", 24)), 256))
        self.max_input_chars = max(128, int(self.cfg.get("max_input_chars", 4000)))
        self.timeout = max(2.0, float(self.cfg.get("timeout_seconds", 25.0)))
        self.encoding_format = str(self.cfg.get("encoding_format") or "").strip() or None
        self.trust_env = bool(self.cfg.get("trust_env", False))
        self.proxy = str(self.cfg.get("proxy") or "").strip() or None
        self.extra_body = dict(self.cfg.get("extra_body") or {})
        self.extra_headers = dict(self.cfg.get("headers") or {})

        self.backoff_seconds = max(0.0, float(self.cfg.get("backoff_seconds", 60.0)))
        self._backoff_until = 0.0
        self._consecutive_failures = 0
        self.total_requests = 0
        self.total_tokens = 0
        self._cache = OrderedDict()
        self._cache_max = max(32, int(self.cfg.get("cache_size", 256)))

    @property
    def model_tag(self):
        dim_suffix = f":{self.dimensions}" if self.dimensions else ""
        return f"{self.model}{dim_suffix}"

    def is_configured(self):
        return bool(self.enabled and self.base_url and self.model)

    def sanitize_one(self, text):
        s = str(text or "").strip()[:self.max_input_chars]
        return s if s else " "

    def _sanitize_inputs(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        return [self.sanitize_one(t) for t in texts]

    def get_cached(self, text):
        cleaned = self.sanitize_one(text)
        key = (self.model_tag, text_sha256(cleaned))
        vec = self._cache.get(key)
        if vec is not None:
            self._cache.move_to_end(key)
        return vec

    def _put_cached(self, cleaned_text, vec):
        key = (self.model_tag, text_sha256(cleaned_text))
        self._cache[key] = vec
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_max:
            self._cache.popitem(last=False)

    def _build_request(self, batch):
        if not self.model:
            raise EmbeddingError("未配置 embedding.model", code="missing_config")
        url = resolve_embeddings_url(self.base_url)
        headers = {"Content-Type": "application/json", **self.extra_headers}
        api_key = resolve_api_key(self.cfg)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {"model": self.model, "input": batch}
        if self.dimensions is not None and self.dimensions > 0:
            payload["dimensions"] = self.dimensions
        if self.encoding_format:
            payload["encoding_format"] = self.encoding_format
        if self.extra_body:
            payload.update(self.extra_body)
        return url, headers, payload

    def _check_circuit(self, now=None):
        now = time.time() if now is None else float(now)
        if self._backoff_until > now:
            wait = round(self._backoff_until - now, 1)
            raise EmbeddingError(f"Embedding 接口处于退避冷却中（剩余 {wait}s）", code="circuit_open")

    def _record_failure(self):
        self._consecutive_failures += 1
        if self.backoff_seconds > 0:
            factor = min(8, 2 ** min(self._consecutive_failures - 1, 3))
            self._backoff_until = time.time() + self.backoff_seconds * factor

    def _record_success(self, usage=None):
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self.total_requests += 1
        if isinstance(usage, dict):
            self.total_tokens += int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0)

    def _parse_response(self, response, expected_count):
        if response.status_code >= 400:
            detail = ""
            try:
                err_json = response.json()
                err_obj = err_json.get("error", err_json) if isinstance(err_json, dict) else err_json
                detail = err_obj.get("message", str(err_obj)) if isinstance(err_obj, dict) else str(err_obj)
            except Exception:
                detail = response.text[:300]
            raise EmbeddingError(
                f"Embedding HTTP {response.status_code}: {detail[:300]}",
                status_code=response.status_code,
                code="http_error",
            )
        try:
            body = response.json()
        except Exception as exc:
            raise EmbeddingError("Embedding 响应不是合法 JSON", code="invalid_json") from exc

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected_count:
            got = len(data) if isinstance(data, list) else type(data).__name__
            raise EmbeddingError(
                f"Embedding 返回条数不匹配: 期望 {expected_count}, 实际 {got}",
                code="count_mismatch",
            )
        ordered = sorted(enumerate(data), key=lambda pair: pair[1].get("index", pair[0]) if isinstance(pair[1], dict) else pair[0])
        vectors = []
        expected_dim = None
        for _, item in ordered:
            if not isinstance(item, dict) or "embedding" not in item:
                raise EmbeddingError("Embedding 响应条目缺少 embedding 字段", code="invalid_response")
            vec = _decode_embedding_item(item["embedding"])
            if expected_dim is None:
                expected_dim = len(vec)
            elif len(vec) != expected_dim:
                raise EmbeddingError("同批次 Embedding 向量维度不一致", code="dim_mismatch")
            vectors.append(vec)
        self._record_success(body.get("usage") if isinstance(body, dict) else None)
        return vectors

    def _client_kwargs(self):
        kw = {"timeout": self.timeout, "trust_env": self.trust_env}
        if self.proxy:
            kw["proxy"] = self.proxy
        return kw

    def embed(self, texts, *, use_cache=True):
        """Synchronously embed a list of texts into L2-normalized `array('f')` vectors."""
        cleaned = self._sanitize_inputs(texts)
        if not cleaned:
            return []
        results = [None] * len(cleaned)
        missing_indices = []
        for idx, text in enumerate(cleaned):
            cached = self.get_cached(text) if use_cache else None
            if cached is not None:
                results[idx] = cached
            else:
                missing_indices.append(idx)
        if not missing_indices:
            return results

        self._check_circuit()
        missing_texts = [cleaned[i] for i in missing_indices]
        fetched = []
        try:
            with httpx.Client(**self._client_kwargs()) as client:
                for start in range(0, len(missing_texts), self.batch_size):
                    batch = missing_texts[start:start + self.batch_size]
                    url, headers, payload = self._build_request(batch)
                    resp = client.post(url, headers=headers, json=payload)
                    fetched.extend(self._parse_response(resp, len(batch)))
        except EmbeddingError:
            self._record_failure()
            raise
        except Exception as exc:
            self._record_failure()
            raise EmbeddingError(f"Embedding 请求异常: {type(exc).__name__}: {str(exc)[:240]}", code="network_error") from exc

        for idx, vec in zip(missing_indices, fetched):
            if use_cache:
                self._put_cached(cleaned[idx], vec)
            results[idx] = vec
        return results

    async def aembed(self, texts, *, use_cache=True):
        """Asynchronously embed a list of texts into L2-normalized `array('f')` vectors."""
        cleaned = self._sanitize_inputs(texts)
        if not cleaned:
            return []
        results = [None] * len(cleaned)
        missing_indices = []
        for idx, text in enumerate(cleaned):
            cached = self.get_cached(text) if use_cache else None
            if cached is not None:
                results[idx] = cached
            else:
                missing_indices.append(idx)
        if not missing_indices:
            return results

        self._check_circuit()
        missing_texts = [cleaned[i] for i in missing_indices]
        fetched = []
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                for start in range(0, len(missing_texts), self.batch_size):
                    batch = missing_texts[start:start + self.batch_size]
                    url, headers, payload = self._build_request(batch)
                    resp = await client.post(url, headers=headers, json=payload)
                    fetched.extend(self._parse_response(resp, len(batch)))
        except EmbeddingError:
            self._record_failure()
            raise
        except Exception as exc:
            self._record_failure()
            raise EmbeddingError(f"Embedding 请求异常: {type(exc).__name__}: {str(exc)[:240]}", code="network_error") from exc

        for idx, vec in zip(missing_indices, fetched):
            if use_cache:
                self._put_cached(cleaned[idx], vec)
            results[idx] = vec
        return results

    def embed_one(self, text, *, use_cache=True):
        return self.embed([text], use_cache=use_cache)[0]

    async def aembed_one(self, text, *, use_cache=True):
        return (await self.aembed([text], use_cache=use_cache))[0]

    def probe(self, sample_texts=("水彩速写", "淡彩风景", "手冲咖啡")):
        """Connectivity and semantic sanity check for CLI/manual verification."""
        t0 = time.time()
        vecs = self.embed(list(sample_texts), use_cache=False)
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        pairs = []
        for i in range(len(sample_texts)):
            for j in range(i + 1, len(sample_texts)):
                pairs.append({
                    "pair": [sample_texts[i], sample_texts[j]],
                    "cosine": round(cosine_similarity(vecs[i], vecs[j]), 4),
                })
        return {
            "ok": True,
            "endpoint": resolve_embeddings_url(self.base_url),
            "model": self.model,
            "dimension": len(vecs[0]) if vecs else 0,
            "elapsed_ms": elapsed_ms,
            "similarities": pairs,
        }

    async def aprobe(self, sample_texts=("水彩速写", "淡彩风景", "手冲咖啡")):
        t0 = time.time()
        vecs = await self.aembed(list(sample_texts), use_cache=False)
        elapsed_ms = round((time.time() - t0) * 1000, 1)
        pairs = []
        for i in range(len(sample_texts)):
            for j in range(i + 1, len(sample_texts)):
                pairs.append({
                    "pair": [sample_texts[i], sample_texts[j]],
                    "cosine": round(cosine_similarity(vecs[i], vecs[j]), 4),
                })
        return {
            "ok": True,
            "endpoint": resolve_embeddings_url(self.base_url),
            "model": self.model,
            "dimension": len(vecs[0]) if vecs else 0,
            "elapsed_ms": elapsed_ms,
            "similarities": pairs,
        }


class EmbeddingStore:
    """SQLite-backed semantic vector index & topic graph helper.

    Schema is created lazily only when `ensure_schema()` or an index/search method is invoked,
    leaving `state.sqlite` untouched when `embedding.enabled` is false.
    """

    def __init__(self, store, client):
        self.s = store
        self.client = client
        self._schema_ready = False
        self._anchor_mem = {}

    @property
    def active_model_tag(self):
        return self.client.model_tag

    def ensure_schema(self):
        if self._schema_ready:
            return
        self.s.db.executescript("""
        CREATE TABLE IF NOT EXISTS record_embeddings(
            record_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            text_hash TEXT NOT NULL,
            vector BLOB NOT NULL,
            updated REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_record_embeddings_model ON record_embeddings(model);
        """)
        self._schema_ready = True

    def status(self):
        if not self.client.is_configured():
            return {"enabled": False, "configured": False}
        self.ensure_schema()
        cnt = self.s.db.execute(
            "SELECT count(*) FROM record_embeddings WHERE model=?",
            (self.active_model_tag,),
        ).fetchone()[0]
        return {
            "enabled": True,
            "configured": True,
            "endpoint": resolve_embeddings_url(self.client.base_url),
            "model": self.client.model,
            "indexed_records": cnt,
            "requests": self.client.total_requests,
            "circuit_open": self.client._backoff_until > time.time(),
        }

    def get_vector(self, record_id):
        if not self.client.is_configured():
            return None
        self.ensure_schema()
        row = self.s.db.execute(
            "SELECT model, dim, vector FROM record_embeddings WHERE record_id=?",
            (record_id,),
        ).fetchone()
        if not row or row["model"] != self.active_model_tag:
            return None
        vec = unpack_vector(row["vector"])
        return vec if len(vec) == row["dim"] else None

    def get_vectors(self, record_ids):
        """Batch fetch stored embeddings from SQLite (0 HTTP calls, sub-millisecond)."""
        if not self.client.is_configured():
            return {}
        ids = [rid for rid in dict.fromkeys(record_ids) if rid]
        if not ids:
            return {}
        self.ensure_schema()
        marks = ",".join("?" for _ in ids)
        rows = self.s.db.execute(
            f"SELECT record_id, dim, vector FROM record_embeddings WHERE model=? AND record_id IN ({marks})",
            (self.active_model_tag, *ids),
        ).fetchall()
        out = {}
        for r in rows:
            vec = unpack_vector(r["vector"])
            if len(vec) == r["dim"]:
                out[r["record_id"]] = vec
        return out

    def get_relational_anchor(self, *, allow_network=False):
        """Return the L2-normalized relational-topic anchor vector (cached in memory & SQLite meta)."""
        if not self.client.is_configured():
            return None
        tag = self.active_model_tag
        if tag in self._anchor_mem:
            return self._anchor_mem[tag]
        meta_key = "embedding_rel_anchor:" + tag
        stored = self.s.get(meta_key)
        if isinstance(stored, str) and stored:
            try:
                vec = unpack_vector(base64.b64decode(stored))
                self._anchor_mem[tag] = vec
                return vec
            except Exception:
                pass
        if not allow_network:
            return None
        vec = self.client.embed_one(RELATIONAL_ANCHOR_TEXT)
        blob, _ = pack_vector(vec, normalize=True)
        self.s.set(meta_key, base64.b64encode(blob).decode("ascii"))
        self._anchor_mem[tag] = vec
        return vec

    async def aensure_relational_anchor(self):
        if not self.client.is_configured():
            return None
        existing = self.get_relational_anchor(allow_network=False)
        if existing is not None:
            return existing
        tag = self.active_model_tag
        vec = await self.client.aembed_one(RELATIONAL_ANCHOR_TEXT)
        blob, _ = pack_vector(vec, normalize=True)
        with self.s.transaction():
            self.s.set("embedding_rel_anchor:" + tag, base64.b64encode(blob).decode("ascii"))
        self._anchor_mem[tag] = vec
        return vec

    def _needs_reindex(self, record_id, digest):
        row = self.s.db.execute(
            "SELECT model, text_hash FROM record_embeddings WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return (not row) or row["model"] != self.active_model_tag or row["text_hash"] != digest

    def _save_vector(self, record_id, digest, vec, now=None):
        blob, dim = pack_vector(vec, normalize=True)
        now = time.time() if now is None else float(now)
        self.s.db.execute(
            "INSERT OR REPLACE INTO record_embeddings(record_id, model, dim, text_hash, vector, updated) VALUES(?,?,?,?,?,?)",
            (record_id, self.active_model_tag, dim, digest, blob, now),
        )

    def index_from_cache(self, record_id, label, payload):
        """Persist vector if already computed in client LRU cache (0 network I/O, safe inside DB transactions)."""
        if not self.client.is_configured():
            return False
        text = canonical_record_text(label, payload)
        vec = self.client.get_cached(text)
        if vec is None:
            return False
        self.ensure_schema()
        digest = text_sha256(text)
        if self._needs_reindex(record_id, digest):
            self._save_vector(record_id, digest, vec)
            return True
        return False

    def index_record(self, record_id, label, payload, *, force=False):
        if not self.client.is_configured():
            return False
        self.ensure_schema()
        text = canonical_record_text(label, payload)
        digest = text_sha256(text)
        if not force and not self._needs_reindex(record_id, digest):
            return False
        vec = self.client.embed_one(text)
        with self.s.transaction():
            self._save_vector(record_id, digest, vec)
        return True

    async def aindex_record(self, record_id, label, payload, *, force=False):
        if not self.client.is_configured():
            return False
        self.ensure_schema()
        text = canonical_record_text(label, payload)
        digest = text_sha256(text)
        if not force and not self._needs_reindex(record_id, digest):
            return False
        vec = await self.client.aembed_one(text)
        with self.s.transaction():
            self._save_vector(record_id, digest, vec)
        return True

    def index_record_ids(self, record_ids):
        """Batch-embed and persist specific newly saved record IDs outside the main write transaction."""
        if not self.client.is_configured():
            return 0
        ids = [rid for rid in dict.fromkeys(record_ids) if rid]
        if not ids:
            return 0
        self.ensure_schema()
        marks = ",".join("?" for _ in ids)
        rows = self.s.db.execute(
            f"SELECT id, label, payload FROM records WHERE id IN ({marks})",
            ids,
        ).fetchall()
        pending = []
        for r in rows:
            payload = json.loads(r["payload"])
            text = canonical_record_text(r["label"], payload)
            digest = text_sha256(text)
            if self._needs_reindex(r["id"], digest):
                pending.append((r["id"], text, digest))
        if not pending:
            return 0
        vecs = self.client.embed([item[1] for item in pending])
        now = time.time()
        with self.s.transaction():
            for (rid, _, digest), vec in zip(pending, vecs):
                self._save_vector(rid, digest, vec, now=now)
        return len(pending)

    def _collect_backfill_candidates(self, *, limit=32, active_only=True, max_journals=120):
        # Prioritize structured working memories first (`interest`, `situation`, `belief`, etc.),
        # then recent `journal` entries; skip transient `episode` impressions unless small DB.
        structured = self.s.db.execute(
            "SELECT id, kind, label, payload FROM records WHERE kind NOT IN ('episode', 'journal') ORDER BY updated DESC"
        ).fetchall()
        journals = self.s.db.execute(
            "SELECT id, kind, label, payload FROM records WHERE kind='journal' ORDER BY updated DESC LIMIT ?",
            (max_journals,),
        ).fetchall()
        pending = []
        for r in list(structured) + list(journals):
            payload = json.loads(r["payload"])
            if active_only and not active({"payload": payload}):
                continue
            text = canonical_record_text(r["label"], payload)
            digest = text_sha256(text)
            if self._needs_reindex(r["id"], digest):
                pending.append((r["id"], text, digest))
                if len(pending) >= limit:
                    break
        return pending

    def backfill_records(self, *, limit=32, active_only=True):
        """Incrementally embed unindexed or stale records (respects model/dimension switches)."""
        if not self.client.is_configured():
            return 0
        self.ensure_schema()
        self.get_relational_anchor(allow_network=True)
        pending = self._collect_backfill_candidates(limit=limit, active_only=active_only)
        if not pending:
            return 0
        vecs = self.client.embed([item[1] for item in pending])
        now = time.time()
        with self.s.transaction():
            for (rid, _, digest), vec in zip(pending, vecs):
                self._save_vector(rid, digest, vec, now=now)
        return len(pending)

    async def abackfill_records(self, *, limit=32, active_only=True):
        if not self.client.is_configured():
            return 0
        self.ensure_schema()
        await self.aensure_relational_anchor()
        pending = self._collect_backfill_candidates(limit=limit, active_only=active_only)
        if not pending:
            return 0
        vecs = await self.client.aembed([item[1] for item in pending])
        now = time.time()
        with self.s.transaction():
            for (rid, _, digest), vec in zip(pending, vecs):
                self._save_vector(rid, digest, vec, now=now)
        return len(pending)

    def _score_against_query_vec(self, q_vec, *, kinds=None, active_only=True, limit=10, min_score=0.44):
        self.ensure_schema()
        kind_clause = ""
        params = [self.active_model_tag]
        if kinds:
            marks = ",".join("?" for _ in kinds)
            kind_clause = f" AND r.kind IN ({marks})"
            params.extend(kinds)
        rows = self.s.db.execute(
            f"""SELECT r.*, e.dim, e.vector
                FROM record_embeddings e
                JOIN records r ON r.id = e.record_id
                WHERE e.model = ?{kind_clause}""",
            params,
        ).fetchall()
        scored = []
        for r in rows:
            vec = unpack_vector(r["vector"])
            if len(vec) != len(q_vec):
                continue
            payload = json.loads(r["payload"])
            item = {
                "id": r["id"],
                "kind": r["kind"],
                "label": r["label"],
                "payload": payload,
                "refs": json.loads(r["refs"]),
                "created": r["created"],
                "updated": r["updated"],
            }
            if active_only and not active(item):
                continue
            sim = cosine_similarity(q_vec, vec)
            if sim >= min_score:
                item["semantic_score"] = round(sim, 4)
                item["recalled_by"] = "embedding"
                scored.append(item)
        scored.sort(key=lambda x: (x["semantic_score"], x["updated"]), reverse=True)
        return scored[:limit]

    def search(self, query, *, kinds=None, active_only=True, limit=10, min_score=0.44):
        """Semantic top-k search for hybrid `memory.recall()` and parked-topic wakeup."""
        if not self.client.is_configured() or not str(query or "").strip():
            return []
        q_vec = self.client.embed_one(query)
        return self._score_against_query_vec(q_vec, kinds=kinds, active_only=active_only, limit=limit, min_score=min_score)

    async def asearch(self, query, *, kinds=None, active_only=True, limit=10, min_score=0.44):
        if not self.client.is_configured() or not str(query or "").strip():
            return []
        q_vec = await self.client.aembed_one(query)
        return self._score_against_query_vec(q_vec, kinds=kinds, active_only=active_only, limit=limit, min_score=min_score)

    def find_near_duplicate(self, label, payload=None, *, kind="interest", threshold=0.78):
        """Consolidate near-synonymous interests in `Life.prepare()` instead of fragmenting."""
        if not self.client.is_configured():
            return None
        text = canonical_record_text(label, payload or {})
        hits = self.search(text, kinds=[kind], active_only=False, limit=1, min_score=threshold)
        return hits[0] if hits else None

    def topic_adjacency(self, record_ids, *, threshold=0.50):
        """Build symmetric semantic adjacency weights W_ij = cos(e_i, e_j) for Graph Laplacian topic diffusion."""
        vecs = self.get_vectors(record_ids)
        if len(vecs) < 2:
            return {}
        adj = {rid: {} for rid in vecs}
        present = list(vecs.keys())
        for i in range(len(present)):
            u = present[i]
            for j in range(i + 1, len(present)):
                v = present[j]
                if len(vecs[u]) != len(vecs[v]):
                    continue
                sim = cosine_similarity(vecs[u], vecs[v])
                if sim >= threshold:
                    w = round(sim, 4)
                    adj[u][v] = w
                    adj[v][u] = w
        return adj


def main(argv=None):
    parser = argparse.ArgumentParser(description="测试 Hermes Soul OpenAI 兼容 Embedding 接口")
    parser.add_argument("--config", help="配置文件路径（默认 ~/.config/hermes-soul/config.yaml）")
    parser.add_argument("--base-url", help="临时覆盖 embedding.base_url")
    parser.add_argument("--model", help="临时覆盖 embedding.model")
    parser.add_argument("--api-key", help="临时覆盖 embedding.api_key")
    parser.add_argument("--dimensions", type=int, help="临时覆盖 embedding.dimensions")
    parser.add_argument("--probe", nargs="*", default=["水彩速写", "淡彩风景", "手冲咖啡"], help="用于测试向量化与余弦相似度的文本")
    args = parser.parse_args(argv)

    from .config import load_config
    cfg = load_config(args.config) if (args.config or (Path.home() / ".config/hermes-soul/config.yaml").exists()) else {"embedding": {}}
    ecfg = dict(cfg.get("embedding") or {})
    ecfg["enabled"] = True
    if args.base_url:
        ecfg["base_url"] = args.base_url
    if args.model:
        ecfg["model"] = args.model
    if args.api_key is not None:
        ecfg["api_key"] = args.api_key
    if args.dimensions:
        ecfg["dimensions"] = args.dimensions

    client = OpenAIEmbeddingClient(ecfg)
    result = client.probe(args.probe)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
