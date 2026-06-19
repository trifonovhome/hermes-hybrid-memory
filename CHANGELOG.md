# Changelog

## 1.2.0 - 2026-06-19

_Third public release. Documentation overhaul and architecture cleanup._

### Changed

- **Breaking:** Remove cross-agent memory sharing endpoints (`/memory/share`, `/memory/receive`, `/memory/broadcast`) from public API ([`de272a5`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/de272a5))
- Update fusion weights to 0.45 Chroma + 0.25 FTS5 + 0.30 MemoryGraph ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- Switch embeddings to local embeddinggemma-300M (768d) via llama-cpp, drop bge-m3/LiteLLM dependency ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- Use abstract Chroma collection name `memory_{AGENT_ID}` instead of specific names ([`1e18bb6`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/1e18bb6))
- Reorganize all documentation into separate EN/RU files with language switchers ([`31b5519`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/31b5519))
- Remove all historical references from documentation — only current architecture ([`c6b7e97`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/c6b7e97))
- Move sharing functionality to private repository ([`8b1e069`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/8b1e069))

### Added

- MemoryGraph backend in host plugin (direct SQLite, 259 nodes) ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- Recency boost applied uniformly to all 3 backends with formula `score × (0.7 + 0.3 × boost)` ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- Detailed timestamp documentation — storage locations, formats, reindex behavior ([`1e18bb6`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/1e18bb6))
- Dynamic `system_prompt_block` in plugin — reads real backend counts ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- `AGENTS.md` upgrade guide for AI agents (EN + RU) ([`2549e7e`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/2549e7e))

### Removed

- Shared Pool backend from public API — all `/memory/share`, `/memory/receive`, `/memory/broadcast` endpoints ([`de272a5`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/de272a5))
- bge-m3 embedding support (1024d, LiteLLM-dependent) — replaced by local embeddinggemma-300M ([`0a44cc7`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/0a44cc7))
- Headroom, LiteLLM, Ollama references from documentation ([`c6b7e97`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/c6b7e97))

## 1.1.0 - 2026-06-14

_Second release. Local embeddings and migration from centralized LiteLLM._

### Added

- Local GGUF embedding support via `LOCAL_EMBED_MODEL` env var (embeddinggemma-300M, 768d) ([`3ad5d56`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/3ad5d56))
- SecureStore — Age-encrypted secrets storage for tokens and API keys ([`3ad5d56`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/3ad5d56))

### Changed

- Migrate all agents from bge-m3/LiteLLM to local embeddinggemma-300M GGUF ([`3ad5d56`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/3ad5d56))
- Chroma collections rebuilt for 768d dimension ([`092a006`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/092a006))

### Fixed

- Local embedding return format — handle both `embedding` key and `data[0].embedding` ([`092a006`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/092a006))

## 1.0.0 - 2026-06-13

_First public release._

### Added

- Per-agent hybrid memory stack: FTS5 + Chroma (bge-m3) + Shared Pool + MemoryGraph ([`abb0995`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/abb0995))
- Unified Docker image with `AGENT_ID`-based identity ([`f7bf074`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/f7bf074))
- Hermes Agent plugin with `hybrid_search` and `hybrid_status` tools ([`3b8d8ba`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/3b8d8ba))
- Recency boost for FTS5 and Chroma with `created_at` timestamps ([`c2becfd`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/c2becfd))
- Cross-agent memory sharing via `/memory/share` and `/memory/broadcast`
- Shared Pool (agent-gamma) for centralized fact storage
- Session search and import (FTS5 + Chroma)
- Technical specification (SPECIFICATION.md)

### Fixed

- FTS5 database path normalized to `memory.db` ([`a1bf9b2`](https://github.com/trifonovhome/hermes-hybrid-memory/commit/a1bf9b2))
