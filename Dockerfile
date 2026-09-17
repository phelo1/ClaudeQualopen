FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/New_York

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY universe ./universe
COPY deploy/start.sh ./deploy/start.sh

RUN pip install -e ".[alpaca,ibkr]" && chmod +x deploy/start.sh

# Trader state, charts and the price cache live on volumes so restarts keep them.
VOLUME ["/app/paper_state", "/app/data"]
EXPOSE 8765

# QMAG_BROKER: paper | alpaca | alpaca-live | ibkr | ibkr-live
ENV QMAG_BROKER=paper \
    QMAG_STATE_DIR=/app/paper_state \
    QMAG_DASHBOARD_PORT=8765

CMD ["./deploy/start.sh"]
