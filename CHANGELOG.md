# Changelog

## v1.2.1 — Public/Home repo split (2026-06-22)

### Changed
- **Выделен публичный репо** — убраны home-agent фичи: `llm_proxy.py`, `/admin/key/*`, `entrypoint-home.sh`, `AGENT_ROLE`
- **hermes-hybrid-memory** теперь office-ready: LiteLLM или прямой API, без кастомного прокси
- **hermes-hybrid-memory-home** (приватный) — забрал home-agent key distribution и свой LLM-прокси

### Removed
- `agent/llm_proxy.py` — кастомный LLM-прокси (→ home repo)
- `docker/entrypoint-home.sh` — home-agent entrypoint (→ home repo)
- `docker/llm_proxy.py` — дубликат
- `/admin/key/request`, `/admin/key/spend` — key distribution (→ home repo)
- `AGENT_ROLE`, `AGENT_AUTH_KEY`, `HOME_AGENT_SECRET` — home-specific env vars

---

## v1.2.0 — Docker Plugin Fix + Path Auto-Detection (2026-06-20)

### Fixed
- **Docker plugin discovery** — добавлен `plugin.yaml` в контейнер, плагин наследует `MemoryProvider`
- **queue_prefetch** — Docker HTTP-плагин теперь реально вызывает Memory API и кеширует контекст
- **hybrid_secure_get** — добавлен в Docker HTTP-плагин (ранее был только в host-плагине)
- **Жёсткие пути `/home/andreitrifonov/`** — заменены на авто-детект в `hybrid_memory_provider.py` и host-плагине
- **HERMES_HOME** — документировано требование `HERMES_HOME=/home/hermes/.hermes/profile` для Docker-контейнеров
- **entrypoint curl timeout** — добавлен `--connect-timeout 3 --max-time 10` чтобы не виснуть при недоступном home-агенте

### Docker Deployment Requirements
Для работы hybrid memory в Docker-контейнере нужны **три обязательных условия**:
1. `plugin.yaml` — смонтирован в `/usr/local/lib/python3.12/site-packages/plugins/memory/hybrid/plugin.yaml`
2. `class HybridMemoryProvider(MemoryProvider)` — наследование от `MemoryProvider`
3. `HERMES_HOME=/home/hermes/.hermes/profile` — иначе `load_config()` не видит `provider: hybrid`

### Files
- `plugin/hybrid/__init__.py` — Docker HTTP plugin: MemoryProvider inheritance, queue_prefetch, hybrid_secure_get
- `plugin/hybrid/plugin.yaml` — plugin discovery manifest
- `scripts/hybrid_memory_provider.py` — host provider: path auto-detection
- `docker-compose.yml` — mounts for plugin.yaml, HERMES_HOME env var

---

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
