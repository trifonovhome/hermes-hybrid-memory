# Hermes Hybrid Memory

[🇷🇺 Русская версия](README.ru.md)

Per-agent hybrid memory stack for [Hermes Agent](https://github.com/nousresearch/hermes-agent).
**3 backends in one Docker container + host plugin**: FTS5 (keyword), Chroma (semantic, local GGUF),
MemoryGraph (graph relationships). + SecureStore (encrypted secrets).

## Architecture

```
agent-alpha (:8642)    agent-beta (:8643)
┌──────────────────┐   ┌──────────────────┐
│ Hermes Gateway   │   │ Hermes Gateway   │
│ Memory API :8711 │   │ Memory API :8712 │
│ FTS5 + Chroma    │   │ FTS5 + Chroma    │
│ + MemoryGraph    │   │ + MemoryGraph    │
└──────────────────┘   └──────────────────┘
    isolated agents — each with its own memory
```

## 3 Backends

| # | Backend | Storage | Search Type | Fusion Weight |
|---|---------|---------|-------------|---------------|
| 1 | **FTS5** | SQLite | BM25 keyword + recency boost | 0.25× |
| 2 | **Chroma** | ChromaDB | Semantic (local GGUF, 768d) | 0.45× |
| 3 | **MemoryGraph** | SQLite | Graph relationships + recency boost | 0.30× |
| 🔐 | **SecureStore** | Age-encrypted | Encrypted key-value (tokens, API keys) | — |

Chroma collection name: `memory_{AGENT_ID}` (auto-generated per agent).

## Recency Boost (Timestamps)

Every fact stores a `created_at` timestamp (ISO 8601). At search time,
a recency boost is applied to each backend's base score:

```
final_score = base_score × (0.7 + 0.3 × recency_boost(created_at))
```

### recency_boost formula

```
days since creation    boost    score multiplier
──────────────────    ──────    ─────────────────
today                   1.00    × (0.7 + 0.30) = ×1.00
1–7 days            1.00→0.60   × (0.7 + 0.30)→(0.7 + 0.18)
8–30 days           0.60→0.30   × (0.7 + 0.18)→(0.7 + 0.09)
31–90 days          0.30→0.05   × (0.7 + 0.09)→(0.7 + 0.015)
90+ days               0.05     × (0.7 + 0.015) = ×0.715
```

- Today: full weight
- 1 week: 88–100% weight
- 1 month: 79–88% weight
- 90+ days: retains 71.5% weight — never zero

### Where timestamps are stored

| Backend | Field | Format |
|---------|-------|--------|
| FTS5 | `facts.created_at` (SQLite column) | ISO 8601 with timezone |
| Chroma | `metadatas["created_at"]` | ISO 8601 Z-suffix |
| MemoryGraph | `nodes.created_at` (SQLite column) | `YYYY-MM-DD HH:MM:SS` |

Timestamps are written at fact creation. During reindexing (FTS5→Chroma),
timestamps are transferred atomically — old facts don't get today's date.

## Quick Start

### Prerequisites

- Docker 24+
- GGUF embedding model: `embeddinggemma-300M-Q8_0.gguf` (319MB, 768d)
  - Place at `data/models/embeddinggemma-300M-Q8_0.gguf`
- Hermes Agent profiles for each agent

### 1. Clone

```bash
git clone https://github.com/trifonovhome/hermes-hybrid-memory.git
cd hermes-hybrid-memory
```

### 2. Create data directories

```bash
mkdir -p data/{alpha,beta}/{fts5,chroma,memorygraph}
mkdir -p profiles/{alpha,beta}
chown -R 1000:1000 data/ profiles/
```

### 3. Configure Hermes profiles

Create a minimal `profiles/alpha/config.yaml`:

```yaml
model:
  provider: custom
  model: deepseek-v4-pro
  base_url: ${LITELLM_URL:-http://127.0.0.1:4000}/v1

memory:
  provider: hybrid
  memory_char_limit: 5000
```

### 4. Build and start

```bash
# Build all agents
docker compose -f docker/docker-compose.yml build --no-cache

# Start all
docker compose -f docker/docker-compose.yml up -d

# Or start individually
docker compose -f docker/docker-compose.yml up -d agent-alpha
```

### 5. Verify

```bash
# Health checks
curl http://127.0.0.1:8711/health  # agent-alpha
curl http://127.0.0.1:8712/health  # agent-beta

# Memory stats
curl http://127.0.0.1:8711/status

# Search
curl -X POST http://127.0.0.1:8711/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"setup instructions","limit":3}'
```

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | `{"status":"ok","agent":"..."}` |
| GET | `/status` | `{"fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Unified 3-backend search |
| POST | `/memory/extract` | LLM fact extraction → all backends |
| POST | `/memory/sessions/search` | FTS5 + Chroma session search |
| POST | `/memory/sessions/import` | Import session |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_ID` | agent-alpha | Agent identity |
| `AGENT_PORT` | 8642 | Hermes gateway port |
| `MEMORY_PORT` | 8711 | Memory API port |
| `LISTEN_PORT` | 8711 | Actual bind port |
| `LOCAL_EMBED_MODEL` | `/data/models/embeddinggemma-300M-Q8_0.gguf` | Local GGUF embedding model |
| `FTS5_DB` | /data/fts5/memory.db | FTS5 database path |
| `CHROMA_DIR` | /data/chroma | ChromaDB persistent directory |
| `MEMORYGRAPH_DIR` | /data/memorygraph | MemoryGraph database directory |

## Fusion Algorithm

3 backends queried in parallel (limit × 2). Results deduplicated by normalized key
(alnum, lowercase, 80 chars). Weights applied additively:

| Step | Backend | Action |
|------|---------|--------|
| 1 | Chroma | New fact, fusion = 0.45 × cosine_score |
| 2 | FTS5 | New OR boost: +0.25 × normalized_bm25 |
| 3 | MemoryGraph | New OR boost: +0.30 + tag_bonus |

Sorted by fusion score descending, trimmed to limit.

## Docs

- [SPECIFICATION.md](docs/SPECIFICATION.md) — Full technical specification
- [SPECIFICATION.ru.md](docs/SPECIFICATION.ru.md) — Russian version
- [SKILL.md](SKILL.md) — Hermes Agent skill definition
- [AGENTS.md](AGENTS.md) — Upgrade instructions for AI agents
- [AGENTS.ru.md](AGENTS.ru.md) — Russian version

## Hermes Plugin

The `plugin/` directory contains a Hermes Agent memory provider plugin.
Install it to enable `hybrid_search` and `hybrid_status` tools in your agent:

```bash
cp -r plugin/ ~/.hermes/hermes-agent/plugins/memory/hybrid/
hermes config set memory.provider hybrid
```

The plugin reads from the same SQLite/Chroma databases used by the Docker containers.

## License

MIT
