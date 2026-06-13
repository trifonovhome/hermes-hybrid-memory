"""
Hybrid memory plugin — FTS5 + Chroma semantic search via LiteLLM bge-m3 + MemoryGraph.

Combines:
  - FTS5 SQLite (keyword precision, BM25 ranking)
  - Chroma + LiteLLM bge-m3 (semantic understanding, cosine scores)
  - MemoryGraph SQLite + spaCy (graph relationships, recency boost)

All backends run on SQLite — no PostgreSQL, no Docker containers.
Activate with: hermes config set memory.provider hybrid

Tools exposed:
  - hybrid_search(query, n)    — fused multi-backend search
  - hybrid_status              — provider health check
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Ensure hybrid provider module is importable
_SCRIPT_DIR = "/home/andreitrifonov/scripts"
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from agent.memory_provider import MemoryProvider

# ── Tool Schemas ──────────────────────────────────────────────────────────

SEARCH_SCHEMA = {
    "name": "hybrid_search",
    "description": (
        "Hybrid memory search — combines FTS5 keyword precision with "
        "Chroma semantic understanding via bge-m3 (1024d). "
        "Returns ranked results from both backends with fusion scores. "
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

ALL_TOOL_SCHEMAS = [SEARCH_SCHEMA, STATUS_SCHEMA]


# ── MemoryProvider Implementation ─────────────────────────────────────────

class HybridMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider wrapping the hybrid FTS5+Chroma stack."""

    def __init__(self):
        self._provider = None       # the actual HybridMemoryProvider from scripts
        self._initialized = False
        self._prefetch_cache: str = ""

    # ── Abstract interface ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "hybrid"

    def is_available(self) -> bool:
        """Check that SQLite databases exist and chromadb is installed."""
        import os
        fts5_db = Path(os.environ.get("HYBRID_FTS5_DB",
            "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/fts5/memory.db"))
        chroma_dir = Path(os.environ.get("HYBRID_CHROMA_DIR",
            "/home/andreitrifonov/infra/docker/hermes-hybrid-memory/data/andrei/chroma"))

        if not fts5_db.exists() and not chroma_dir.exists():
            logger.debug("Hybrid: no databases found — provider inactive")
            return False

        try:
            import chromadb  # noqa: F401
        except ImportError:
            logger.debug("Hybrid: chromadb not installed — provider inactive")
            return False

        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Lazy-init the hybrid provider — embeds on first use to avoid
        LiteLLM call at startup."""
        if self._initialized:
            return

        agent_context = kwargs.get("agent_context", "")
        if agent_context in ("cron", "flush"):
            logger.debug("Hybrid skipped: cron/flush context")
            return

        # Auto-switch memory paths based on active model config
        self._detect_memory_mode()

        logger.info("Hybrid memory provider initialized (session=%s)", session_id)
        self._initialized = True

    def _detect_memory_mode(self):
        """Read model.base_url from config.yaml; if Docker Gateway (:8642),
        set env vars to point hybrid_memory_provider to Docker data dirs."""
        import os
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
                        "/home/andreitrifonov/infra/docker/hermes-trial"
                        "/data/andrei/fts5/memory.db")
                    os.environ["HYBRID_CHROMA_DIR"] = (
                        "/home/andreitrifonov/infra/docker/hermes-trial"
                        "/data/andrei/chroma")
                    logger.info("Hybrid: Docker Gateway mode — switched to Docker data dirs")
                else:
                    os.environ.pop("HYBRID_FTS5_DB", None)
                    os.environ.pop("HYBRID_CHROMA_DIR", None)
                    logger.info("Hybrid: host/direct mode — using host data dirs")
        except Exception as e:
            logger.debug("Hybrid: _detect_memory_mode failed: %s", e)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return ALL_TOOL_SCHEMAS

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "hybrid_search":
            return self._handle_search(args)
        elif tool_name == "hybrid_status":
            return self._handle_status()
        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

    # ── Optional hooks ──────────────────────────────────────────────────

    def system_prompt_block(self) -> str:
        """Static description injected into the system prompt."""
        return (
            "\n## Hybrid Memory (trial)\n"
            "You have a hybrid memory backend with two search layers:\n"
            "- FTS5 (keyword precision) — 42 deduped facts from hermes-local-memory\n"
            "- Chroma + LiteLLM bge-m3 (semantic) — 84 facts with cosine ranking\n"
            "\n"
            "Use `hybrid_search` for infrastructure facts, config details, "
            "and architecture decisions. Use `hybrid_status` to check health.\n"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return cached context from the last search."""
        return self._prefetch_cache

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Run a background search and cache results for next turn."""
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
        """Mirror memory writes to Chroma for future semantic search."""
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
            logger.debug("Hybrid: mirrored memory write (%s/%s)", target, action)
        except Exception as e:
            logger.debug("Hybrid: mirror write failed: %s", e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called at session end — nothing to persist (SQLite is durable)."""
        pass

    def shutdown(self) -> None:
        self._provider = None
        self._initialized = False

    # ── Internal ────────────────────────────────────────────────────────

    def _ensure_provider(self):
        """Lazy-load the actual hybrid provider."""
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

    def _handle_status(self) -> str:
        self._ensure_provider()
        if self._provider is None:
            return json.dumps({"error": "Hybrid provider not available"})

        s = self._provider.status()
        return json.dumps(s, ensure_ascii=False, default=str)