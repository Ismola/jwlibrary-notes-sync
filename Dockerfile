FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/Ismola/jwlibrary-notes-sync" \
      org.opencontainers.image.description="Sincroniza exclusivamente notas Markdown desde copias JW Library" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    BACKUP_DIR=/backups NOTES_DIR=/notes STATE_DIR=/state \
    POLL_INTERVAL_SECONDS=30 SETTLE_SECONDS=10

WORKDIR /app
COPY sync.py /app/sync.py
ENTRYPOINT ["python", "/app/sync.py"]
