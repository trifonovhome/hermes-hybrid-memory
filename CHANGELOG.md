# Changelog

## v1.1.0 — Embedding Provider Switch (2026-06-20)

### Added
- **RemoteEmbedder** — OpenAI-совместимый внешний embedding-провайдер
- **EMBED_PROVIDER** env var: `local` (llama-cpp GGUF, по умолчанию) или `external` (OpenAI API)
- **EMBED_API_URL**, **EMBED_API_KEY**, **EMBED_API_MODEL** — настройки внешнего провайдера
- **Авто-коллекции по размерности** — при смене эмбеддера с 768d на 1024d ChromaBackend создаёт новую коллекцию с суффиксом `_{N}d`
- `system_prompt_block()` динамически показывает тип эмбеддера (local/external)

### Changed
- `HybridMemoryProvider.__init__()` — выбирает LocalEmbedder или RemoteEmbedder по EMBED_PROVIDER
- `ChromaBackend.__init__()` — принимает любой embedder (не только LocalEmbedder), авто-детект размерности
- `status()` — возвращает `embed_backend` + `embed_provider` вместо хардкода

### Benchmarks (ysh, Debian 13, 20 calls)

| Режим | Latency |
|-------|---------|
| Local (llama-cpp-python, 768d) | 12ms direct / 1ms после прогрева |
| External (LiteLLM → llama-cpp) | 76ms |
| External (LiteLLM → Ollama, 1024d) | 100ms |

### Files
- `~/scripts/hybrid_memory_provider.py` — RemoteEmbedder, EMBED_PROVIDER, авто-коллекции
- `plugin/__init__.py` — dynamic embedder label
- `AGENTS.md` — документация

---

## v1.0.0 — Initial Release (2026-06-13)

- FTS5 + Chroma + MemoryGraph hybrid memory
- LocalEmbedder (llama-cpp GGUF, embeddinggemma-300M, 768d)
- Recency boost on all backends
- SecureStore (age-encrypted secrets)
- Fusion: 0.45×Chroma + 0.25×FTS5 + 0.30×MemoryGraph
- Tools: hybrid_search, hybrid_status, hybrid_secure_get
