# Hermes Hybrid Memory — Specification v1.2

[🇷🇺 Русская версия](SPECIFICATION.ru.md)

**Date:** June 19, 2026
**Repo:** [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory)

---

## 1. Overview

Hermes Hybrid Memory is a **per-agent** system with 3 backends running inside
unified Docker containers. Each container hosts both Memory API and Hermes Gateway.
One Docker image, `AGENT_ID` determines identity.

**Principles:**
- **Isolation:** each agent has its own data (FTS5/Chroma/MemoryGraph)
- **Unified:** agent + memory in one container, `network_mode:host`

---

## 2. Backends

### 2.1 FTS5 — Keyword Precision

| Parameter | Value |
|-----------|-------|
| Storage | SQLite `/data/fts5/memory.db` |
| Index | FTS5 virtual table over `content` |
| Search | BM25 keyword matching |
| Latency | < 1 ms |
| Fallback | Multi-word → OR-search |
| Tables | `facts` (id, content, source, created_at, updated_at) + `facts_fts` (virtual) |
| Sessions | `sessions` + `sessions_fts` |

### 2.2 Chroma — Semantic Understanding

| Parameter | Value |
|-----------|-------|
| Storage | ChromaDB Persistent `/data/chroma/` |
| Embedding | embeddinggemma-300M-Q8_0 (768d), via llama-cpp-python |
| Mode selection | `LOCAL_EMBED_MODEL` env var → local GGUF |
| Metric | Cosine distance → similarity score |
| Collections | `memory_{AGENT_ID}` + `sessions_{AGENT_ID}` |
| Latency | 100–500 ms (CPU, 300M params) |
| Recency boost | × (0.7 + 0.3 × recency_boost(created_at)) |

### 2.3 MemoryGraph — Graph Relationships

| Parameter | Value |
|-----------|-------|
| Storage | SQLite `{MEMORYGRAPH_DIR}/memorygraph_{AGENT_ID}.db` |
| SDK | `memorygraphMCP` v0.12.4 (SQLiteMemoryDatabase + SQLiteFallbackBackend) |
| NLP | spaCy `ru_core_news_md` (Russian NER) |
| Memory types | GENERAL, TASK, WORKFLOW, COMMAND, PROBLEM, PROJECT |
| Search | FTS over nodes + per-word fallback with dedup |
| Recency boost | × (0.7 + 0.3 × recency_boost(created_at)) |
| Fusion weight | 0.30 + tag_bonus (up to 0.05) |

### 2.4 SecureStore — Encrypted Secrets

| Parameter | Value |
|-----------|-------|
| Storage | Age-encrypted file `/data/secrets/secrets.enc` |
| Key | `AGE_KEY` (env var) — age secret key |
| Encryption | age (rage) — X25519 + ChaCha20-Poly1305 |
| API | `GET/POST/DELETE /memory/secrets` and `GET /memory/secrets/{key}` |
| Use cases | HA tokens, SSH passwords, API keys |

Without `AGE_KEY`, SecureStore returns 503.

### 2.5 Recency Boost (Timestamps)

Every fact in every backend stores a creation timestamp. At search time,
a recency boost gives priority to recent facts over old ones.

**Formula:**

```
recency_boost(created_at):
  today      → 1.00
  1-7 days   → 0.60 + 0.40 × (7 − days) / 7
  8-30 days  → 0.30 + 0.30 × (30 − days) / 23
  31-90 days → 0.05 + 0.25 × (90 − days) / 60
  90+ days   → 0.05

final_score = base_score × (0.7 + 0.3 × recency_boost(created_at))
```

Score multiplier by age:

| Age | boost | multiplier | Effect |
|-----|-------|-----------|--------|
| Today | 1.00 | ×1.00 | Full weight |
| 3 days | 0.83 | ×0.95 | −5% |
| 7 days | 0.60 | ×0.88 | −12% |
| 14 days | 0.47 | ×0.84 | −16% |
| 30 days | 0.30 | ×0.79 | −21% |
| 60 days | 0.16 | ×0.75 | −25% |
| 90+ days | 0.05 | ×0.715 | −28.5% |

