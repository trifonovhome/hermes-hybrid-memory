# Hermes Hybrid Memory — Спецификация v1.0

**Дата:** 13 июня 2026
**Хост:** home-server (Linux 6.x, 64G RAM, 1TB NVMe)
**Репо:** [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory)

---

## 1. Обзор

Гибридная память Hermes — это **per-agent** система с 4 бэкендами, работающая внутри
унифицированных Docker-контейнеров. Каждый контейнер содержит и Memory API, и
Hermes Gateway. Один Docker-образ (`Dockerfile.unified`), `AGENT_ID` задаёт
идентичность.

**Принципы:**
- **Изоляция:** у каждого агента свои данные (FTS5/Chroma/MemoryGraph)
- **Shared pool:** агент-agent-gamma (:8710) — общий пул фактов, доступный всем
- **Share/Broadcast:** прямая peer-to-peer репликация фактов между агентами
- **Unified:** агент + память в одном контейнере, `network_mode:host`

---

## 2. Бэкенды (4 уровня)

### 2.1 FTS5 — Keyword Precision

| Параметр | Значение |
|----------|----------|
| Хранилище | SQLite `/data/memory/fts5/memory.db` |
| Индекс | FTS5 virtual table over `content` |
| Поиск | BM25 keyword matching |
| Латентность | < 1 ms |
| Fallback | Multi-word → OR-поиск |
| Таблицы | `facts` (id, content, source, created_at, updated_at) + `facts_fts` (virtual) |
| Сессии | `sessions` + `sessions_fts` (id, title, preview, content, message_count) |

### 2.2 Chroma + bge-m3 — Semantic Understanding

| Параметр | Значение |
|----------|----------|
| Хранилище | ChromaDB Persistent `/data/memory/chroma/` |
| Эмбеддинг | bge-m3 (1024-мерный), через `LITELLM_URL/v1/embeddings` |
| Метрика | Cosine distance → similarity score |
| Коллекции | `memory_{AGENT_ID}` (факты) + `sessions_{AGENT_ID}` (сессии) |
| Латентность | 50–200 ms (включая embedding round-trip) |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |

### 2.3 Shared Pool — Remote Facts

| Параметр | Значение |
|----------|----------|
| Тип | Удалённый HTTP-эндпоинт |
| Эндпоинт | `SHARED_URL/memory/search` (agent-agent-gamma :8710) |
| Маршрутизация | Все агенты читают; shared сам себя не опрашивает |
| Вес | 0.45 × score (в fusion) |

### 2.4 MemoryGraph — Graph Relationships

| Параметр | Значение |
|----------|----------|
| Хранилище | SQLite `{MEMORYGRAPH_DIR}/memorygraph_{AGENT_ID}.db` |
| SDK | `memorygraphMCP` v0.12.4 (PyPI: SQLiteMemoryDatabase + SQLiteFallbackBackend) |
| NLP | spaCy `ru_core_news_md` (русский NER) |
| Типы памяти | GENERAL, TASK, WORKFLOW, COMMAND, PROBLEM, PROJECT |
| Поиск | FTS over nodes + per-word fallback с дедупликацией |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |
| Вес в fusion | 0.15 + tag_bonus (до 0.05) |

### 2.5 Ресенси-буст (единая формула)

```
recency_boost(days):
 today      → 1.0
 1–7 days   → 0.6 + 0.4 × (7 − days) / 7
 8–30 days  → 0.3 + 0.3 × (30 − days) / 23
 31–90 days → 0.05 + 0.25 × (90 − days) / 60
 90+ days   → 0.05

final_score = similarity_score × (0.7 + 0.3 × recency_boost)
```

---

## 3. Архитектура контейнеров

### 3.1 Docker Compose (`./docker/docker-compose-unified.yml`)

| Сервис | Контейнер | AGENT_ID | Gateway | Memory | Shared |
|--------|-----------|----------|---------|--------|--------|
| agent-agent-alpha | agent-agent-alpha | agent-alpha | :8642 | :8711 | :8710 |
| agent-agent-beta | agent-agent-beta | agent-beta | :8643 | :8712 | :8710 |
| agent-agent-gamma | agent-agent-gamma | agent-gamma | :8647 ✅ | :8710 ✅ (master) | :8710 |
| ~~memory-shared~~ | УДАЛЁН | — | — | — | — |

**Shared pool** — agent-agent-gamma на :8710, без отдельного контейнера (устранён конфликт портов и data-директорий).

**Все:** `network_mode:host`, `user:1000:1000`, `restart:unless-stopped`.

### 3.2 Volume mounts (per-agent)

| Контейнер | FTS5 | Chroma | MemoryGraph | Профиль |
|-----------|------|--------|-------------|---------|
| agent-agent-alpha | `data/agent-alpha/fts5` | `data/agent-alpha/chroma` | `data/agent-alpha/memorygraph` | `./profiles/alpha` |
| agent-agent-beta | `data/agent-beta/fts5` | `data/agent-beta/chroma` | `data/agent-beta/memorygraph` | `./profiles/beta` |
| agent-agent-gamma | `data/shared/fts5` | `data/shared/chroma` | `data/shared/memorygraph` | — |
| memory-shared | `data/shared/fts5` | `data/shared/chroma` | `data/shared/memorygraph` | — |

