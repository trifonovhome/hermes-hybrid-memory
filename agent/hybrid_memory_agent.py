#!/usr/bin/env python3
"""
Per-Agent Memory API — each container serves exactly one agent.
Agent identity is set via AGENT_ID env var (container-level), not request body.

Endpoints:
  GET  /health, /status
  POST /memory/search      — search own + shared pool
  POST /memory/extract     — LLM extraction → own storage
  POST /memory/share       — send fact to another agent's container
  POST /memory/receive     — receive fact from another agent
  POST /memory/broadcast   — send fact to all peers
  POST /memory/sessions/*  — session search/import
"""

import json, sys, os, sqlite3, urllib.request, asyncio, datetime, uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------- Agent identity ----------
AGENT_ID = os.environ.get("AGENT_ID", "agent-alpha")
SHARED_URL = os.environ.get("SHARED_URL", "http://127.0.0.1:8710")
PEERS_RAW = os.environ.get("PEERS", "")

# Parse peers: name:host:port,name:host:port
PEERS = {}
for p in PEERS_RAW.split(","):
    p = p.strip()
    if ":" in p:
        parts = p.split(":")
        if len(parts) >= 3:
            PEERS[parts[0]] = f"http://{parts[1]}:{parts[2]}"
        elif len(parts) == 2:
            PEERS[parts[0]] = f"http://{parts[0]}:{parts[1]}"

# ---------- Config ----------
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
LITELLM_KEY = os.environ.get("LITELLM_API_KEY", os.environ.get("LITELLM_KEY", ""))
EMBED_MODEL = os.environ.get("EMBED_MODEL", "bge-m3")
FTS5_DB = os.environ.get("FTS5_DB", "/data/fts5/trial_db.sqlite")
CHROMA_DIR = os.environ.get("CHROMA_DIR", "/data/chroma")
MEMORYGRAPH_DIR = os.environ.get("MEMORYGRAPH_DIR", "/data/memorygraph")
LISTEN_HOST = os.environ.get("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8711"))
CHROMA_COLLECTION = f"memory_{AGENT_ID}"  # own collection
CHROMA_SESSIONS = f"sessions_{AGENT_ID}"

EXTRACTION_MODEL = os.environ.get("EXTRACTION_MODEL", "deepseek-v4-pro")
EXTRACTION_SYSTEM_PROMPT = """Ты извлекаешь ключевые факты из текста диалога.
Отвечай СТРОГО в JSON формате без markdown-блоков, без пояснений:
{"facts": ["факт 1", "факт 2"]}

Правила:
- Каждый факт — законченное предложение на русском
- Извлекай ТОЛЬКО новую информацию, не очевидные вещи
- Факты должны быть полезны для будущего поиска (ключевые слова)
- Максимум 5 фактов
- Если в тексте нет значимых фактов — верни {"facts": []}"""

