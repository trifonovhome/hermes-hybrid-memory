     1|# Hermes Hybrid Memory — Спецификация v1.0
     2|
     3|**Дата:** 13 июня 2026
     4|**Хост:** home-server (Linux 6.x, 64G RAM, 1TB NVMe)
     5|**Репо:** [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory)
     6|
     7|---
     8|
     9|## 1. Обзор
    10|
    11|Гибридная память Hermes — это **per-agent** система с 4 бэкендами, работающая внутри
    12|унифицированных Docker-контейнеров. Каждый контейнер содержит и Memory API, и
    13|Hermes Gateway. Один Docker-образ (`Dockerfile.unified`), `AGENT_ID` задаёт
    14|идентичность.
    15|
    16|**Принципы:**
    17|- **Изоляция:** у каждого агента свои данные (FTS5/Chroma/MemoryGraph)
    18|- **Shared pool:** агент-agent-gamma (:8710) — общий пул фактов, доступный всем
    19|- **Share/Broadcast:** прямая peer-to-peer репликация фактов между агентами
    20|- **Unified:** агент + память в одном контейнере, `network_mode:host`
    21|
    22|---
    23|
    24|## 2. Бэкенды (4 уровня)
    25|
    26|### 2.1 FTS5 — Keyword Precision
    27|
    28|| Параметр | Значение |
    29||----------|----------|
    30|| Хранилище | SQLite `/data/memory/fts5/memory.db` |
    31|| Индекс | FTS5 virtual table over `content` |
    32|| Поиск | BM25 keyword matching |
    33|| Латентность | < 1 ms |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) через JOIN facts.created_at |
    34|| Fallback | Multi-word → OR-поиск |
    35|| Таблицы | `facts` (id, content, source, created_at, updated_at) + `facts_fts` (virtual) |
    36|| Сессии | `sessions` + `sessions_fts` (id, title, preview, content, message_count) |
    37|
    38|### 2.2 Chroma + bge-m3 — Semantic Understanding
    39|
    40|| Параметр | Значение |
    41||----------|----------|
    42|| Хранилище | ChromaDB Persistent `/data/memory/chroma/` |
    43|| Эмбеддинг | bge-m3 (1024-мерный), через `LITELLM_URL/v1/embeddings` |
    44|| Метрика | Cosine distance → similarity score |
    45|| Коллекции | `memory_{AGENT_ID}` (факты) + `sessions_{AGENT_ID}` (сессии) |
    46|| Латентность | 50–200 ms (включая embedding round-trip) |
