---
name: hermes-hybrid-memory
description: Per-agent hybrid memory stack (FTS5 + Chroma bge-m3 + Shared Pool + MemoryGraph) for Hermes Agent.
version: 1.0.0
---

# Hermes Hybrid Memory

Per-agent memory stack with 4 backends. One Docker image, one entrypoint — `AGENT_ID` determines identity.

## Architecture

```
agent-alpha (:8642)    agent-beta (:8643)     agent-gamma (:8647)
┌──────────────────┐   ┌──────────────────┐   ┌───────────────────────┐
│ Hermes Gateway   │   │ Hermes Gateway   │   │ Hermes Gateway        │
│ Memory API :8711 │   │ Memory API :8712 │   │ Memory API :8710 ⭐   │
└──────┬───────────┘   └──────┬───────────┘   └───────────┬───────────┘
       │ share/broadcast      │                           │
       └──────────────────────┴───────────────────────────┘
            все читают agent-gamma (:8710) как SHARED_URL
```

- **agent-gamma** — shared master, `SHARED_URL=http://127.0.0.1:8710`
- **agent-alpha/beta** — изолированные, обмениваются через `/memory/share` и `/memory/broadcast`
- Все три — **один Docker-образ**, `AGENT_ID` определяет роль, `network_mode: host`

## Quick Start

```bash
git clone https://github.com/trifonovhome/hermes-hybrid-memory.git
cd hermes-hybrid-memory
mkdir -p data/{alpha,beta,gamma}/{fts5,chroma,memorygraph}
mkdir -p profiles/{alpha,beta}
chown -R 1000:1000 data/ profiles/
docker compose -f docker/docker-compose.yml build --no-cache
docker compose -f docker/docker-compose.yml up -d
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/memory/search` | Unified 4-backend search |
| POST | `/memory/extract` | LLM fact extraction → all backends |
| POST | `/memory/share` | Send fact to another agent's container |
| POST | `/memory/receive` | Receive fact from another agent |
| POST | `/memory/broadcast` | Send fact to all peers |
| POST | `/memory/sessions/search` | FTS5 + Chroma session search |
| POST | `/memory/sessions/import` | Import session from Honcho |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_ID` | agent-alpha | Agent identity |
| `AGENT_PORT` | 8642 | Hermes gateway port |
| `MEMORY_PORT` | 8711 | Memory API port |
| `LISTEN_PORT` | 8711 | Actual bind port |
| `LITELLM_URL` | http://127.0.0.1:4000 | LiteLLM proxy |
| `SHARED_URL` | http://127.0.0.1:8710 | Shared pool (agent-gamma) |
| `PEERS` | — | `name:host:port` for share/broadcast |
| `EXTRACTION_MODEL` | deepseek-v4-pro | LLM for fact extraction |
| `EMBED_MODEL` | bge-m3 | Embedding model |

## Fusion Weights

| Backend | Weight | Role |
|---------|--------|------|
| shared pool | 0.45 × score | Remote shared facts |
| Chroma | 0.50 × score | Semantic similarity |
| FTS5 | 0.20 × bm25_norm | Keyword precision |
| MemoryGraph | 0.15 + tag_bonus | Graph relationships |

## Repo

https://github.com/trifonovhome/hermes-hybrid-memory