**Where timestamps are stored:**

| Backend | Field | Format | Written by |
|---------|-------|--------|------------|
| FTS5 | `facts.created_at` (TEXT) | `2026-06-14T18:32:08.199747+00:00` | `fts5_store()` |
| Chroma | `metadatas["created_at"]` | `2026-06-14T18:27:05.737907Z` | `chroma_store()` |
| MemoryGraph | `nodes.created_at` (TIMESTAMP) | `2026-06-12 20:25:42` | `memorygraph_store()` |

**Reindexing behavior:** when transferring facts from FTS5 to Chroma, timestamps
are transferred atomically — old facts don't get today's date. A fact created
a week ago correctly gets 0.88 boost, not 1.0.

**Why 0.7:** the 0.7 coefficient ensures even very old facts (>90 days) retain
71.5% of their base weight. Memory never fully "forgets" — it only fades.

---

## 3. Container Architecture

### 3.1 Docker Compose

| Service | Container | AGENT_ID | Gateway | Memory |
|---------|-----------|----------|---------|--------|
| agent-alpha | agent-alpha | agent-alpha | :8642 | :8711 |
| agent-beta | agent-beta | agent-beta | :8643 | :8712 |

**All:** `network_mode:host`, `user:1000:1000`, `restart:unless-stopped`.

### 3.2 Volume Mounts (per-agent)

| Container | FTS5 | Chroma | MemoryGraph | Profile |
|-----------|------|--------|-------------|---------|
| agent-alpha | `data/alpha/fts5` | `data/alpha/chroma` | `data/alpha/memorygraph` | `./profiles/alpha` |
| agent-beta | `data/beta/fts5` | `data/beta/chroma` | `data/beta/memorygraph` | `./profiles/beta` |

### 3.3 LLM Chain

```
TUI (host) ──→ Hermes Gateway :8642 (Docker) ──→ DeepSeek API
```

Memory API uses `LITELLM_URL` for fact extraction LLM calls.

---

## 4. REST API Endpoints

All on `127.0.0.1:{MEMORY_PORT}`:

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | `{"status":"ok","agent":"...","port":...}` |
| GET | `/status` | `{"agent":"...","fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Unified 3-backend search |
| POST | `/memory/extract` | LLM fact extraction → all backends |
| POST | `/memory/sessions/search` | Chat history search (FTS5 + Chroma) |
| POST | `/memory/sessions/import` | Import session |
| GET | `/memory/secrets` | List SecureStore keys |
| GET | `/memory/secrets/{key}` | Get secret value |
| POST | `/memory/secrets` | Store `{"key":"...","value":"..."}` |
| DELETE | `/memory/secrets/{key}` | Delete secret |

---

## 5. Fusion Algorithm (`unified_search`)

### 5.1 Stages

1. Query all 3 backends in parallel (limit × 2 each)
2. Dedup by normalized key (alnum, lowercase, 80 chars)
3. Apply fusion weights
4. Sort by fusion score descending
5. Trim to `limit` results

### 5.2 Weights (3-backend)

| Backend | Weight | Type |
|---------|--------|------|
| Chroma | 0.45 × score | New fact |
| FTS5 | 0.25 × min(1.0, bm25_norm + 0.2) | New OR boost existing |
| MemoryGraph | 0.30 + tag_bonus | New OR boost existing |

**Boosting:** if a fact is already found by Chroma, FTS5 adds +0.25 and
MemoryGraph adds +0.30 to the fusion score (additive, not replacement).

### 5.3 Response

```json
{
 "query": "unified Docker containers",
 "results": [
   {
     "content": "...",
     "score": 0.85,
     "fusion": 0.62,
     "backend": "chroma",
     "keyword_match": true,
     "graph_match": false
   }
 ],
 "backends": {
   "fts5": 6,
   "chroma": 3,
   "memorygraph": 4
 }
}
```

---

## 6. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_ID` | agent-alpha | Agent identity |
| `MEMORY_PORT` | 8711 | Memory API port |
| `AGENT_PORT` | 8642 | Hermes Gateway port |
| `LISTEN_HOST` | 127.0.0.1 | Memory API bind address |
| `LISTEN_PORT` | 8711 | Alternative name for MEMORY_PORT |
| `LOCAL_EMBED_MODEL` | `/data/models/embeddinggemma-300M-Q8_0.gguf` | Local GGUF embedding model |
| `FTS5_DB` | /data/fts5/memory.db | FTS5 database path |
| `CHROMA_DIR` | /data/chroma | ChromaDB directory |
| `MEMORYGRAPH_DIR` | /data/memorygraph | MemoryGraph directory |
| `CHROMA_COLLECTION` | memory_{AGENT_ID} | Chroma collection name (auto) |
| `CHROMA_SESSIONS` | sessions_{AGENT_ID} | Sessions collection name (auto) |
| `SECRETS_FILE` | /data/secrets/secrets.enc | Encrypted secrets file |
| `AGE_KEY` | — | Age secret key for SecureStore |

