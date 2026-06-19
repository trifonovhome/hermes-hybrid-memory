# Hermes Hybrid Memory — Спецификация v1.2

[🇬🇧 English version](SPECIFICATION.md)

**Дата:** 19 июня 2026
**Хост:** home-server (Linux 6.x, 64G RAM, 1TB NVMe)
**Репо:** [github.com/trifonovhome/hermes-hybrid-memory](https://github.com/trifonovhome/hermes-hybrid-memory)

---

## 1. Обзор

Гибридная память Hermes — это **per-agent** система с 3 бэкендами, работающая внутри
унифицированных Docker-контейнеров. Каждый контейнер содержит и Memory API, и
Hermes Gateway. Один Docker-образ, `AGENT_ID` задаёт идентичность.

**Принципы:**
- **Изоляция:** у каждого агента свои данные (FTS5/Chroma/MemoryGraph)
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

### 2.2 Chroma — Семантический поиск

| Параметр | Значение |
|----------|----------|
| Хранилище | ChromaDB Persistent `/data/chroma/` |
| Эмбеддинг | embeddinggemma-300M-Q8_0 (768d), через llama-cpp-python |
| Метрика | Cosine distance → similarity score |
| Коллекции | `memory_{AGENT_ID}` + `sessions_{AGENT_ID}` |
| Латентность | 100–500 ms (CPU, 300M параметров) |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |

### 2.3 MemoryGraph — Graph Relationships

| Параметр | Значение |
|----------|----------|
| Хранилище | SQLite `{MEMORYGRAPH_DIR}/memorygraph_{AGENT_ID}.db` |
| SDK | `memorygraphMCP` v0.12.4 (PyPI: SQLiteMemoryDatabase + SQLiteFallbackBackend) |
| NLP | spaCy `ru_core_news_md` (русский NER) |
| Типы памяти | GENERAL, TASK, WORKFLOW, COMMAND, PROBLEM, PROJECT |
| Поиск | FTS over nodes + per-word fallback с дедупликацией |
| Ресенси-буст | × (0.7 + 0.3 × recency_boost(created_at)) |
| Вес в fusion | 0.30 + tag_bonus (до 0.05) |

### 2.4 SecureStore — Шифрованные секреты

| Параметр | Значение |
|----------|----------|
| Хранилище | Age-шифрованный файл `/data/secrets/secrets.enc` |
| Ключ | `AGE_KEY` (env var) — age secret key |
| Шифрование | age (rage) — X25519 + ChaCha20-Poly1305 |
| Формат | Строки `key=value`, по одной на строку |
| API | `GET/POST/DELETE /memory/secrets` и `GET /memory/secrets/{key}` |
| Использование | HA-токены, SSH-пароли, API-ключи |
| Статус | Количество ключей видно в `/status` (`secrets` поле) |

**Без `AGE_KEY` SecureStore не активен** — все запросы к `/memory/secrets` возвращают 503.

**Генерация ключа:**

```bash
# Сгенерировать пару age-ключей
age-keygen -o key.txt

# Извлечь секретный ключ (использовать как AGE_KEY)
grep "AGE-SECRET-KEY-" key.txt

# Пример вывода:
# AGE-SECRET-KEY-1QV7LZ2...3XYZ
```

**Как работает:**

1. При запуске, если `AGE_KEY` задан и файл секретов существует — содержимое расшифровывается в память
2. Все операции читают/пишут словарь в памяти; `set()` и `delete()` вызывают атомарное перешифрование
3. Формат файла: простые строки `key=value` внутри age-шифрованной оболочки
4. Комментарии (`#`) игнорируются при расшифровке

**Примеры API:**

```bash
# Сохранить секрет
curl -X POST http://127.0.0.1:8711/memory/secrets \
  -H 'Content-Type: application/json' \
  -d '{"key":"ha_token","value":"eyJhbGciOiJIUzI1NiIs..."}'

# Прочитать секрет
curl http://127.0.0.1:8711/memory/secrets/ha_token
# → {"key":"ha_token","value":"eyJhbGciOiJIUzI1NiIs..."}

# Список всех ключей (значения скрыты)
curl http://127.0.0.1:8711/memory/secrets
# → {"keys":["ha_token"],"agent":"andrei"}

# Удалить секрет
curl -X DELETE http://127.0.0.1:8711/memory/secrets/ha_token
```

**Свойства безопасности:**

- Секреты никогда не записываются на диск в открытом виде — всегда age-шифрованы
- Расшифрованное содержимое живёт только в памяти
- Права на файл зависят от UID контейнера (по умолчанию 1000:1000)
- Атомарная запись: весь файл перезаписывается при каждом `set()`/`delete()`
- Потеря `AGE_KEY` = потеря секретов (восстановление невозможно)

### 2.5 Recency Boost (Timestamps)

Все факты во всех бэкендах хранятся с меткой времени создания. При поиске
применяется буст свежести — недавние факты получают приоритет над старыми.

**Формула:**

```
recency_boost(created_at):
  today      → 1.00
  1-7 days   → 0.60 + 0.40 × (7 − days) / 7
  8-30 days  → 0.30 + 0.30 × (30 − days) / 23
  31-90 days → 0.05 + 0.25 × (90 − days) / 60
  90+ days   → 0.05

final_score = base_score × (0.7 + 0.3 × recency_boost(created_at))
```

Множитель к скору по дням:

| Возраст | boost | multiplier | Эффект |
|---------|-------|-----------|--------|
| Сегодня | 1.00 | ×1.00 | Полный вес |
| 3 дня | 0.83 | ×0.95 | −5% |
| 7 дней | 0.60 | ×0.88 | −12% |
| 14 дней | 0.47 | ×0.84 | −16% |
| 30 дней | 0.30 | ×0.79 | −21% |
| 60 дней | 0.16 | ×0.75 | −25% |
| 90+ дней | 0.05 | ×0.715 | −28.5% |

**Где хранятся таймстемпы:**

| Backend | Поле | Формат | Запись |
|---------|------|--------|--------|
| FTS5 | `facts.created_at` (TEXT) | `2026-06-14T18:32:08.199747+00:00` | При `fts5_store()` |
| Chroma | `metadatas["created_at"]` | `2026-06-14T18:27:05.737907Z` | При `chroma_store()` |
| MemoryGraph | `nodes.created_at` (TIMESTAMP) | `2026-06-12 20:25:42` | При `memorygraph_store()` |

**Recency boost при переиндексации:** при переносе фактов из FTS5 в Chroma
таймстемпы переносятся атомарно — старым фактам не назначается текущая дата.
Благодаря этому факты, созданные неделю назад, корректно получают буст 0.88,
а не 1.0.

**Почему 0.7 в формуле:** коэффициент 0.7 гарантирует что даже очень старые
факты (>90 дней) сохраняют 71.5% своего базового веса. Память никогда не
«забывает» полностью — только затухает.

---

## 3. Архитектура контейнеров

### 3.1 Docker Compose (`./docker/docker-compose-unified.yml`)

| Сервис | Контейнер | AGENT_ID | Gateway | Memory |
|--------|-----------|----------|---------|--------|
| agent-agent-alpha | agent-agent-alpha | agent-alpha | :8642 | :8711 |
| agent-agent-beta | agent-agent-beta | agent-beta | :8643 | :8712 |

**Все:** `network_mode:host`, `user:1000:1000`, `restart:unless-stopped`.

### 3.2 Volume mounts (per-agent)

| Контейнер | FTS5 | Chroma | MemoryGraph | Профиль |
|-----------|------|--------|-------------|---------|
| agent-agent-alpha | `data/agent-alpha/fts5` | `data/agent-alpha/chroma` | `data/agent-alpha/memorygraph` | `./profiles/alpha` |
| agent-agent-beta | `data/agent-beta/fts5` | `data/agent-beta/chroma` | `data/agent-beta/memorygraph` | `./profiles/beta` |

### 3.3 LLM-цепочка

```
TUI (хост) ──→ Hermes Gateway :8642 (Docker) ──→ DeepSeek API
```

Memory API использует DeepSeek API для экстракции фактов. Эмбеддинги — локальные (llama-cpp).

---

## 4. REST API эндпоинты

Все на `127.0.0.1:{MEMORY_PORT}`:

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | `{"status":"ok","agent":"...","port":...}` |
| GET | `/status` | `{"agent":"...","fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Унифицированный поиск (3 бэкенда) |
| POST | `/memory/extract` | LLM-экстракция фактов → хранение во всех бэкендах |
| POST | `/memory/sessions/search` | Поиск по истории чатов (FTS5 + Chroma) |
| POST | `/memory/sessions/import` | Импорт сессии |
| GET | `/memory/secrets` | Список ключей SecureStore |
| GET | `/memory/secrets/{key}` | Значение секрета |
| POST | `/memory/secrets` | Сохранить `{"key":"...","value":"..."}` |
| DELETE | `/memory/secrets/{key}` | Удалить секрет |

---

## 5. Fusion-алгоритм (`unified_search`)

### 5.1 Этапы

1. Запрос ко всем 3 бэкендам параллельно (limit × 2 каждый)
2. Дедупликация по нормализованному ключу (alnum, lowercase, 80 символов)
3. Применение fusion-весов
4. Сортировка по fusion score (desc)
5. Обрезка до `limit` результатов

### 5.2 Веса (текущие, 3-backend)

| Бэкенд | Вес | Тип |
|--------|-----|-----|
| Chroma | 0.45 × score | Новый факт |
| FTS5 | 0.25 × min(1.0, bm25_norm + 0.2) | Новый ИЛИ буст существующего |
| MemoryGraph | 0.30 + tag_bonus | Новый ИЛИ буст существующего |

**Бустинг:** если факт уже найден Chroma, FTS5 добавляет +0.25, MemoryGraph добавляет +0.30 к fusion-счёту (не заменяет).

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
| `LOCAL_EMBED_MODEL` | `/data/models/embeddinggemma-300M-Q8_0.gguf` | Путь к GGUF-файлу для локальных эмбеддингов |
| `EMBED_MODEL_HF` | embeddinggemma-300M | HF-модель для авто-загрузки в entrypoint |
| `SECRETS_FILE` | /data/secrets/secrets.enc | Путь к шифрованным секретам |
| `AGE_KEY` | — | Age secret key для SecureStore |
| `FTS5_DB` | /data/fts5/memory.db | Путь к SQLite FTS5 |
| `CHROMA_DIR` | /data/chroma | Путь к ChromaDB |
| `MEMORYGRAPH_DIR` | /data/memorygraph | Путь к MemoryGraph |
| `CHROMA_COLLECTION` | memory_{AGENT_ID} | Имя коллекции Chroma (авто) |
| `CHROMA_SESSIONS` | sessions_{AGENT_ID} | Имя коллекции сессий (авто) |

---

## 7. Текущее состояние (19.06.2026)

| Агент | Gateway | Memory | FTS5 | Chroma | MemoryGraph | Статус |
|-------|---------|--------|------|--------|-------------|--------|
| agent-alpha | :8642 ✅ | :8711 ✅ | 48 | 51 | 259 | Up |
| agent-beta | :8643 ✅ | :8712 ✅ | — | — | — | Up |

**Все 3 бэкенда работают** (FTS5 + Chroma + MemoryGraph). Эмбеддинги — локальный embeddinggemma-300M (768d, llama-cpp). Recency boost активен.

**Hermes plugin:** `memory.provider=hybrid`, инструменты `hybrid_search`/`hybrid_status` активны.

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

### 8.4 MemoryGraph integer overflow
- **Симптом:** `SearchQuery limit=8589934592` → Pydantic validation error
- **Причина:** неограниченный `limit * 2` при передаче между unified_search → memorygraph_search
- **Фикс:** `limit = min(max(1, limit), 100)` в `memorygraph_search` и `unified_search`

### 8.5 Local embeddings
- **Первая загрузка:** entrypoint авто-загружает GGUF с HuggingFace (~300 MB)
- **Размерность:** embeddinggemma-300M даёт 768d
- **CPU:** Celeron N4000 (2 ядра) — ~200–500ms на эмбеддинг, достаточно для домашнего агента
- **Память:** модель 300 MB + llama.cpp runtime ~50 MB — помещается в 512 MB RAM

### 8.6 SecureStore
- **Без `AGE_KEY`** — все запросы к `/memory/secrets` возвращают 503
- **age** должен быть установлен в контейнере (`apt-get install age`)
- **Запись атомарна:** `set()` перезаписывает весь файл заново
- **Декрипт при старте** — если файл существует и `AGE_KEY` задан

### 8.7 hybrid_search из хоста
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
for port in 8711 8712; do
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
 -d '{"text":"Настроил новый бэкенд памяти для агента"}' | python3 -m json.tool
```
