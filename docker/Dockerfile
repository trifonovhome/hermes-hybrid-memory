FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential curl grep git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    chromadb==1.5.9 \
    spacy==3.8.14 \
    memorygraphMCP \
    'mautrix[encryption]' \
    asyncpg \
    aiosqlite \
    Markdown \
    aiohttp-socks

RUN python -m spacy download ru_core_news_sm && \
    python -m spacy download ru_core_news_md

RUN pip install --no-cache-dir hermes-agent

COPY hybrid_memory_agent.py /opt/memory/hybrid_memory_agent.py
COPY entrypoint-unified.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

RUN mkdir -p /home/hermes/.hermes /data/memory && \
    chmod 777 /home/hermes /home/hermes/.hermes /data /data/memory

WORKDIR /home/hermes
ENTRYPOINT ["/opt/entrypoint.sh"]