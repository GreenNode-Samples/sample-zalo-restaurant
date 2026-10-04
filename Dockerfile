# Agent image (the only image deployed to AgentBase). Build context is the repository root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY src/backend/requirements.txt ./requirements.txt
RUN pip install -r requirements.txt
COPY src/backend ./backend
COPY src/frontend ./frontend
# Make the code readable by the non-root user regardless of the host file modes.
RUN chmod -R a+rX /app/backend /app/frontend

# Run as a non-root user. The application writes nothing to disk: state lives in AgentBase Memory.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin app
USER 10001:10001

WORKDIR /app/backend
EXPOSE 8080
# python:3.12-slim has no curl, so the check uses python. /health is served by the AgentBase SDK.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"]
CMD ["python", "main.py"]
