# Hermes Hybrid Memory

Per-agent hybrid memory stack for [Hermes Agent](https://github.com/nousresearch/hermes-agent).
4 backends in one Docker container: FTS5 (keyword), Chroma + bge-m3 (semantic),
Shared Pool (remote), MemoryGraph (graph + spaCy NLP).

## Architecture

```
agent-alpha (:8642)    agent-beta (:8643)     agent-gamma (:8647, shared master)
┌──────────────────┐   ┌──────────────────┐   ┌───────────────────────┐
│ Hermes Gateway   │   │ Hermes Gateway   │   │ Hermes Gateway        │
│ Memory API :8711 │   │ Memory API :8712 │   │ Memory API :8710 ⭐   │
│ FTS5 + Chroma    │   │ FTS5 + Chroma    │   │ FTS5 + Chroma         │
│ + MemoryGraph    │   │ + MemoryGraph    │   │ + MemoryGraph         │
└──────┬───────────┘   └──────┬───────────┘   └───────────┬───────────┘
       │ share/broadcast      │                           │
       └──────────────────────┴───────────────────────────┘
                    все читают agent-gamma (:8710) как SHARED_URL
```

- **agent-gamma** — shared master, SHARED_URL=http://127.0.0.1:8710
- **agent-alpha / agent-beta** — изолированные агенты с share/broadcast

## 4 Backends

| # | Backend | Storage | Search Type | Weight |
|---|---------|---------|-------------|--------|
| 1 | **FTS5** | SQLite | BM25 keyword, <1ms | 0.20× |
| 2 | **Chroma + bge-m3** | ChromaDB | Cosine semantic, 50-200ms | 0.50× |
| 3 | **Shared Pool** | Remote HTTP | agent-gamma :8710 | 0.45× |
| 4 | **MemoryGraph** | SQLite + spaCy | Graph + FTS, recency boost | 0.15+ |

## Quick Start

### Prerequisites

- Docker 24+
- LiteLLM proxy running on `:4000` (or set `LITELLM_URL`)
- Hermes Agent profiles for each agent

### 1. Clone

```bash
git clone https://github.com/trifonovhome/hermes-hybrid-memory.git
cd hermes-hybrid-memory
```

### 2. Create data directories

```bash
mkdir -p data/{alpha,beta,gamma}/{fts5,chroma,memorygraph}
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

# Start all three
docker compose -f docker/docker-compose.yml up -d

# Or start individually
docker compose -f docker/docker-compose.yml up -d agent-alpha
```

### 5. Verify

```bash
# Health checks
curl http://127.0.0.1:8710/health  # agent-gamma (shared)
curl http://127.0.0.1:8711/health  # agent-alpha
curl http://127.0.0.1:8712/health  # agent-beta

# Memory stats
curl http://127.0.0.1:8711/status

# Search
curl -X POST http://127.0.0.1:8711/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"setup instructions","limit":3}'

# Share fact between agents
curl -X POST http://127.0.0.1:8711/memory/share \
  -H 'Content-Type: application/json' \
  -d '{"to":"agent-beta","fact":"Important discovery"}'
```

## REST API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | `{"status":"ok","agent":"..."}` |
| GET | `/status` | `{"fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Unified 4-backend search |
| POST | `/memory/extract` | LLM fact extraction → all backends |
| POST | `/memory/share` | Send fact to peer agent |
| POST | `/memory/receive` | Receive fact from peer |
| POST | `/memory/broadcast` | Send fact to all peers |
| POST | `/memory/sessions/search` | FTS5 + Chroma session search |
| POST | `/memory/sessions/import` | Import session (Honcho format) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_ID` | agent-alpha | Agent identity |
| `AGENT_PORT` | 8642 | Hermes gateway port |
| `MEMORY_PORT` | 8711 | Memory API port |
| `LISTEN_PORT` | 8711 | Actual bind port (use same as MEMORY_PORT) |
| `LITELLM_URL` | http://127.0.0.1:4000 | LiteLLM for embeddings + extraction |
| `LITELLM_API_KEY` | — | LiteLLM master key |
| `SHARED_URL` | http://127.0.0.1:8710 | Shared pool (agent-gamma) |
| `PEERS` | — | `name:host:port,...` for share/broadcast |
| `EXTRACTION_MODEL` | deepseek-v4-pro | LLM for fact extraction |
| `EMBED_MODEL` | bge-m3 | Embedding model |
| `FTS5_DB` | /data/memory/fts5/memory.db | FTS5 database path |
| `CHROMA_DIR` | /data/memory/chroma | ChromaDB persistent directory |
| `MEMORYGRAPH_DIR` | /data/memory/memorygraph | MemoryGraph database directory |

## Fusion Algorithm

4 backends queried in parallel (limit × 2). Results deduplicated by normalized key.
Weights applied: shared 0.45×, chroma 0.50×, fts5 0.20×, memorygraph 0.15+tag_bonus.
Recency boost applied across all backends: 1.0 (today) → 0.05 (90+ days).

## Docs

- [SPECIFICATION.md](docs/SPECIFICATION.md) — Full technical specification
- [SKILL.md](SKILL.md) — Hermes Agent skill definition

## Hermes Plugin

The `plugin/` directory contains a Hermes Agent memory provider plugin.
Install it to enable `hybrid_search` and `hybrid_status` tools in your agent:

```bash
# Copy plugin to Hermes plugins directory
cp -r plugin/ ~/.hermes/hermes-agent/plugins/memory/hybrid/

# Activate
hermes config set memory.provider hybrid
```

The plugin reads from the same SQLite/Chroma databases used by the Docker containers.

## License

MIT
