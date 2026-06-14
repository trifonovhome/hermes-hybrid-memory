#!/bin/bash
set -e

AGENT_ID=${AGENT_ID:-andrei}
MEMORY_PORT=${MEMORY_PORT:-8711}
AGENT_PORT=${AGENT_PORT:-8642}
LITELLM_URL=${LITELLM_URL:-http://127.0.0.1:4000}

# Load LiteLLM key if mounted
if [ -f /opt/litellm.env ]; then
    export LITELLM_API_KEY=$(grep -oP 'LITELLM_MASTER_KEY=\K.*' /opt/litellm.env | head -1)
    echo "[entry] LiteLLM key loaded" >&2
fi

echo "[entry] Agent: $AGENT_ID | Memory: $MEMORY_PORT | Agent port: $AGENT_PORT" >&2

# Auto-download embedding model if LOCAL_EMBED_MODEL is set
if [ -n "${LOCAL_EMBED_MODEL:-}" ] && [ ! -f "$LOCAL_EMBED_MODEL" ]; then
    MODEL_DIR=$(dirname "$LOCAL_EMBED_MODEL")
    mkdir -p "$MODEL_DIR"
    echo "[entry] Downloading embedding model to $LOCAL_EMBED_MODEL..." >&2
    # Default: embeddinggemma-300M if no custom model specified
    _model="${EMBED_MODEL_HF:-ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf}"
    curl -fSL "https://huggingface.co/${_model}/resolve/main/embeddinggemma-300M-Q8_0.gguf" \
        -o "$LOCAL_EMBED_MODEL" || true
    if [ -f "$LOCAL_EMBED_MODEL" ]; then
        echo "[entry] Embedding model ready ($(du -h "$LOCAL_EMBED_MODEL" | cut -f1))" >&2
    fi
fi

# ---- (1) Start Memory API in background ----
LISTEN_HOST=127.0.0.1 LISTEN_PORT=$MEMORY_PORT \
    FTS5_DB=/data/memory/fts5/memory.db \
    CHROMA_DIR=/data/memory/chroma \
    MEMORYGRAPH_DIR=/data/memory/memorygraph \
    AGENT_ID=$AGENT_ID \
    LITELLM_URL=$LITELLM_URL \
    LITELLM_API_KEY=$LITELLM_API_KEY \
    LOCAL_EMBED_MODEL=${LOCAL_EMBED_MODEL:-} \
    SHARED_URL=${SHARED_URL:-http://127.0.0.1:8710} \
    PEERS="${PEERS:-}" \
    python3 /opt/memory/hybrid_memory_agent.py &

MEMORY_PID=$!
echo "[entry] Memory API PID=$MEMORY_PID on :$MEMORY_PORT" >&2

# Wait for memory API
for i in $(seq 1 20); do
    if curl -sf http://127.0.0.1:$MEMORY_PORT/health >/dev/null 2>&1; then
        echo "[entry] Memory API ready" >&2
        break
    fi
    sleep 1
done

# ---- (2) Start Hermes Agent in foreground ----
export HERMES_HOME=/home/hermes/.hermes/profile
echo "[entry] Starting Hermes Gateway on :$AGENT_PORT (HERMES_HOME=$HERMES_HOME)" >&2

exec hermes gateway run --replace
