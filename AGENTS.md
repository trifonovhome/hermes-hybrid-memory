# AGENTS.md — Upgrade Guide for AI Agents

[🇷🇺 Русская версия](AGENTS.ru.md)

> Version: 2026-06-19

## Current Architecture

| Component | Details |
|-----------|---------|
| **MemoryGraph** | Direct SQLite in host plugin + Docker container |
| **Embeddings** | embeddinggemma-300M (768d) local via llama-cpp |
| **Chroma collection** | `memory_{AGENT_ID}` (shared between host plugin and Docker) |
| **Recency boost** | Active in all 3 backends (FTS5, Chroma, MemoryGraph) |
| **Fusion weights** | 0.45×Chroma + 0.25×FTS5 + 0.30×MemoryGraph |

## 1. Host Provider (hybrid_memory_provider.py)

File: `~/scripts/hybrid_memory_provider.py`

Ensure it has:
- `LocalEmbedder` (llama-cpp, GGUF, 768d)
- `MemoryGraphBackend` (direct SQLite)
- `recency_boost()` function
- `COLLECTION_NAME = "memory_{AGENT_ID}"` — matches Docker container
- `LOCAL_EMBED_MODEL_PATH` pointing to `embeddinggemma-300M-Q8_0.gguf`
- Fusion: 0.45 chroma + 0.25 fts5 + 0.30 memorygraph
- Recency boost: `score × (0.7 + 0.3 × boost)` in all backends

### Verification:

```bash
python3 ~/scripts/hybrid_memory_provider.py status
# Should show:
#   Embeddings: embeddinggemma-300M (GGUF, 768d) via llama-cpp
#   Chroma count: N (768d)
#   MemoryGraph count: N

python3 ~/scripts/hybrid_memory_provider.py search "test query"
# Should show results from 3 backends: fts5, chroma_768d, memorygraph
```

## 2. Hermes Plugin

File: `plugin/__init__.py` in the repository

### What should be present:

1. `system_prompt_block()` mentions 3 backends + SecureStore
2. `SEARCH_SCHEMA` describes 3 backends
3. `SECURE_GET_SCHEMA` provides `hybrid_secure_get(key)` tool
4. `HybridMemoryProvider.__init__()` uses `LocalEmbedder` + `MemoryGraphBackend`

### Installation:

```bash
cd ~/projects/hermes-hybrid-memory
git pull origin master

# Copy to Hermes
cp plugin/__init__.py ~/.hermes/profiles/<agent>/plugins/memory/hybrid/__init__.py

# Restart Hermes
hermes restart  # or restart Docker container
```

## 3. Docker Containers

Containers already use:
- Local embeddings (`LOCAL_EMBED_MODEL=/data/models/embeddinggemma-300M-Q8_0.gguf`)
- MemoryGraph in `unified_search()`
- Recency boost in all backends

When rebuilding, ensure `docker-compose.yml` contains:
```yaml
environment:
  - LOCAL_EMBED_MODEL=/data/models/embeddinggemma-300M-Q8_0.gguf
volumes:
  - ./data/models:/data/models:ro
```

## 4. Recency Boost (Timestamps)

All facts store `created_at` metadata. Boost applied at search time:

```python
def recency_boost(created_str: str) -> float:
    today     → 1.00
    1-7 days  → 1.00 → 0.60  (linear)
    8-30 days → 0.60 → 0.30  (linear)
    31-90 days → 0.30 → 0.05 (linear)
    90+ days  → 0.05

applied_score = base_score × (0.7 + 0.3 × boost)
```

**Where timestamps are stored:**

| Backend | Field | Format |
|---------|-------|--------|
| FTS5 | `facts.created_at` (TEXT) | `2026-06-14T18:32:08.199747+00:00` |
| Chroma | `metadatas["created_at"]` | `2026-06-14T18:27:05.737907Z` |
| MemoryGraph | `nodes.created_at` (TIMESTAMP) | `2026-06-12 20:25:42` |

During reindexing (FTS5→Chroma), timestamps transfer atomically — old facts
don't get today's date.

The 0.7 coefficient ensures even very old facts (>90 days) retain 71.5% of
their base weight. Memory never fully "forgets" — it only fades.

## 5. Post-Upgrade Verification

```bash
# 1. Check status
python3 ~/scripts/hybrid_memory_provider.py status

# 2. Search — should use all 3 backends
python3 ~/scripts/hybrid_memory_provider.py search "docker"

# 3. In Hermes — call hybrid_status
# (should show memorygraph_count > 0 and embed_backend = llama-cpp)

# 4. In Hermes — call hybrid_search
# (results should include backend=memorygraph and backend=chroma_768d)

# 5. Test SecureStore
curl -X POST http://127.0.0.1:8711/memory/secrets \
  -H 'Content-Type: application/json' \
  -d '{"key":"test","value":"hello"}'
curl http://127.0.0.1:8711/memory/secrets/test
# In Hermes: hybrid_secure_get("test") → {"key":"test","value":"hello"}
```

## 6. Affected Files

| File | Location |
|------|----------|
| `~/scripts/hybrid_memory_provider.py` | Host script (not in git) |
| `plugin/__init__.py` | `github.com/trifonovhome/hermes-hybrid-memory` |
| `agent/hybrid_memory_agent.py` | `github.com/trifonovhome/hermes-hybrid-memory` |
| `docker-compose.yml` | `~/infra/docker/hermes-hybrid-memory/` (local) |
