"""Local topic recall: Chinese bigrams + words, with provenance and bounded prompts."""
import json
import re
import time


def terms(text):
    result = set()
    for word in re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]+", text.lower()[:12000]):
        if re.fullmatch(r"[\u4e00-\u9fff]+", word):
            result.update(word[i:i+2] for i in range(len(word)-1))
        else:
            result.add(word)
    return sorted(result)[:400]


def index_record(store, rid, label, payload):
    text = label + " " + str(payload.get("content", payload.get("text", "")))
    store.db.execute("DELETE FROM record_terms WHERE record_id=?", (rid,))
    store.db.executemany("INSERT OR IGNORE INTO record_terms VALUES(?,?)", [(t, rid) for t in terms(text)])


def rebuild_missing(store):
    # One-time migration indexes existing text without changing any memory meaning.
    if store.get("recall_index_version") != 1:
        with store.transaction():
            for r in store.db.execute("SELECT id,label,payload FROM records").fetchall():
                index_record(store, r["id"], r["label"], json.loads(r["payload"]))
            store.set("recall_index_version", 1)


def active(record, now=None):
    p = record["payload"]
    return p.get("state") not in {"resolved", "archived", "completed", "expired", "superseded"} and (
        not p.get("expires_at") or p["expires_at"] > (now or time.time()))


def recall(store, query="", limit=20, embeddings=None):
    selected = {}
    sem_map = {}
    if embeddings and embeddings.client.is_configured() and str(query or "").strip():
        try:
            for item in embeddings.search(query, limit=min(limit, 10), min_score=0.44):
                sem_map[item["id"]] = item
        except Exception:
            sem_map = {}

    lex_items = []
    q = terms(query)
    if q:
        marks = ','.join('?' for _ in q)
        rows = store.db.execute(f"""SELECT r.*,COUNT(*) score FROM record_terms t JOIN records r ON r.id=t.record_id
          WHERE t.term IN ({marks}) GROUP BY r.id ORDER BY score DESC,r.updated DESC LIMIT ?""", (*q, limit)).fetchall()
        for r in rows:
            item = dict(r); item["payload"] = json.loads(item["payload"]); item["refs"] = json.loads(item["refs"])
            if active(item):
                if item["id"] in sem_map:
                    item["recalled_by"] = "hybrid"
                    item["semantic_score"] = sem_map[item["id"]]["semantic_score"]
                else:
                    item["recalled_by"] = "topic"
                lex_items.append(item)

    # Hybrid hits (both lexical + semantic) first, then top semantic hits, then lexical hits
    for item in lex_items:
        if item["recalled_by"] == "hybrid":
            selected[item["id"]] = item
    for rid, item in sem_map.items():
        if len(selected) < limit:
            selected.setdefault(rid, item)
    for item in lex_items:
        if len(selected) < limit:
            selected.setdefault(item["id"], item)

    # Always expose unresolved commitments/boundaries, even when the topic is unrelated.
    important = store.db.execute("SELECT * FROM records WHERE kind IN ('situation','boundary','agreement','task') ORDER BY updated DESC LIMIT 80").fetchall()
    for r in important:
        item = dict(r); item["payload"] = json.loads(item["payload"]); item["refs"] = json.loads(item["refs"])
        if active(item) and len(selected) < limit + 8:
            selected.setdefault(item["id"], {**item, "recalled_by": "open_state"})
    for item in store.rows("records", 40):
        if active(item) and len(selected) < limit + 8:
            selected.setdefault(item["id"], {**item, "recalled_by": "recent"})
    return list(selected.values())