---

## 7. Pitfalls

### 7.1 Docker
- **UID mismatch:** volumes created with wrong UID → "readonly database"
  → fix: `chown -R 1000:1000` via alpine
- **Build cache:** Dockerfile changes require `--no-cache`
- **Symlinks:** `.` → symlink, `patch`/`write_file` may target wrong location

### 7.2 MemoryGraph
- **Multi-word search:** SDK `search_memories(match_mode="any")` returns 0 —
  need per-word fallback with dedup
- **recency_boost must be defined BEFORE memorygraph_search in the file**
- **`/status` count:** use `get_memory_statistics()`, not `len(_mg_db[...])`

### 7.3 FTS5
- **Directory not created:** `_init_fts5()` calls `os.makedirs()` only at
  import time — ensure `_ensure_fts5()` before every operation

### 7.4 MemoryGraph integer overflow
- **Symptom:** `SearchQuery limit=8589934592` → Pydantic validation error
- **Cause:** unbounded `limit * 2` passing between unified_search → memorygraph_search
- **Fix:** `limit = min(max(1, limit), 100)` in both functions

### 7.5 Local embeddings
- **First load:** entrypoint auto-downloads GGUF from HuggingFace (~300 MB)
- **Dimensions:** embeddinggemma-300M → 768d (bge-m3 = 1024d) — collections are incompatible
- **Performance:** ~200–500ms per embedding on CPU, sufficient for home agent

### 7.6 SecureStore
- **Without `AGE_KEY`:** all `/memory/secrets` requests return 503
- **age must be installed** in the container (`apt-get install age`)
- **Atomic write:** `set()` rewrites the entire file

### 7.7 hybrid_search from host
- **Stale Chroma:** `hybrid_status` may show a different Chroma dir —
  verify `chroma_dir` in the response

---

## 8. File Paths

| What | Where |
|------|-------|
| Source code | `./agent/hybrid_memory_agent.py` |
| Build context | `./docker/` |
| Docker Compose | `./docker/docker-compose.yml` |
| Data (agent-alpha) | `./data/alpha/{fts5,chroma,memorygraph}/` |
| Data (agent-beta) | `./data/beta/{fts5,chroma,memorygraph}/` |
| Hermes profile | `./profiles/alpha/config.yaml` |
| GitHub | [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory) |

---

## 9. Diagnostics (Quick Commands)

```bash
# Container status
docker ps --filter "name=agent-" --format "table {{.Names}}\t{{.Status}}"

# Ports
ss -tlnp | grep -E '864[23]|871[12]'

# Memory status for each agent
for port in 8711 8712; do
 echo "=== :$port ==="
 curl -s http://127.0.0.1:$port/status | python3 -m json.tool
done

# Search
curl -s -X POST http://127.0.0.1:8711/memory/search \
 -H 'Content-Type: application/json' \
 -d '{"query":"Docker unified containers","limit":3}' | python3 -m json.tool

# LLM extraction
curl -s -X POST http://127.0.0.1:8711/memory/extract \
 -H 'Content-Type: application/json' \
 -d '{"text":"Configured Headroom proxy on port 8787"}' | python3 -m json.tool
```