# ---------- FTS5 ----------
def _init_fts5():
    try:
        os.makedirs(os.path.dirname(FTS5_DB), exist_ok=True)
        conn = sqlite3.connect(FTS5_DB)
        conn.execute("""CREATE TABLE IF NOT EXISTS facts (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, source TEXT DEFAULT 'extraction',
            created_at TEXT, updated_at TEXT)""")
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
            USING fts5(content, fact_id UNINDEXED)""")
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[FTS5] Init error: {e}", file=sys.stderr)

def _ensure_fts5():
    """Ensure FTS5 tables exist — call before any FTS5 operation."""
    try:
        conn = sqlite3.connect(FTS5_DB)
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='facts_fts'")
        if not cur.fetchone():
            conn.close(); _init_fts5()
        else:
            conn.close()
    except Exception:
        pass

_init_fts5()

def fts5_search(query: str, limit: int = 10) -> list:
    _ensure_fts5()
    try:
        conn = sqlite3.connect(FTS5_DB); conn.row_factory = sqlite3.Row
        cur = conn.execute("""SELECT fts.content, fts.fact_id, fts.rank as bm25_score,
                              f.created_at
                              FROM facts_fts fts
                              JOIN facts f ON fts.fact_id = f.id
                              WHERE facts_fts MATCH ? ORDER BY fts.rank LIMIT ?""",
                           (query, limit))
        rows = cur.fetchall()
        if not rows and ' ' in query:
            cur = conn.execute("""SELECT fts.content, fts.fact_id, fts.rank as bm25_score,
                                  f.created_at
                                  FROM facts_fts fts
                                  JOIN facts f ON fts.fact_id = f.id
                                  WHERE facts_fts MATCH ? ORDER BY fts.rank LIMIT ?""",
                               (' OR '.join(query.split()), limit))
            rows = cur.fetchall()
        results = []
        for r in rows:
            bm25 = round(min(1.0, max(0.0, abs(r["bm25_score"] or 0) / 5.0)), 4)
            created = r["created_at"] or ""
            if created:
                bm25 *= (0.7 + 0.3 * recency_boost(created))
            results.append({"content": r["content"], "fact_id": r["fact_id"],
                           "bm25": bm25, "created_at": created,
                           "backend": "fts5"})
        conn.close(); return results
    except Exception as e:
        print(f"[FTS5] Search error: {e}", file=sys.stderr); return []

def fts5_store(content: str) -> bool:
    _ensure_fts5()
    try:
        now = datetime.datetime.utcnow().isoformat() + "Z"
        conn = sqlite3.connect(FTS5_DB)
        fid = str(uuid.uuid4())
        conn.execute("INSERT INTO facts (id, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                     (fid, content, now, now))
        conn.execute("INSERT INTO facts_fts (content, fact_id) VALUES (?, ?)", (content, fid))
        conn.commit(); conn.close(); return True
    except Exception as e:
        print(f"[FTS5] Store error: {e}", file=sys.stderr); return False

# ---------- LiteLLM Embedding ----------
def embed_litellm(texts: list) -> list:
    try:
        payload = json.dumps({"model": EMBED_MODEL, "input": texts}).encode()
        req = urllib.request.Request(f"{LITELLM_URL}/v1/embeddings", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LITELLM_KEY}"})
        resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
        return [d["embedding"] for d in resp["data"]]
    except Exception as e:
        print(f"[Embed] Error: {e}", file=sys.stderr); return []

# ---------- Chroma ----------
_chroma_client = None
def _get_chroma():
    global _chroma_client
    if _chroma_client is None:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    return _chroma_client

def chroma_search(query: str, limit: int = 10) -> list:
    emb = embed_litellm([query])
    if not emb: return []
    try:
        client = _get_chroma()
        hits = []
        collections_to_search = [CHROMA_COLLECTION]
        if AGENT_ID == "agent-alpha":
            collections_to_search.insert(0, "hermes_memory_bge_m3")  # legacy
        for cname in collections_to_search:
            try:
                coll = client.get_collection(name=cname)
                results = coll.query(query_embeddings=emb, n_results=limit)
                for i, doc in enumerate(results.get("documents", [[]])[0]):
                    dist = results.get("distances", [[]])[0][i]
                    score = round(1.0 - dist, 4)
                    meta = results.get("metadatas", [[]])[0][i] if results.get("metadatas") else {}
                    created = meta.get("created_at", "") if isinstance(meta, dict) else ""
                    if created:
                        score *= (0.7 + 0.3 * recency_boost(created))
                    hits.append({"content": doc, "score": score,
                                 "created_at": created, "backend": "chroma"})
            except Exception: pass
        return sorted(hits, key=lambda h: h["score"], reverse=True)[:limit]
    except Exception as e:
        print(f"[Chroma] Search error: {e}", file=sys.stderr); return []

def chroma_store(content: str) -> bool:
    emb = embed_litellm([content])
    if not emb: return False
    try:
        client = _get_chroma()
        coll = client.get_or_create_collection(name=CHROMA_COLLECTION, metadata={"hnsw:space": "cosine"})
        now = datetime.datetime.utcnow().isoformat() + "Z"
        coll.add(ids=[str(uuid.uuid4())], documents=[content], embeddings=emb,
                 metadatas=[{"created_at": now, "source": "extraction"}])
        return True
    except Exception as e:
        print(f"[Chroma] Store error: {e}", file=sys.stderr); return False

# ---------- Shared Pool (remote) ----------
def shared_pool_search(query: str, limit: int = 10) -> list:
    """Query the shared memory container."""
    if AGENT_ID == "shared": return []  # shared doesn't query itself
    try:
        payload = json.dumps({"query": query, "limit": limit}).encode()
        req = urllib.request.Request(f"{SHARED_URL}/memory/search", data=payload,
            headers={"Content-Type":"application/json"}, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        results = resp.get("results", [])
        for r in results: r["backend"] = "shared"
        return results
    except Exception as e:
        print(f"[Shared] Search error: {e}", file=sys.stderr); return []

# ---------- MemoryGraph ----------
_mg_db: dict = {}
_mg_backend: dict = {}
_mg_initialized: dict = {}

def recency_boost(created_str: str) -> float:
    """Boost score by recency: 1.0 today → 0.05 at 90+ days."""
    try:
        if not created_str:
            return 0.05
        created_str = created_str.replace("Z", "+00:00")
        created = datetime.datetime.fromisoformat(created_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        days = (now - created).days
        if days < 0: return 1.0
        if days == 0: return 1.0
        if days <= 7: return 0.6 + (1.0 - 0.6) * (7 - days) / 7
        if days <= 30: return 0.3 + (0.6 - 0.3) * (30 - days) / 23
        if days <= 90: return 0.05 + (0.3 - 0.05) * (90 - days) / 60
        return 0.05
    except Exception:
        return 0.3

def _init_mg():
    agent_id = AGENT_ID
    if _mg_initialized.get(agent_id): return True
    try:
        from memorygraph.models import Memory, MemoryType, SearchQuery
        from memorygraph.sqlite_database import SQLiteMemoryDatabase
        from memorygraph.backends.sqlite_fallback import SQLiteFallbackBackend
        os.makedirs(MEMORYGRAPH_DIR, exist_ok=True)
        db_path = f"{MEMORYGRAPH_DIR}/memorygraph_{agent_id}.db"
        backend = SQLiteFallbackBackend(db_path=db_path)

        async def _init():
            await backend.connect()
            await backend.initialize_schema()
            db = SQLiteMemoryDatabase(backend)
            await db.initialize_schema()
            return db, backend

        loop = asyncio.new_event_loop()
        db, backend = loop.run_until_complete(_init())
        loop.close()
        _mg_db[agent_id] = db
        _mg_backend[agent_id] = backend
        _mg_initialized[agent_id] = True
        print(f"[MemoryGraph] Initialized: {db_path}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[MemoryGraph] Init error: {e}", file=sys.stderr)
        return False

def memorygraph_search(query: str, limit: int = 10) -> list:
    if not _init_mg(): return []
    # Clamp to prevent integer overflow in SearchQuery (max 1000)
    limit = min(max(1, limit), 100)
    try:
        from memorygraph.models import SearchQuery, MemoryType
        agent_id = AGENT_ID
        memory_types = [
            MemoryType.GENERAL, MemoryType.TASK, MemoryType.WORKFLOW,
            MemoryType.COMMAND, MemoryType.PROBLEM, MemoryType.PROJECT
        ]

        async def _search(q: str, lim: int):
            sq = SearchQuery(query=q, memory_types=memory_types,
                             limit=lim, match_mode="any", search_tolerance="fuzzy")
            return await _mg_db[agent_id].search_memories(sq)

        loop = asyncio.new_event_loop()
        # Try full query first; if empty, try each word
        search_limit = min(limit * 2, 100)
        results = loop.run_until_complete(_search(query, search_limit))
        if not results:
            words = [w for w in query.split() if len(w) > 1]
            seen_ids = set()
            word_results = []
            for w in words:
                for r in loop.run_until_complete(_search(w, limit)):
                    rid = getattr(r, "id", str(r))
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        word_results.append(r)
            results = word_results
        loop.close()

        out = []
        for r in results:
            created = getattr(r, "created_at", None)
            created_str = created.isoformat() + "Z" if created else ""
            score = getattr(r, "score", 0.3) or 0.3
            if created_str:
                score *= (0.7 + 0.3 * recency_boost(created_str))
            out.append({
                "content": r.content[:300] if r.content else "",
                "title": r.title or "", "id": r.id,
                "tags": r.tags[:5] if r.tags else [],
                "score": round(score, 4),
                "created_at": created_str,
                "backend": "memorygraph"
            })
        return sorted(out, key=lambda h: h["score"], reverse=True)[:limit]
    except Exception as e:
        print(f"[MemoryGraph] Search error: {e}", file=sys.stderr); return []

def memorygraph_store(content: str, source: str = "") -> bool:
    if not _init_mg(): return False
    try:
        from memorygraph.models import Memory, MemoryType
        agent_id = AGENT_ID
        title = content.split(".")[0].strip()[:120] if "." in content else content[:120]
        now = datetime.datetime.utcnow()
        memory = Memory(
            title=title,
            content=f"[{source}] {content}" if source else content,
            type=MemoryType.GENERAL,
            tags=[source] if source else [],
            created_at=now,
            updated_at=now,
        )
        async def _store():
            return await _mg_db[agent_id].store_memory(memory)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(_store())
        loop.close()
        return True
    except Exception as e:
        print(f"[MemoryGraph] Store error: {e}", file=sys.stderr); return False

# ---------- Unified Search ----------
def _norm_key(text: str) -> str:
    return ''.join(c.lower() for c in str(text) if c.isalnum())[:80]

def unified_search(query: str, limit: int = 5) -> dict:
    limit = min(max(1, limit), 100)  # safety clamp
    fts5_hits = fts5_search(query, limit * 2)
    chroma_hits = chroma_search(query, limit * 2)
    shared_hits = shared_pool_search(query, limit * 2)
    mg_hits = memorygraph_search(query, limit * 2)

    seen = set(); merged = []
    for h in shared_hits:
        key = _norm_key(h["content"])
        if key not in seen: seen.add(key); h["fusion"] = 0.45 * h.get("score", 0.2); merged.append(h)
    for h in chroma_hits:
        key = _norm_key(h["content"])
        if key not in seen: seen.add(key); h["fusion"] = 0.50 * h["score"]; merged.append(h)
    for h in fts5_hits:
        key = _norm_key(h["content"]); bm25 = h.get("bm25", 0.1)
        if key in seen:
            for m in merged:
                if _norm_key(m["content"]) == key:
                    m["fusion"] += 0.20 * min(1.0, bm25 + 0.2); m["keyword_match"] = True; break
        else:
            seen.add(key); h["fusion"] = 0.20 * min(1.0, bm25 + 0.2); merged.append(h)
    for h in mg_hits:
        key = _norm_key(h["content"])
        tag_bonus = min(0.05, len(h.get("tags", [])) * 0.01)
        mg_score = 0.15 + tag_bonus
        if key in seen:
            for m in merged:
                if _norm_key(m["content"]) == key:
                    m["fusion"] += mg_score; m["graph_match"] = True; break
        else:
            seen.add(key); h["fusion"] = mg_score; merged.append(h)
    merged.sort(key=lambda x: x["fusion"], reverse=True)
    return {"query": query, "results": merged[:limit],
            "backends": {"fts5": len(fts5_hits), "chroma": len(chroma_hits),
                         "shared": len(shared_hits), "memorygraph": len(mg_hits)}}

# ---------- LLM Extraction ----------
def extract_facts_via_llm(text: str) -> list:
    try:
        payload = json.dumps({"model": EXTRACTION_MODEL,
            "messages": [{"role":"system","content":EXTRACTION_SYSTEM_PROMPT},
                         {"role":"user","content":text[:8000]}],
            "max_tokens": 500, "temperature": 0.1}).encode()
        req = urllib.request.Request(f"{LITELLM_URL}/v1/chat/completions", data=payload,
            headers={"Content-Type":"application/json","Authorization":f"Bearer {LITELLM_KEY}"})
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        content = resp["choices"][0]["message"]["content"].strip()
        if content.startswith("```"): content = content.split("```")[1]
        if content.startswith("json"): content = content[4:]
        return json.loads(content.strip()).get("facts", [])
    except Exception as e:
        print(f"[Extract] LLM error: {e}", file=sys.stderr); return []

# ---------- Session Search ----------
def _init_sessions_fts5():
    try:
        conn = sqlite3.connect(FTS5_DB)
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY, title TEXT, preview TEXT, content TEXT,
            message_count INTEGER DEFAULT 0, created_at TEXT, indexed_at TEXT)""")
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
            USING fts5(id, title, preview, content, content='sessions', content_rowid='rowid')""")
        conn.execute("INSERT INTO sessions_fts(sessions_fts) VALUES('rebuild')")
        conn.commit(); conn.close()
    except Exception as e: print(f"[Sess] Init: {e}", file=sys.stderr)

def session_search(query: str, limit: int = 5) -> dict:
    _init_sessions_fts5()
    try:
        conn = sqlite3.connect(FTS5_DB); conn.row_factory = sqlite3.Row
        terms = [t for t in query.replace("'","''").split() if len(t) > 2]
        fts_q = " OR ".join(f"{t}*" for t in terms) if terms else query.replace("'","''")
        rows = conn.execute("""SELECT s.id, s.title, s.preview, s.message_count, sessions_fts.rank as bm25
            FROM sessions_fts JOIN sessions s ON sessions_fts.rowid = s.rowid
            WHERE sessions_fts MATCH ? ORDER BY sessions_fts.rank LIMIT ?""", [fts_q, limit]).fetchall()
        results = [{"session_id": r["id"], "title": r["title"] or "", "preview": (r["preview"] or "")[:200],
                    "message_count": r["message_count"] or 0, "backend": "fts5_sessions"} for r in rows]
        conn.close()
        emb = embed_litellm([query])
        if emb:
            try:
                client = _get_chroma()
                coll = client.get_or_create_collection(name=CHROMA_SESSIONS, metadata={"hnsw:space": "cosine"})
                cr = coll.query(query_embeddings=emb, n_results=limit)
                for i, doc in enumerate(cr.get("documents", [[]])[0]):
                    dist = cr.get("distances", [[]])[0][i]; meta = cr.get("metadatas", [[]])[0][i] if cr.get("metadatas") else {}
                    results.append({"content": doc[:300], "score": round(1.0-dist,4),
                                    "session_id": meta.get("session_id",""), "backend": "chroma_sessions"})
            except Exception: pass
        return {"query": query, "results": results[:limit],
                "backends": {"fts5": len([r for r in results if r["backend"]=="fts5_sessions"]),
                            "chroma": len([r for r in results if r["backend"]=="chroma_sessions"])}}
    except Exception as e:
        print(f"[Sess] Search error: {e}", file=sys.stderr); return {"query": query, "results": [], "backends": {"fts5":0,"chroma":0}}

# ---------- HTTP Handler ----------
class MemoryAPIHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok", "agent": AGENT_ID, "port": LISTEN_PORT})
        elif self.path == "/status":
            try:
                client = _get_chroma()
                chroma_n = 0
                for cname in [CHROMA_COLLECTION] + (["hermes_memory_bge_m3"] if AGENT_ID == "agent-alpha" else []):
                    try: chroma_n += client.get_collection(name=cname).count()
                    except Exception: pass
            except Exception: chroma_n = 0
            try:
                conn = sqlite3.connect(FTS5_DB)
                fts5_n = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]; conn.close()
            except Exception: fts5_n = 0
            try:
                if _init_mg():
                    async def _mg_count():
                        stats = await _mg_db[AGENT_ID].get_memory_statistics()
                        return stats.get('total_memories', {}).get('count', 0)
                    loop = asyncio.new_event_loop()
                    mg_n = loop.run_until_complete(_mg_count())
                    loop.close()
                else: mg_n = 0
            except Exception: mg_n = 0
            self._send_json({"agent": AGENT_ID, "fts5": fts5_n, "chroma": chroma_n, "memorygraph": mg_n})
        else:
            self._send_json({"error":"not found"}, 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length > 0 else {}
        except json.JSONDecodeError:
            self._send_json({"error":"invalid JSON"}, 400); return

        # ---- Search ----
        if self.path == "/memory/search":
            query = body.get("query", ""); limit = body.get("limit", 5)
            if not query: self._send_json({"error":"query required"}, 400); return
            self._send_json(unified_search(query, limit))

        # ---- Extract ----
        elif self.path == "/memory/extract":
            text = body.get("text", ""); store = body.get("store", True)
            if not text: self._send_json({"error":"text required"}, 400); return
            facts = extract_facts_via_llm(text)
            s_fts5 = s_chroma = s_mg = 0
            if store and facts:
                for f in facts:
                    if fts5_store(f): s_fts5 += 1
                    if chroma_store(f): s_chroma += 1
                    if memorygraph_store(f, "extraction"): s_mg += 1
            self._send_json({"facts": facts, "count": len(facts),
                             "stored": {"fts5": s_fts5, "chroma": s_chroma, "memorygraph": s_mg},
                             "model": EXTRACTION_MODEL, "agent": AGENT_ID})

        # ---- Share: send to another agent's container ----
        elif self.path == "/memory/share":
            to_agent = body.get("to", ""); fact = body.get("fact", "")
            if not to_agent or not fact: self._send_json({"error":"to and fact required"}, 400); return
            target_url = PEERS.get(to_agent)
            if not target_url:
                self._send_json({"error": f"unknown peer: {to_agent}", "peers": list(PEERS.keys())}, 400); return
            try:
                payload = json.dumps({"from": AGENT_ID, "fact": fact}).encode()
                req = urllib.request.Request(f"{target_url}/memory/receive", data=payload,
                    headers={"Content-Type":"application/json"}, method="POST")
                resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
                self._send_json({"from": AGENT_ID, "to": to_agent, "received": resp.get("status") == "stored"})
            except Exception as e:
                self._send_json({"from": AGENT_ID, "to": to_agent, "error": str(e)[:100]})

        # ---- Receive: called by another agent's /memory/share ----
        elif self.path == "/memory/receive":
            from_agent = body.get("from", "?"); fact = body.get("fact", "")
            if not fact: self._send_json({"error":"fact required"}, 400); return
            fts5_ok = fts5_store(fact)
            chroma_ok = chroma_store(fact)
            mg_ok = memorygraph_store(fact, from_agent)
            self._send_json({"status": "stored" if (fts5_ok or chroma_ok or mg_ok) else "failed",
                             "from": from_agent, "fts5": fts5_ok, "chroma": chroma_ok, "memorygraph": mg_ok})

        # ---- Broadcast: send to all peers ----
        elif self.path == "/memory/broadcast":
            fact = body.get("fact", "")
            if not fact: self._send_json({"error":"fact required"}, 400); return
            results = {}
            for name, url in PEERS.items():
                try:
                    payload = json.dumps({"from": AGENT_ID, "fact": fact}).encode()
                    req = urllib.request.Request(f"{url}/memory/receive", data=payload,
                        headers={"Content-Type":"application/json"}, method="POST")
                    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
                    results[name] = resp.get("status") == "stored"
                except Exception as e:
                    results[name] = str(e)[:60]
            self._send_json({"from": AGENT_ID, "broadcasted_to": results})

        # ---- Session search ----
        elif self.path == "/memory/sessions/search":
            query = body.get("query", ""); limit = body.get("limit", 5)
            if not query: self._send_json({"error":"query required"}, 400); return
            self._send_json(session_search(query, limit))

        # ---- Session import ----
        elif self.path == "/memory/sessions/import":
            sid = body.get("session_id",""); title = body.get("title", sid)
            messages = body.get("messages",[])
            if not sid or not messages: self._send_json({"error":"session_id and messages required"}, 400); return
            parts = [f"[{m.get('role',m.get('type','user'))}] {m.get('content','')}" for m in messages]
            full_text = "\n".join(parts); preview = full_text[:300]
            _init_sessions_fts5()
            now = datetime.datetime.utcnow().isoformat() + "Z"
            conn = sqlite3.connect(FTS5_DB)
            try:
                conn.execute("""INSERT OR REPLACE INTO sessions
                    (id,title,preview,content,message_count,created_at,indexed_at)
                    VALUES(?,?,?,?,?,?,?)""", [sid,title,preview,full_text[:10000],len(messages),now,now])
                conn.commit(); fts5_ok = True
            except Exception as e:
                fts5_ok = False
            finally: conn.close()
            emb = embed_litellm([preview]); chroma_ok = False
            if emb:
                try:
                    client = _get_chroma()
                    coll = client.get_or_create_collection(name=CHROMA_SESSIONS, metadata={"hnsw:space":"cosine"})
                    coll.upsert(ids=[sid], documents=[preview], embeddings=emb,
                                metadatas=[{"session_id":sid,"title":title,"message_count":len(messages)}])
                    chroma_ok = True
                except Exception: pass
            self._send_json({"session_id":sid,"title":title,"message_count":len(messages),
                             "stored":{"fts5":fts5_ok,"chroma":chroma_ok}})

        else:
            self._send_json({"error":"not found"}, 404)

    def log_message(self, format, *args):
        print(f"[API:{AGENT_ID}] {args[0]}", file=sys.stderr)

if __name__ == "__main__":
    print(f"[{AGENT_ID}] Memory API on {LISTEN_HOST}:{LISTEN_PORT}", file=sys.stderr)
    print(f"  Peers: {PEERS}, Shared: {SHARED_URL}", file=sys.stderr)
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), MemoryAPIHandler)
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()
