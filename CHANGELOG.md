# Changelog

## v1.1.0 — Embedding Provider Switch (2026-06-20)

### Added
- **RemoteEmbedder** — OpenAI-совместимый внешний embedding-провайдер
- **EMBED_PROVIDER** env var: `local` (llama-cpp GGUF, по умолчанию) или `external` (OpenAI API)
- **EMBED_API_URL**, **EMBED_API_KEY**, **EMBED_API_MODEL** — настройки внешнего провайдера
- **Авто-коллекции по размерности** — при смене эмбеддера с 768d на 1024d ChromaBackend создаёт новую коллекцию с суффиксом `_{N}d`

### Changed
- `HybridMemoryProvider.__init__()` — выбирает LocalEmbedder или RemoteEmbedder по EMBED_PROVIDER
- `ChromaBackend.__init__()` — принимает любой embedder (не только LocalEmbedder), авто-детект размерности
- `status()` — возвращает `embed_backend` + `embed_provider` вместо хардкода
- `system_prompt_block()` динамически показывает тип эмбеддера (local/external)

### Usage

```bash
# Local (по умолчанию) — llama-cpp GGUF, 768d
python3 hybrid_memory_provider.py status
# → embed_backend: local (llama-cpp GGUF)

# External через LiteLLM
EMBED_PROVIDER=external \
EMBED_API_URL=http://127.0.0.1:4000/v1 \
EMBED_API_KEY=*** \
EMBED_API_MODEL=embeddinggemma-300M \
python3 hybrid_memory_provider.py status
# → embed_backend: external (http://127.0.0.1:4000/v1, model=embeddinggemma-300M)
```

### Benchmarks

20 последовательных вызовов, ysh (Debian 13, 64 GB RAM).

| Режим | Средняя | Мин | Макс | Dim |
|-------|---------|-----|-----|-----|
| Прямой llama-cpp-python (embed_server :8709) | 12ms | 7ms | 73ms | 768 |
| LiteLLM → llama-cpp (:4000 → :8709) | 76ms | 25ms | 207ms | 768 |
| LiteLLM → Ollama (:4000 → :11434, bge-m3) | 100ms | 83ms | 136ms | 1024 |

Full hybrid search (FTS5 + Chroma + MemoryGraph) после прогрева:

| Режим | Latency | Модель |
|-------|---------|--------|
| Local (llama-cpp-python) | 1ms | embeddinggemma-300M, 768d |
| External (LiteLLM → llama-cpp) | 40ms | embeddinggemma-300M, 768d |
| External (LiteLLM → Ollama) | 60ms | BAAI/bge-m3, 1024d |

Полные замеры: [hermes-office-architecture/BENCHMARKS.md](https://github.com/trifonovhome/hermes-office-architecture/blob/master/BENCHMARKS.md)

### Files
- `~/scripts/hybrid_memory_provider.py` — RemoteEmbedder, EMBED_PROVIDER, авто-коллекции
- `plugin/__init__.py` — dynamic embedder label в system_prompt_block
- `AGENTS.md` — документация по EMBED_PROVIDER переключателю

---

## v1.0.0 — Initial Release (2026-06-13)

- FTS5 + Chroma + MemoryGraph hybrid memory (SQLite-only)
- LocalEmbedder (llama-cpp GGUF, embeddinggemma-300M, 768d)
- Recency boost на всех трёх backend'ах
- SecureStore (age-encrypted secrets)
- Fusion: 0.45×Chroma + 0.25×FTS5 + 0.30×MemoryGraph
- Tools: `hybrid_search`, `hybrid_status`, `hybrid_secure_get`
- Hermes plugin integration
