# Hermes Hybrid Memory

[🇬🇧 English version](README.md)

Гибридная память для [Hermes Agent](https://github.com/nousresearch/hermes-agent).
**3 бэкенда в одном Docker-контейнере + плагин на хосте**: FTS5 (ключевые слова),
Chroma (семантика, локальный GGUF), MemoryGraph (граф связей). + SecureStore (шифрованные секреты).

## Архитектура

```
agent-alpha (:8642)    agent-beta (:8643)
┌──────────────────┐   ┌──────────────────┐
│ Hermes Gateway   │   │ Hermes Gateway   │
│ Memory API :8711 │   │ Memory API :8712 │
│ FTS5 + Chroma    │   │ FTS5 + Chroma    │
│ + MemoryGraph    │   │ + MemoryGraph    │
└──────────────────┘   └──────────────────┘
    изолированные агенты — у каждого своя память
```

## 3 бэкенда

| # | Бэкенд | Хранилище | Тип поиска | Вес в fusion |
|---|--------|-----------|------------|--------------|
| 1 | **FTS5** | SQLite | BM25 ключевые слова + буст свежести | 0.25× |
| 2 | **Chroma** | ChromaDB | Семантический (локальный GGUF, 768d) | 0.45× |
| 3 | **MemoryGraph** | SQLite | Граф связей + буст свежести | 0.30× |
| 🔐 | **SecureStore** | Age-шифрование | Зашифрованные ключи-значения (токены, API-ключи) | — |

Имя коллекции Chroma: `memory_{AGENT_ID}` (автоматически генерируется для каждого агента).

## Буст свежести (Recency Boost)

Каждый факт хранится с меткой времени `created_at` (ISO 8601). При поиске
применяется буст свежести к базовому скору каждого бэкенда:

```
final_score = base_score × (0.7 + 0.3 × recency_boost(created_at))
```

### Формула recency_boost

```
дней с момента создания    boost    множитель к скору
──────────────────────    ──────    ─────────────────
сегодня                    1.00     × (0.7 + 0.30) = ×1.00
1–7 дней               1.00→0.60   × (0.7 + 0.30)→(0.7 + 0.18)
8–30 дней              0.60→0.30   × (0.7 + 0.18)→(0.7 + 0.09)
31–90 дней             0.30→0.05   × (0.7 + 0.09)→(0.7 + 0.015)
90+ дней                  0.05     × (0.7 + 0.015) = ×0.715
```

- Сегодня: полный вес
- Неделя: 88–100% веса
- Месяц: 79–88% веса
- 90+ дней: сохраняется 71.5% веса — никогда не обнуляется

### Где хранятся таймстемпы

| Бэкенд | Поле | Формат |
|--------|------|--------|
| FTS5 | `facts.created_at` (колонка SQLite) | ISO 8601 с timezone |
| Chroma | `metadatas["created_at"]` | ISO 8601 с Z-суффиксом |
| MemoryGraph | `nodes.created_at` (колонка SQLite) | `YYYY-MM-DD HH:MM:SS` |

Таймстемпы записываются при создании факта. При переиндексации (FTS5→Chroma)
переносятся атомарно — старым фактам не назначается текущая дата.

## Быстрый старт

### Требования

- Docker 24+
- GGUF-модель эмбеддингов: `embeddinggemma-300M-Q8_0.gguf` (319 MB, 768d)
  - Разместить в `data/models/embeddinggemma-300M-Q8_0.gguf`
- Профили Hermes Agent для каждого агента

### 1. Клонирование

```bash
git clone https://github.com/trifonovhome/hermes-hybrid-memory.git
cd hermes-hybrid-memory
```

### 2. Создание директорий данных

```bash
mkdir -p data/{alpha,beta}/{fts5,chroma,memorygraph}
mkdir -p profiles/{alpha,beta}
chown -R 1000:1000 data/ profiles/
```

### 3. Настройка профилей Hermes

Минимальный `profiles/alpha/config.yaml`:

```yaml
model:
  provider: custom
  model: deepseek-v4-pro
  base_url: ${LITELLM_URL:-http://127.0.0.1:4000}/v1

memory:
  provider: hybrid
  memory_char_limit: 5000
```

### 4. Сборка и запуск

```bash
# Собрать всех агентов
docker compose -f docker/docker-compose.yml build --no-cache

# Запустить всех
docker compose -f docker/docker-compose.yml up -d

# Или запустить по отдельности
docker compose -f docker/docker-compose.yml up -d agent-alpha
```

### 5. Проверка

```bash
# Проверка здоровья
curl http://127.0.0.1:8711/health  # agent-alpha
curl http://127.0.0.1:8712/health  # agent-beta

# Статистика памяти
curl http://127.0.0.1:8711/status

# Поиск
curl -X POST http://127.0.0.1:8711/memory/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"инструкция по установке","limit":3}'
```

## REST API

| Метод | Путь | Описание |
|-------|------|----------|
| GET | `/health` | `{"status":"ok","agent":"..."}` |
| GET | `/status` | `{"fts5":N,"chroma":N,"memorygraph":N}` |
| POST | `/memory/search` | Унифицированный поиск по 3 бэкендам |
| POST | `/memory/extract` | LLM-экстракция фактов → все бэкенды |
| POST | `/memory/sessions/search` | Поиск по истории чатов (FTS5 + Chroma) |
| POST | `/memory/sessions/import` | Импорт сессии |

## Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `AGENT_ID` | agent-alpha | Идентификатор агента |
| `AGENT_PORT` | 8642 | Порт Hermes Gateway |
| `MEMORY_PORT` | 8711 | Порт Memory API |
| `LISTEN_PORT` | 8711 | Фактический порт bind |
| `LOCAL_EMBED_MODEL` | `/data/models/embeddinggemma-300M-Q8_0.gguf` | Локальная GGUF-модель эмбеддингов |
| `FTS5_DB` | /data/fts5/memory.db | Путь к базе FTS5 |
| `CHROMA_DIR` | /data/chroma | Директория ChromaDB |
| `MEMORYGRAPH_DIR` | /data/memorygraph | Директория MemoryGraph |

## Алгоритм fusion

3 бэкенда опрашиваются параллельно (limit × 2). Результаты дедуплицируются
по нормализованному ключу (alnum, lowercase, 80 символов). Веса применяются аддитивно:

| Шаг | Бэкенд | Действие |
|-----|--------|----------|
| 1 | Chroma | Новый факт, fusion = 0.45 × cosine_score |
| 2 | FTS5 | Новый ИЛИ буст: +0.25 × normalized_bm25 |
| 3 | MemoryGraph | Новый ИЛИ буст: +0.30 + tag_bonus |

Сортировка по fusion score по убыванию, обрезка до limit.

## Документация

- [SPECIFICATION.ru.md](docs/SPECIFICATION.ru.md) — Полная техническая спецификация
- [SPECIFICATION.md](docs/SPECIFICATION.md) — English version
- [SKILL.md](SKILL.md) — Определение навыка Hermes Agent
- [AGENTS.ru.md](AGENTS.ru.md) — Инструкция по обновлению для AI-агентов
- [AGENTS.md](AGENTS.md) — English version
- [CHANGELOG.md](CHANGELOG.md) — История релизов

## Плагин Hermes

Директория `plugin/` содержит плагин memory provider для Hermes Agent.
Установите его для активации инструментов `hybrid_search` и `hybrid_status`:

```bash
cp -r plugin/ ~/.hermes/hermes-agent/plugins/memory/hybrid/
hermes config set memory.provider hybrid
```

Плагин читает те же базы SQLite/Chroma, что используются Docker-контейнерами.

## Лицензия

MIT
