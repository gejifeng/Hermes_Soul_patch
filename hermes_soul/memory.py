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


def recall(store, query="", limit=20):
    selected = {}
    q = terms(query)
    if q:
        marks = ','.join('?' for _ in q)
        rows = store.db.execute(f"""SELECT r.*,COUNT(*) score FROM record_terms t JOIN records r ON r.id=t.record_id
          WHERE t.term IN ({marks}) GROUP BY r.id ORDER BY score DESC,r.updated DESC LIMIT ?""", (*q, limit)).fetchall()
        for r in rows:
            item = dict(r); item["payload"] = json.loads(item["payload"]); item["refs"] = json.loads(item["refs"])
            if active(item):
                item["recalled_by"] = "topic"; selected[item["id"]] = item
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
