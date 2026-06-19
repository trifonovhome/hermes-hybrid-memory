---
name: hermes-hybrid-memory
description: Per-agent hybrid memory stack (FTS5 + Chroma embeddinggemma-300M + MemoryGraph + SecureStore) for Hermes Agent.
version: 1.2.0
---

# Hermes Hybrid Memory

Per-agent memory stack with 3 backends. One Docker image, one entrypoint — `AGENT_ID` determines identity.

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

## Quick Start

```bash
git clone https://github.com/trifonovhome/hermes-hybrid-memory.git
cd hermes-hybrid-memory
mkdir -p data/{alpha,beta}/{fts5,chroma,memorygraph}
mkdir -p profiles/{alpha,beta}
chown -R 1000:1000 data/ profiles/
docker compose -f docker/docker-compose.yml build --no-cache
docker compose -f docker/docker-compose.yml up -d
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | `{"status":"ok","agent":"..."}` |
| GET | `/status` | `{"fts5":N,"chroma":N,"memorygraph":N,"secrets":N}` |
| POST | `/memory/search` | Unified 3-backend search |
| POST | `/memory/extract` | LLM fact extraction → all backends |
| POST | `/memory/sessions/search` | FTS5 + Chroma session search |
| POST | `/memory/sessions/import` | Import session |
| GET | `/memory/secrets` | List SecureStore keys |
| GET | `/memory/secrets/{key}` | Read a secret |
| POST | `/memory/secrets` | Store `{"key":"...","value":"..."}` |
| DELETE | `/memory/secrets/{key}` | Delete a secret |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_ID` | agent-alpha | Agent identity |
| `AGENT_PORT` | 8642 | Hermes gateway port |
| `MEMORY_PORT` | 8711 | Memory API port |
| `LISTEN_PORT` | 8711 | Actual bind port |
| `LOCAL_EMBED_MODEL` | `/data/models/embeddinggemma-300M-Q8_0.gguf` | Local GGUF embedding model |
| `AGE_KEY` | — | Age secret key for SecureStore (required) |
| `SECRETS_FILE` | /data/secrets/secrets.enc | Encrypted secrets file |
| `FTS5_DB` | /data/fts5/memory.db | FTS5 database path |
| `CHROMA_DIR` | /data/chroma | ChromaDB directory |
| `MEMORYGRAPH_DIR` | /data/memorygraph | MemoryGraph directory |

## Fusion Weights

| Backend | Weight | Role |
|---------|--------|------|
| Chroma | 0.45 × score | Semantic similarity |
| FTS5 | 0.25 × bm25_norm | Keyword precision |
| MemoryGraph | 0.30 + tag_bonus | Graph relationships |

All backends use recency boost: `score × (0.7 + 0.3 × recency_boost(created_at))`.

## SecureStore — Encrypted Secrets

Age-encrypted key-value store inside the container (`/data/secrets/secrets.enc`).
Requires `AGE_KEY` env var to be active.

### Using from agent code

```python
# Store a secret (e.g., HA token)
curl -X POST http://127.0.0.1:{MEMORY_PORT}/memory/secrets \
  -H 'Content-Type: application/json' \
  -d '{"key":"ha_token","value":"eyJ..."}'

# Read a secret
curl http://127.0.0.1:{MEMORY_PORT}/memory/secrets/ha_token
# → {"key":"ha_token","value":"eyJ..."}

# List all keys
curl http://127.0.0.1:{MEMORY_PORT}/memory/secrets
# → {"keys":["ha_token","api_key"],"agent":"..."}

# Delete a secret
curl -X DELETE http://127.0.0.1:{MEMORY_PORT}/memory/secrets/ha_token
```

### Setup

```bash
# Generate key (once)
age-keygen -o key.txt
# Extract: grep "AGE-SECRET-KEY-" key.txt

# Pass to container
# docker-compose.yml:
#   environment:
#     - AGE_KEY=AGE-SECRET-KEY-1QV7LZ2...3XYZ
```

### Security

- Secrets never written to disk in plaintext
- Decrypted content lives in memory only
- Atomic writes — entire file rewritten on each change
- Lost AGE_KEY = lost secrets (no recovery)

## Docs

- [README.md](README.md) — Overview and quick start
- [README.ru.md](README.ru.md) — Russian version
- [docs/SPECIFICATION.md](docs/SPECIFICATION.md) — Full technical specification
- [docs/SPECIFICATION.ru.md](docs/SPECIFICATION.ru.md) — Russian version
- [AGENTS.md](AGENTS.md) — Upgrade guide for AI agents
- [AGENTS.ru.md](AGENTS.ru.md) — Russian version

## Repo

https://github.com/trifonovhome/hermes-hybrid-memory