| Хранение timestamp | `created_at` в metadata (ISO 8601) |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) из metadata |
    47|| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |
    48|
    49|### 2.3 Shared Pool — Remote Facts
    50|
    51|| Параметр | Значение |
    52||----------|----------|
    53|| Тип | Удалённый HTTP-эндпоинт |
    54|| Эндпоинт | `SHARED_URL/memory/search` (agent-agent-gamma :8710) |
    55|| Маршрутизация | Все агенты читают; shared сам себя не опрашивает |
    56|| Вес | 0.45 × score (в fusion) |
    57|
    58|### 2.4 MemoryGraph — Graph Relationships
    59|
    60|| Параметр | Значение |
    61||----------|----------|
    62|| Хранилище | SQLite `{MEMORYGRAPH_DIR}/memorygraph_{AGENT_ID}.db` |
    63|| SDK | `memorygraphMCP` v0.12.4 (PyPI: SQLiteMemoryDatabase + SQLiteFallbackBackend) |
    64|| NLP | spaCy `ru_core_news_md` (русский NER) |
    65|| Типы памяти | GENERAL, TASK, WORKFLOW, COMMAND, PROBLEM, PROJECT |
    66|| Поиск | FTS over nodes + per-word fallback с дедупликацией |
    67|| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |
    68|| Вес в fusion | 0.15 + tag_bonus (до 0.05) |
    69|
    70|### 2.5 Ресенси-буст (единая формула)
    71|
    72|```
    73|recency_boost(days):
    74|  today      → 1.0
    75|  1–7 days   → 0.6 + 0.4 × (7 − days) / 7
    76|  8–30 days  → 0.3 + 0.3 × (30 − days) / 23
    77|  31–90 days → 0.05 + 0.25 × (90 − days) / 60
    78|  90+ days   → 0.05
    79|
    80|final_score = similarity_score × (0.7 + 0.3 × recency_boost)
    81|```
    82|
    83|---
    84|
    85|## 3. Архитектура контейнеров
    86|
    87|### 3.1 Docker Compose (`./docker/docker-compose-unified.yml`)
    88|
    89|| Сервис | Контейнер | AGENT_ID | Gateway | Memory | Shared |
    90||--------|-----------|----------|---------|--------|--------|
    91|| agent-agent-alpha | agent-agent-alpha | agent-alpha | :8642 | :8711 | :8710 |
    92|| agent-agent-beta | agent-agent-beta | agent-beta | :8643 | :8712 | :8710 |
    93|| agent-agent-gamma | agent-agent-gamma | agent-gamma | :8647 ✅ | :8710 ✅ (master) | :8710 |
    94|| ~~memory-shared~~ | УДАЛЁН | — | — | — | — |
    95|
    96|**Shared pool** — agent-agent-gamma на :8710, без отдельного контейнера (устранён конфликт портов и data-директорий).
    97|
    98|**Все:** `network_mode:host`, `user:1000:1000`, `restart:unless-stopped`.
    99|
   100|### 3.2 Volume mounts (per-agent)
   101|
   102|| Контейнер | FTS5 | Chroma | MemoryGraph | Профиль |
   103||-----------|------|--------|-------------|---------|
   104|| agent-agent-alpha | `data/agent-alpha/fts5` | `data/agent-alpha/chroma` | `data/agent-alpha/memorygraph` | `./profiles/alpha` |
   105|| agent-agent-beta | `data/agent-beta/fts5` | `data/agent-beta/chroma` | `data/agent-beta/memorygraph` | `./profiles/beta` |
   106|| agent-agent-gamma | `data/shared/fts5` | `data/shared/chroma` | `data/shared/memorygraph` | — |
   107|| memory-shared | `data/shared/fts5` | `data/shared/chroma` | `data/shared/memorygraph` | — |
   108|
   109|### 3.3 LLM-цепочка
   110|
   111|```
   112|TUI (хост) ──→ Hermes Gateway :8642 (Docker) ──→ Headroom :8787 ──→ LiteLLM :4000 ──→ DeepSeek API
   113|                                                                          ↘ Ollama (опционально)
   114|```
   115|
   116|**Memory API использует `LITELLM_URL` для:**
   117|- Эмбеддингов (bge-m3): `{LITELLM_URL}/v1/embeddings`
   118|- Экстракции фактов (deepseek-v4-pro): `{LITELLM_URL}/v1/chat/completions`
   119|
   120|**Текущий LITELLM_URL:** `http://127.0.0.1:8787` (через Headroom).
   121|
   122|### 3.4 Custom-провайдеры TUI (host)
   123|
   124|| Провайдер | base_url | Использование |
   125||-----------|----------|---------------|
   126|| `custom:Headroom (:8787)` | `http://127.0.0.1:8787/v1` | ⭐ Основной |
   127|| `custom:hermes-docker` | `http://127.0.0.1:8642/v1` | Docker gateway напрямую |
   128|| `custom:llm.trifonov.su` | `http://llm.trifonov.su:4000/v1` | LiteLLM напрямую |
   129|| `custom:deepseek-direct` | `https://api.deepseek.com` | Прямой API |
   130|
   131|---
   132|
   133|## 4. REST API эндпоинты
   134|
   135|Все на `127.0.0.1:{MEMORY_PORT}`:
   136|
   137|| Метод | Путь | Описание |
   138||-------|------|----------|
   139|| GET | `/health` | `{"status":"ok","agent":"...","port":...}` |
   140|| GET | `/status` | `{"agent":"...","fts5":N,"chroma":N,"memorygraph":N}` |
   141|| POST | `/memory/search` | Унифицированный поиск (4 бэкенда) |
   142|| POST | `/memory/extract` | LLM-экстракция фактов → хранение во всех бэкендах |
   143|| POST | `/memory/share` | Отправить факт другому агенту |
   144|| POST | `/memory/receive` | Принять факт от другого агента |
   145|| POST | `/memory/broadcast` | Отправить факт всем пирам |
   146|| POST | `/memory/sessions/search` | Поиск по истории чатов (FTS5 + Chroma) |
   147|| POST | `/memory/sessions/import` | Импорт сессии (из Honcho) |
   148|
   149|---
   150|
   151|## 5. Fusion-алгоритм (`unified_search`)
   152|
   153|### 5.1 Этапы
   154|
   155|1. Запрос ко всем 4 бэкендам параллельно (limit × 2 каждый)
   156|2. Дедупликация по нормализованному ключу (alnum, lowercase, 80 символов)
   157|3. Применение fusion-весов
   158|4. Сортировка по fusion score (desc)
   159|5. Обрезка до `limit` результатов
   160|
   161|### 5.2 Веса (текущие, 4-backend)
   162|
   163|| Бэкенд | Вес | Тип |
   164||--------|-----|-----|
   165|| Shared pool | 0.45 × score | Новый факт |
   166|| Chroma | 0.50 × score | Новый факт |
   167|| FTS5 | 0.20 × min(1.0, bm25_norm + 0.2) | Новый ИЛИ буст существующего |
   168|| MemoryGraph | 0.15 + tag_bonus | Новый ИЛИ буст существующего |
   169|
   170|**Бустинг:** если факт уже найден Chroma/Shared, FTS5 добавляет +0.20, MemoryGraph добавляет +0.15 к fusion-счёту (не заменяет).
   171|
   172|### 5.3 Ответ
   173|
   174|```json
   175|{
   176|  "query": "unified Docker containers",
   177|  "results": [
   178|    {
   179|      "content": "...",
   180|      "score": 0.85,
   181|      "fusion": 0.62,
   182|      "backend": "chroma",
   183|      "keyword_match": true,
   184|      "graph_match": false
   185|    }
   186|  ],
   187|  "backends": {
   188|    "fts5": 6,
   189|    "chroma": 3,
   190|    "shared": 1,
   191|    "memorygraph": 4
   192|  }
   193|}
   194|```
   195|
   196|---
   197|
   198|## 6. Environment Variables
   199|
   200|| Переменная | По умолчанию | Описание |
   201||------------|-------------|----------|
   202|| `AGENT_ID` | agent-alpha | Идентификатор агента |
   203|| `MEMORY_PORT` | 8711 | Порт Memory API |
   204|| `AGENT_PORT` | 8642 | Порт Hermes Gateway |
   205|| `LISTEN_HOST` | 127.0.0.1 | Адрес для Memory API |
   206|| `LISTEN_PORT` | 8711 | Альтернативное имя для MEMORY_PORT |
   207|| `LITELLM_URL` | http://127.0.0.1:4000 | URL LiteLLM (эмбеддинги + extraction) |
   208|| `LITELLM_API_KEY` | — | LiteLLM API key |
   209|| `SHARED_URL` | http://127.0.0.1:8710 | Shared pool (agent-agent-gamma) |
   210|| `PEERS` | — | `name:host:port,name:host:port` |
   211|| `EXTRACTION_MODEL` | deepseek-v4-pro | LLM для экстракции фактов |
   212|| `EMBED_MODEL` | bge-m3 | Модель эмбеддингов |
   213|| `FTS5_DB` | /data/fts5/memory.db | Путь к SQLite FTS5 |
   214|| `CHROMA_DIR` | /data/chroma | Путь к ChromaDB |
   215|| `MEMORYGRAPH_DIR` | /data/memorygraph | Путь к MemoryGraph |
   216|| `CHROMA_COLLECTION` | memory_{AGENT_ID} | Имя коллекции Chroma (авто) |
   217|| `CHROMA_SESSIONS` | sessions_{AGENT_ID} | Имя коллекции сессий (авто) |
   218|
   219|---
   220|
   221|## 7. Текущее состояние (13.06.2026 02:30)
   222|
   223|| Агент | Gateway | Memory | FTS5 | Chroma | MemoryGraph | Статус |
   224||-------|---------|--------|------|--------|-------------|--------|
   225|| agent-agent-alpha | :8642 ✅ | :8711 ✅ | 51 | 84 | 51 | Up |
   226|| agent-agent-beta | :8643 ✅ | :8712 ✅ | 2 | 2 | 1 | Up |
   227|| agent-agent-gamma | :8647 ✅ | :8710 ✅ | 1 | 1 | 1 | Up |
   228|
   229|**Все бэкенды работают.** Shared pool = agent-agent-gamma. Share/broadcast agent-alpha→agent-beta работает.
   230|agent-delta/agent-epsilon/agent-zeta не запущены (Connection refused).
   231|
   232|**LLM-цепочка:** Headroom (:8787) ✅, LiteLLM (:4000) ✅, DeepSeek API ✅.
   233|
   234|**Hermes plugin:** `memory.provider=hybrid`, инструменты `hybrid_search`/`hybrid_status`
   235|активны. ⚠️ `hybrid_status` показывает старую standalone Chroma
   236|(`~/projects/chroma_direct`, 214 фактов) — не ту, что в Docker-контейнерах.
   237|
   238|---
   239|
   240|## 8. Pitfalls (из опыта)
   241|
   242|### 8.1 Docker
   243|- **UID mismatch:** volume-директории, созданные с другим UID → «readonly database»
   244|  → фикс: `chown -R 1000:1000` через alpine
   245|- **Build cache:** Dockerfile изменения не подхватываются без `--no-cache`
   246|- **Symlink confusion:** `.` → симлинк, `patch`/`write_file`
   247|  могут писать не туда
   248|
   249|### 8.2 MemoryGraph
   250|- **Multi-word search:** SDK `search_memories(match_mode="any")` возвращает 0 —
   251|  нужен per-word fallback с дедупликацией
   252|- **`recency_boost()` должен быть определён ДО `memorygraph_search()`** в файле
   253|- **`/status` count:** использовать `get_memory_statistics()`, не `len(_mg_db[...])`
   254|- **`MEMORYGRAPH_DIR` обязателен** в docker-compose env (не дефолтный `/data/memorygraph`)
   255|
   256|### 8.3 FTS5
   257|- **Директория не создаётся:** `_init_fts5()` вызывает `os.makedirs()`, но только
   258|  при импорте — `_ensure_fts5()` перед каждой операцией
   259|
   260|### 8.4 Shared pool
   261|- **agent-agent-gamma + memory-shared конфликт:** оба мапят одни `data/shared/` директории
   262|  → Chroma lock / SQLite lock → один падает
   263|- **Решение (применено):** удалить `memory-shared`, agent-agent-gamma сам обслуживает :8710
   264|- **Код читает `LISTEN_PORT`, не `MEMORY_PORT`** — `MEMORY_PORT` в docker-compose
   265|  игнорируется для фактического bind. Нужно либо `LISTEN_PORT`, либо патч кода.
   266|
   267|### 8.5 MemoryGraph integer overflow
   268|- **Симптом:** `SearchQuery limit=8589934592` → Pydantic validation error
   269|- **Причина:** неограниченный `limit * 2` при передаче между unified_search → memorygraph_search
   270|- **Фикс:** `limit = min(max(1, limit), 100)` в `memorygraph_search` и `unified_search`
   271|
   272|### 8.6 hybrid_search из хоста
   273|- **Stale Chroma:** `hybrid_status` может показывать `~/projects/chroma_direct`
   274|  вместо контейнерной Chroma — проверять `chroma_dir` в ответе
   275|
   276|---
   277|
   278|## 9. Файлы и пути
   279|
   280|| Что | Где |
   281||-----|-----|
   282|| Исходный код | `./agent/hybrid_memory_agent.py` |
   283|| Build context | `./docker/` |
   284|| Dockerfile | `./docker/Dockerfile.unified` |
   285|| Docker Compose | `./docker/docker-compose-unified.yml` |
   286|| Data (agent-alpha) | `./docker/data/agent-alpha/{fts5,chroma,memorygraph}/` |
   287|| Data (agent-beta) | `./docker/data/agent-beta/{fts5,chroma,memorygraph}/` |
   288|| Data (shared) | `./docker/data/shared/{fts5,chroma,memorygraph}/` |
   289|| Профиль Hermes (agent-alpha) | `./profiles/alpha/config.yaml` |
   290|| GitHub | [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory) |
   291|
   292|---
   293|
   294|## 10. Диагностика (быстрые команды)
   295|
   296|```bash
   297|# Статус контейнеров
   298|docker ps --filter "name=agent-" --format "table {{.Names}}\t{{.Status}}"
   299|
   300|# Порты
   301|