### 3.3 LLM-цепочка

```
TUI (хост) ──→ Hermes Gateway :8642 (Docker) ──→ Headroom :8787 ──→ LiteLLM :4000 ──→ DeepSeek API
                                                                         ↘ Ollama (опционально)
```

**Memory API использует `LITELLM_URL` для:**
- Эмбеддингов (bge-m3): `{LITELLM_URL}/v1/embeddings`
- Экстракции фактов (deepseek-v4-pro): `{LITELLM_URL}/v1/chat/completions`

**Текущий LITELLM_URL:** `http://127.0.0.1:8787` (через Headroom).

### 3.4 Custom-провайдеры TUI (host)

| Провайдер | base_url | Использование |
|-----------|----------|---------------|
| `custom:Headroom (:8787)` | `http://127.0.0.1:8787/v1` | ⭐ Основной |
| `custom:hermes-docker` | `http://127.0.0.1:8642/v1` | Docker gateway напрямую |
| `custom:llm.trifonov.su` | `http://llm.trifonov.su:4000/v1` | LiteLLM напрямую |
| `custom:deepseek-direct` | `https://api.deepseek.com` | Прямой API |

---

## 4. REST API эндпоинты

Все на `127.0.0.1:{MEMORY_PORT}`:

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | `{"status":"ok","agent":"...","port":...}` |
| GET | `/status` | `{"agent":"...","fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Унифицированный поиск (4 бэкенда) |
| POST | `/memory/extract` | LLM-экстракция фактов → хранение во всех бэкендах |
| POST | `/memory/share` | Отправить факт другому агенту |
| POST | `/memory/receive` | Принять факт от другого агента |
| POST | `/memory/broadcast` | Отправить факт всем пирам |
| POST | `/memory/sessions/search` | Поиск по истории чатов (FTS5 + Chroma) |
| POST | `/memory/sessions/import` | Импорт сессии (из Honcho) |

---

## 5. Fusion-алгоритм (`unified_search`)

### 5.1 Этапы

1. Запрос ко всем 4 бэкендам параллельно (limit × 2 каждый)
2. Дедупликация по нормализованному ключу (alnum, lowercase, 80 символов)
3. Применение fusion-весов
4. Сортировка по fusion score (desc)
5. Обрезка до `limit` результатов

### 5.2 Веса (текущие, 4-backend)

| Бэкенд | Вес | Тип |
|--------|-----|-----|
| Shared pool | 0.45 × score | Новый факт |
| Chroma | 0.50 × score | Новый факт |
| FTS5 | 0.20 × min(1.0, bm25_norm + 0.2) | Новый ИЛИ буст существующего |
| MemoryGraph | 0.15 + tag_bonus | Новый ИЛИ буст существующего |

**Бустинг:** если факт уже найден Chroma/Shared, FTS5 добавляет +0.20, MemoryGraph добавляет +0.15 к fusion-счёту (не заменяет).

### 5.3 Ответ

```json
{
 "query": "unified Docker containers",
 "results": [
   {
     "content": "...",
     "score": 0.85,
     "fusion": 0.62,
     "backend": "chroma",
     "keyword_match": true,
     "graph_match": false
   }
 ],
 "backends": {
   "fts5": 6,
   "chroma": 3,
   "shared": 1,
   "memorygraph": 4
 }
}
```

---

## 6. Environment Variables

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `AGENT_ID` | agent-alpha | Идентификатор агента |
| `MEMORY_PORT` | 8711 | Порт Memory API |
| `AGENT_PORT` | 8642 | Порт Hermes Gateway |
| `LISTEN_HOST` | 127.0.0.1 | Адрес для Memory API |
| `LISTEN_PORT` | 8711 | Альтернативное имя для MEMORY_PORT |
| `LITELLM_URL` | http://127.0.0.1:4000 | URL LiteLLM (эмбеддинги + extraction) |
| `LITELLM_API_KEY` | — | LiteLLM API key |
| `SHARED_URL` | http://127.0.0.1:8710 | Shared pool (agent-agent-gamma) |
| `PEERS` | — | `name:host:port,name:host:port` |
| `EXTRACTION_MODEL` | deepseek-v4-pro | LLM для экстракции фактов |
| `EMBED_MODEL` | bge-m3 | Модель эмбеддингов |
| `FTS5_DB` | /data/fts5/memory.db | Путь к SQLite FTS5 |
| `CHROMA_DIR` | /data/chroma | Путь к ChromaDB |
| `MEMORYGRAPH_DIR` | /data/memorygraph | Путь к MemoryGraph |
| `CHROMA_COLLECTION` | memory_{AGENT_ID} | Имя коллекции Chroma (авто) |
| `CHROMA_SESSIONS` | sessions_{AGENT_ID} | Имя коллекции сессий (авто) |

---

## 7. Текущее состояние (13.06.2026 02:30)

| Агент | Gateway | Memory | FTS5 | Chroma | MemoryGraph | Статус |
|-------|---------|--------|------|--------|-------------|--------|
| agent-agent-alpha | :8642 ✅ | :8711 ✅ | 51 | 84 | 51 | Up |
| agent-agent-beta | :8643 ✅ | :8712 ✅ | 2 | 2 | 1 | Up |
| agent-agent-gamma | :8647 ✅ | :8710 ✅ | 1 | 1 | 1 | Up |

**Все бэкенды работают.** Shared pool = agent-agent-gamma. Share/broadcast agent-alpha→agent-beta работает.
agent-delta/agent-epsilon/agent-zeta не запущены (Connection refused).

**LLM-цепочка:** Headroom (:8787) ✅, LiteLLM (:4000) ✅, DeepSeek API ✅.

**Hermes plugin:** `memory.provider=hybrid`, инструменты `hybrid_search`/`hybrid_status`
активны. ⚠️ `hybrid_status` показывает старую standalone Chroma
(`~/projects/chroma_direct`, 214 фактов) — не ту, что в Docker-контейнерах.

---

## 8. Pitfalls (из опыта)

### 8.1 Docker
- **UID mismatch:** volume-директории, созданные с другим UID → «readonly database»
 → фикс: `chown -R 1000:1000` через alpine
- **Build cache:** Dockerfile изменения не подхватываются без `--no-cache`
- **Symlink confusion:** `.` → симлинк, `patch`/`write_file`
 могут писать не туда

### 8.2 MemoryGraph
- **Multi-word search:** SDK `search_memories(match_mode="any")` возвращает 0 —
 нужен per-word fallback с дедупликацией
- **`recency_boost()` должен быть определён ДО `memorygraph_search()`** в файле
- **`/status` count:** использовать `get_memory_statistics()`, не `len(_mg_db[...])`
- **`MEMORYGRAPH_DIR` обязателен** в docker-compose env (не дефолтный `/data/memorygraph`)

### 8.3 FTS5
- **Директория не создаётся:** `_init_fts5()` вызывает `os.makedirs()`, но только
 при импорте — `_ensure_fts5()` перед каждой операцией

### 8.4 Shared pool
- **agent-agent-gamma + memory-shared конфликт:** оба мапят одни `data/shared/` директории
 → Chroma lock / SQLite lock → один падает
- **Решение (применено):** удалить `memory-shared`, agent-agent-gamma сам обслуживает :8710
- **Код читает `LISTEN_PORT`, не `MEMORY_PORT`** — `MEMORY_PORT` в docker-compose
 игнорируется для фактического bind. Нужно либо `LISTEN_PORT`, либо патч кода.

### 8.5 MemoryGraph integer overflow
- **Симптом:** `SearchQuery limit=8589934592` → Pydantic validation error
- **Причина:** неограниченный `limit * 2` при передаче между unified_search → memorygraph_search
- **Фикс:** `limit = min(max(1, limit), 100)` в `memorygraph_search` и `unified_search`

### 8.6 hybrid_search из хоста
- **Stale Chroma:** `hybrid_status` может показывать `~/projects/chroma_direct`
 вместо контейнерной Chroma — проверять `chroma_dir` в ответе

---

## 9. Файлы и пути

| Что | Где |
|-----|-----|
| Исходный код | `./agent/hybrid_memory_agent.py` |
| Build context | `./docker/` |
| Dockerfile | `./docker/Dockerfile.unified` |
| Docker Compose | `./docker/docker-compose-unified.yml` |
| Data (agent-alpha) | `./docker/data/agent-alpha/{fts5,chroma,memorygraph}/` |
| Data (agent-beta) | `./docker/data/agent-beta/{fts5,chroma,memorygraph}/` |
| Data (shared) | `./docker/data/shared/{fts5,chroma,memorygraph}/` |
| Профиль Hermes (agent-alpha) | `./profiles/alpha/config.yaml` |
| GitHub | [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory) |

---

## 10. Диагностика (быстрые команды)

```bash
# Статус контейнеров
docker ps --filter "name=agent-" --format "table {{.Names}}\t{{.Status}}"

# Порты
ss -tlnp | grep -E '864[237]|871[012]'

# Статус памяти каждого агента
for port in 8710 8711 8712; do
 echo "=== :$port ==="
 curl -s http://127.0.0.1:$port/status | python3 -m json.tool
done

# Поиск
curl -s -X POST http://127.0.0.1:8711/memory/search \
 -H 'Content-Type: application/json' \
 -d '{"query":"Docker unified containers","limit":3}' | python3 -m json.tool

# LLM-экстракция
curl -s -X POST http://127.0.0.1:8711/memory/extract \
 -H 'Content-Type: application/json' \
 -d '{"text":"Сегодня настроили Headroom proxy на порту 8787"}' | python3 -m json.tool
```
