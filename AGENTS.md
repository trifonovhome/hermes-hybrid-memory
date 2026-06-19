# AGENTS.md — Инструкция для AI-агентов по обновлению памяти

> Версия: 2026-06-19

## Что изменилось

| Компонент | Было | Стало |
|-----------|------|-------|
| **MemoryGraph** | Только в Docker-контейнере | + в хост-плагине (прямой SQLite) |
| **Эмбеддинги** | bge-m3 (1024d) через LiteLLM | embeddinggemma-300M (768d) локально через llama-cpp |
| **Chroma коллекция** | `hermes_memory_bge_m3` | `memory_{AGENT_ID}` (авто, общая с Docker) |
| **Recency boost** | Только Docker | Все 3 бэкенда (FTS5, Chroma, MemoryGraph) |
| **Fusion веса** | 0.75×Chroma + 0.25×FTS5 | 0.45×Chroma + 0.25×FTS5 + 0.30×MemoryGraph |

## 1. Хост-провайдер (hybrid_memory_provider.py)

Файл: `~/scripts/hybrid_memory_provider.py`

### Если у тебя его нет — создай:

```bash
cp ~/scripts/hybrid_memory_provider.py ~/scripts/hybrid_memory_provider.py.bak
```

Убедись что в нём есть:
- `LocalEmbedder` (llama-cpp, GGUF, 768d) вместо `LiteLLMEmbedder`
- `MemoryGraphBackend` (прямой SQLite)
- `recency_boost()` функция
- `COLLECTION_NAME = "memory_{AGENT_ID}"` — совпадает с Docker-контейнером
- `LOCAL_EMBED_MODEL_PATH` указывает на `embeddinggemma-300M-Q8_0.gguf`
- Fusion: 0.45 chroma + 0.25 fts5 + 0.30 memorygraph
- Recency boost: `score × (0.7 + 0.3 × boost)` во всех бэкендах

### Проверка:

```bash
python3 ~/scripts/hybrid_memory_provider.py status
# Должен показать:
#   Embeddings: embeddinggemma-300M (GGUF, 768d) via llama-cpp
#   Chroma count: N (768d)
#   MemoryGraph count: N

python3 ~/scripts/hybrid_memory_provider.py search "тестовый запрос"
# Должен показать результаты из 3 бэкендов: fts5, chroma_768d, memorygraph
```

## 2. Hermes Plugin

Файл: `plugin/__init__.py` в репозитории

### Что должно быть:

1. `system_prompt_block()` упоминает 3 бэкенда:
```python
"- FTS5 (keyword precision) — N deduped facts from hermes-local-memory\n"
"- Chroma + embeddinggemma-300M (semantic, 768d, local GGUF) — N facts\n"
f"- MemoryGraph (graph relationships) — {mg_count} nodes via SQLite\n"
```

2. `SEARCH_SCHEMA` описывает 3 бэкенда

3. `HybridMemoryProvider.__init__()` использует `LocalEmbedder` + `MemoryGraphBackend`

### Установка:

```bash
cd ~/projects/hermes-hybrid-memory
git pull origin master

# Скопировать в Hermes (если плагин не встроен)
cp plugin/__init__.py ~/.hermes/profiles/<agent>/plugins/memory/hybrid/__init__.py

# Перезапустить Hermes
hermes restart  # или перезапустить Docker-контейнер
```

## 3. Docker-контейнеры

Контейнеры уже используют:
- Локальные эмбеддинги (`LOCAL_EMBED_MODEL=/data/models/embeddinggemma-300M-Q8_0.gguf`)
- MemoryGraph в `unified_search()`
- Recency boost во всех бэкендах

Если контейнер пересобирается — убедись что `docker-compose.yml` содержит:
```yaml
environment:
  - LOCAL_EMBED_MODEL=/data/models/embeddinggemma-300M-Q8_0.gguf
volumes:
  - ./data/models:/data/models:ro
```

## 4. Recency Boost (Timestamps)

Все факты хранятся с меткой времени `created_at`. При поиске применяется буст:

```python
def recency_boost(created_str: str) -> float:
    today     → 1.00
    1-7 days  → 1.00 → 0.60  (линейно)
    8-30 days → 0.60 → 0.30  (линейно)
    31-90 days → 0.30 → 0.05 (линейно)
    90+ days  → 0.05

applied_score = base_score × (0.7 + 0.3 × boost)
```

**Где хранятся таймстемпы:**

| Backend | Поле | Формат |
|---------|------|--------|
| FTS5 | `facts.created_at` (TEXT) | `2026-06-14T18:32:08.199747+00:00` |
| Chroma | `metadatas["created_at"]` | `2026-06-14T18:27:05.737907Z` |
| MemoryGraph | `nodes.created_at` (TIMESTAMP) | `2026-06-12 20:25:42` |

При переиндексации (FTS5→Chroma) таймстемпы переносятся атомарно — старым
фактам не назначается текущая дата.

Коэффициент 0.7 гарантирует что даже очень старые факты (>90 дней) сохраняют
71.5% своего базового веса. Память никогда не «забывает» полностью.

## 5. Проверка после обновления

```bash
# 1. Проверить статус
python3 ~/scripts/hybrid_memory_provider.py status

# 2. Поискать — должны быть все 3 бэкенда
python3 ~/scripts/hybrid_memory_provider.py search "docker"

# 3. В Hermes — вызвать hybrid_status
# (должен показать memorygraph_count > 0 и embed_backend = llama-cpp)

# 4. В Hermes — вызвать hybrid_search
# (результаты должны включать backend=memorygraph и backend=chroma_768d)
```

## 6. Файлы, затронутые изменениями

| Файл | Где |
|------|-----|
| `~/scripts/hybrid_memory_provider.py` | Хост-скрипт (не в git) |
| `plugin/__init__.py` | `github.com/trifonovhome/hermes-hybrid-memory` |
| `agent/hybrid_memory_agent.py` | `github.com/trifonovhome/hermes-hybrid-memory` |
| `docker-compose.yml` | `~/infra/docker/hermes-hybrid-memory/` (локальный) |
