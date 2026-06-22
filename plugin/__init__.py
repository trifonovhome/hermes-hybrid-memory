"""
Hybrid memory plugin — FTS5 + Chroma + MemoryGraph + Cross-Interface Bridge v2.

Cross-Interface Memory Bridge:
  - queue_prefetch: loads context from all backends for next turn
  - on_session_end: LLM-extracts session_context → Chroma for future sessions
  - turn_snapshot: stores current turn context in Chroma every turn

Activate with: hermes config set memory.provider hybrid
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_SCRIPT_DIR = "/home/andreitrifonov/scripts"
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from agent.memory_provider import MemoryProvider

# ── Cross-Interface Bridge: LLM Context Extraction Prompt ────────────────

SESSION_CONTEXT_PROMPT = """Ты извлекаешь контекст из диалога для будущих сессий.
Ниже — последние сообщения диалога. Извлеки 3-5 ключевых фактов которые
понадобятся агенту в следующей сессии чтобы продолжить работу без потери контекста.

Формат: JSON с ключом "context" — одна строка на русском, 50-200 слов.

Правила:
- Только новое и важное: решения, результаты, планы, архитектурные выборы
- Не включай очевидное, приветствия, вопросы без ответов
- Если диалог короткий или нет значимых решений — верни {"context": ""}
- Пиши от третьего лица: "Агент настроил X", "Решено использовать Y"
"""

# ── Tool Schemas ──────────────────────────────────────────────────────────

SEARCH_SCHEMA = {
    "name": "hybrid_search",
    "description": (
        "Hybrid memory search — combines FTS5 keyword precision with "
        "Chroma semantic understanding via bge-m3 (1024d) and MemoryGraph "
        "graph relationships. "
        "Returns ranked results from all three backends with fusion scores. "
        "Use this as the primary memory recall tool. "
        "Good for: infrastructure facts, configuration details, architecture decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query in Russian or English.",
            },
            "n": {
                "type": "integer",
                "description": "Max results (default 5, max 10).",
                "default": 5,
            },
        },
        "required": ["query"],
    },
}

STATUS_SCHEMA = {
    "name": "hybrid_status",
    "description": (
        "Get hybrid memory provider status — database sizes, "
        "backend availability, and embedder configuration."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

SECURE_GET_SCHEMA = {
    "name": "hybrid_secure_get",
    "description": (
        "Retrieve a secret from the agent's encrypted SecureStore. "
        "Use this to fetch stored tokens (HA, API keys) at runtime. "
        "Requires AGE_KEY to be configured in the container."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "description": "Secret key to retrieve (e.g., 'ha_token', 'api_key').",
            },
        },
        "required": ["key"],
    },
}

ALL_TOOL_SCHEMAS = [SEARCH_SCHEMA, STATUS_SCHEMA, SECURE_GET_SCHEMA]


# ── MemoryProvider Implementation ─────────────────────────────────────────

class HybridMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider — hybrid memory + Cross-Interface Bridge."""

    def __init__(self):
        self._provider = None
        self._initialized = False
        self._prefetch_cache: str = ""
        self._session_id: str = ""
        self._chroma_collection = "memory_andrei"
        self._bridge_collection = "bridge_context"

    # ── Abstract interface ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "hybrid"

    def is_available(self) -> bool:
        fts5_db = Path(os.environ.get("HYBRID_FTS5_DB",
            "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/fts5/memory.db"))
        chroma_dir = Path(os.environ.get("HYBRID_CHROMA_DIR",
            "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/chroma"))

        if not fts5_db.exists() and not chroma_dir.exists():
            return False

        try:
            import chromadb  # noqa: F401
        except ImportError:
            return False

        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        if self._initialized:
            return

        agent_context = kwargs.get("agent_context", "")
        if agent_context in ("cron", "flush"):
            return

        self._session_id = session_id
        self._detect_memory_mode()
        logger.info("Hybrid+BRIDGE: initialized session=%s", session_id)
        self._initialized = True

    def _detect_memory_mode(self):
        try:
            from hermes_constants import get_hermes_home
            conf = get_hermes_home() / "config.yaml"
            if conf.exists():
                import yaml
                with open(conf) as f:
                    cfg = yaml.safe_load(f)
                base_url = str(cfg.get("model", {}).get("base_url", ""))
                if ":8642" in base_url:
                    os.environ["HYBRID_FTS5_DB"] = (
                        "/home/andreitrifonov/infra/docker/hermes-hybrid-memory"
                        "/data/andrei/fts5/memory.db")
                    os.environ["HYBRID_CHROMA_DIR"] = (
                        "/home/andreitrifonov/infra/docker/hermes-hybrid-memory"
                        "/data/andrei/chroma")
        except Exception as e:
            logger.debug("Hybrid: _detect_memory_mode failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "hybrid_search":
            return self._handle_search(args)
        elif tool_name == "hybrid_status":
            return self._handle_status()
        elif tool_name == "hybrid_secure_get":
            return self._handle_secure_get(args)
        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # ── Optional hooks ──────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        mg_count = 0
        chroma_count = 0
        fts5_ok = False
        bridge_count = 0
        try:
            self._ensure_provider()
            if self._provider:
                mg_count = self._provider.memorygraph.count()
                chroma_count = self._provider.chroma.count()
                fts5_ok = self._provider.fts5.db_path.exists()
                bridge_count = self._bridge_context_count()
        except Exception:
            pass
        fts5_str = f"FTS5 (keyword precision) — {'active' if fts5_ok else 'inactive'}"
        chroma_str = (f"Chroma + embeddinggemma-300M (semantic, 768d, "
                      f"{self._provider.embedder.label if self._provider else 'local GGUF'})"
                      f" — {chroma_count} facts with cosine ranking")
        mg_str = f"MemoryGraph (graph relationships) — {mg_count} nodes via SQLite"
        bridge_str = f"Cross-Interface Bridge — {bridge_count} session contexts"
        return (
            "\n## Hybrid Memory (trial)\n"
            "You have a hybrid memory backend with three search layers:\n"
            f"- {fts5_str}\n"
            f"- {chroma_str}\n"
            f"- {mg_str}\n"
            f"- {bridge_str}\n"
            "\n"
            "Use `hybrid_search` for infrastructure facts, config details, "
            "and architecture decisions. Use `hybrid_status` to check health.\n"
            "Use `hybrid_secure_get(key)` to retrieve secrets from the agent's "
            "encrypted SecureStore (HA tokens, API keys).\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached context + session context from previous sessions."""
        parts = []
        if self._prefetch_cache:
            parts.append(self._prefetch_cache)

        # ── Cross-Interface Bridge: load session context ──────────────
        bridge_ctx = self._load_bridge_context(query)
        if bridge_ctx:
            parts.append(bridge_ctx)

        return "\n".join(parts) if parts else ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Run background search + bridge lookup for next turn."""
        if not query.strip():
            return
        self._ensure_provider()
        if self._provider is None:
            return

        try:
            results, _elapsed = self._provider.search(query, n=5)
            if results:
                lines = ["## Hybrid Memory Context\n"]
                for item in results[:5]:
                    lines.append(f"- [{item['fusion_score']:.3f}] {item['memory'][:200]}")
                self._prefetch_cache = "\n".join(lines)
            else:
                self._prefetch_cache = ""

            # ── Cross-Interface Bridge: store turn snapshot ──────────
            self._store_turn_snapshot(session_id or self._session_id, query)

        except Exception as e:
            logger.debug("Hybrid prefetch error: %s", e)
            self._prefetch_cache = ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if action not in ("add", "replace"):
            return
        self._ensure_provider()
        if self._provider is None:
            return

        try:
            source = (metadata or {}).get("write_origin", "memory-write")

            self._provider.chroma.add(
                documents=[content],
                metadatas=[{"source": source, "target": target}],
            )

            fts5_db = self._provider.fts5.db_path
            if fts5_db.exists():
                conn = sqlite3.connect(str(fts5_db))
                now = datetime.now(timezone.utc).isoformat()
                fact_id = str(uuid.uuid4())[:8]
                agent = os.environ.get("AGENT_ID", "andrei")
                conn.execute(
                    "INSERT INTO facts (id, content, source, agent_id, peer_id, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (fact_id, content, source, agent, agent, now, now),
                )
                conn.execute(
                    "INSERT INTO facts_fts(fact_id, content) VALUES (?, ?)",
                    (fact_id, content),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            logger.debug("Hybrid: mirror write failed: %s", e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Cross-Interface Bridge: extract context via LLM, store for future sessions."""
        if not messages or len(messages) < 3:
            return

        try:
            # Take last user+assistant messages (skip tool calls)
            dialog = []
            for m in messages[-20:]:
                role = m.get("role", "?")
                content = m.get("content", "")
                if role in ("user", "assistant") and content:
                    dialog.append(f"[{role}] {content[:500]}")

            if len(dialog) < 2:
                return

            text = "\n".join(dialog)
            context_str = self._extract_context_via_llm(text)

            if context_str and len(context_str) > 20:
                self._store_bridge_context(
                    session_id=self._session_id,
                    context_type="session_context",
                    content=context_str,
                    source=f"session_end:{self._session_id[:12]}"
                )
                logger.info("BRIDGE: session_context stored (%d chars)", len(context_str))
        except Exception as e:
            logger.debug("BRIDGE: on_session_end failed: %s", e)

    def shutdown(self) -> None:
        self._provider = None
        self._initialized = False

    # ── Bridge: Chroma context storage ──────────────────────────────────

    def _get_bridge_chroma(self):
        """Get or create the bridge context Chroma collection."""
        import chromadb
        chroma_dir = os.environ.get("HYBRID_CHROMA_DIR",
            "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/chroma")
        client = chromadb.PersistentClient(path=chroma_dir)
        return client.get_or_create_collection(
            name=self._bridge_collection,
            metadata={"hnsw:space": "cosine"}
        )

    def _bridge_context_count(self) -> int:
        try:
            coll = self._get_bridge_chroma()
            return coll.count()
        except Exception:
            return 0

    def _store_turn_snapshot(self, session_id: str, query: str) -> None:
        """Store current query as turn_snapshot for cross-interface continuity."""
        if not session_id or not query:
            return
        try:
            self._ensure_provider()
            if self._provider is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            embeddings = self._provider.embedder.embed([query])
            if not embeddings:
                return
            coll = self._get_bridge_chroma()
            snap_id = f"turn:{session_id}:{now}"
            coll.upsert(
                ids=[snap_id],
                documents=[query],
                embeddings=embeddings,
                metadatas=[{
                    "type": "turn_snapshot",
                    "session_id": session_id,
                    "created_at": now,
                }]
            )
        except Exception as e:
            logger.debug("BRIDGE: turn_snapshot failed: %s", e)

    def _store_bridge_context(self, session_id: str, context_type: str,
                               content: str, source: str) -> None:
        """Store extracted context in bridge Chroma collection."""
        try:
            self._ensure_provider()
            if self._provider is None:
                return
            now = datetime.now(timezone.utc).isoformat()
            embeddings = self._provider.embedder.embed([content])
            if not embeddings:
                return
            coll = self._get_bridge_chroma()
            ctx_id = f"{context_type}:{session_id}:{now}"
            coll.upsert(
                ids=[ctx_id],
                documents=[content],
                embeddings=embeddings,
                metadatas=[{
                    "type": context_type,
                    "session_id": session_id,
                    "source": source,
                    "created_at": now,
                }]
            )
        except Exception as e:
            logger.debug("BRIDGE: store_context failed: %s", e)

    def _load_bridge_context(self, query: str) -> str:
        """Search bridge Chroma for relevant session contexts."""
        if not query.strip():
            return ""
        try:
            self._ensure_provider()
            if self._provider is None:
                return ""
            embeddings = self._provider.embedder.embed([query])
            if not embeddings:
                return ""
            coll = self._get_bridge_chroma()
            results = coll.query(query_embeddings=embeddings, n_results=3,
                                where={"type": "session_context"})
            if results and results.get("documents") and results["documents"][0]:
                lines = ["## Cross-Interface Bridge: Previous Session Context\n"]
                for doc, meta, dist in zip(
                    results["documents"][0],
                    results.get("metadatas", [[]])[0],
                    results.get("distances", [[]])[0]
                ):
                    sid = meta.get("session_id", "?")[:12]
                    lines.append(f"- [session:{sid}] {doc[:200]}")
                return "\n".join(lines)
        except Exception as e:
            logger.debug("BRIDGE: load_context failed: %s", e)
        return ""

    # ── Bridge: LLM Context Extraction ───────────────────────────────────

    def _extract_context_via_llm(self, dialog_text: str) -> str:
        """Call LLM to extract session context from dialog."""
        try:
            import urllib.request
            litellm_url = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000")
            litellm_key = os.environ.get(
                "LITELLM_MASTER_KEY",
                os.environ.get("LITELLM_API_KEY", ""))

            if not litellm_key:
                # Try reading from config
                try:
                    from hermes_constants import get_hermes_home
                    import yaml
                    conf = get_hermes_home() / "config.yaml"
                    if conf.exists():
                        with open(conf) as f:
                            cfg = yaml.safe_load(f)
                        litellm_key = cfg.get("model", {}).get("api_key", "")
                except Exception:
                    pass

            if not litellm_key:
                logger.debug("BRIDGE: no LLM key for context extraction")
                return ""

            payload = json.dumps({
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": SESSION_CONTEXT_PROMPT},
                    {"role": "user", "content": dialog_text[:4000]}
                ],
                "temperature": 0.3,
                "max_tokens": 300,
            }).encode()

            req = urllib.request.Request(
                f"{litellm_url}/v1/chat/completions",
                data=payload,
                headers={
                    "Authorization": f"Bearer {litellm_key}",
                    "Content-Type": "application/json",
                }
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
            content = resp["choices"][0]["message"]["content"]
            # Try to parse JSON
            try:
                ctx = json.loads(content)
                return ctx.get("context", content)
            except json.JSONDecodeError:
                return content
        except Exception as e:
            logger.debug("BRIDGE: LLM extraction failed: %s", e)
            return ""

    # ── Internal ────────────────────────────────────────────────────────

    def _ensure_provider(self):
        if self._provider is not None:
            return
        try:
            from hybrid_memory_provider import HybridMemoryProvider as HMP
            self._provider = HMP()
            self._initialized = True
            logger.info(
                "Hybrid provider loaded: FTS5=%s, Chroma=%d",
                self._provider.status()["fts5_exists"],
                self._provider.status()["chroma_count"],
            )
        except Exception as e:
            logger.warning("Hybrid: failed to load provider: %s", e)
            self._provider = None

    def _handle_status(self) -> str:
        self._ensure_provider()
        if self._provider is None:
            return json.dumps({"error": "Hybrid provider not available"})
        s = self._provider.status()
        s["bridge_contexts"] = self._bridge_context_count()
        return json.dumps(s, ensure_ascii=False, default=str)

    def _handle_secure_get(self, args: Dict[str, Any]) -> str:
        key = args.get("key", "")
        if not key:
            return json.dumps({"error": "key required"})
        try:
            import urllib.request
            req = urllib.request.Request(
                f"http://127.0.0.1:8711/memory/secrets/{key}"
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            return json.dumps({"key": key, "value": resp.get("value", "")})
        except Exception as e:
            return json.dumps({"error": f"SecureStore unavailable: {e}", "key": key})

    def _handle_search(self, args: Dict[str, Any]) -> str:
        self._ensure_provider()
        if self._provider is None:
            return json.dumps({"error": "Hybrid provider not available", "results": []})

        query = args.get("query", "")
        n = min(args.get("n", 5), 10)

        try:
            results, elapsed = self._provider.search(query, n=n)
        except Exception as e:
            return json.dumps({"error": str(e), "results": []})

        items = []
        for item in results:
            items.append({
                "memory": item["memory"][:300],
                "score": item["fusion_score"],
                "backend": item.get("backends", item.get("backend", "?")),
                "source": item.get("source", ""),
            })

        return json.dumps(
            {
                "query": query,
                "results": items,
                "count": len(items),
                "elapsed_ms": round(elapsed * 1000, 1),
            },
            ensure_ascii=False,
        )
