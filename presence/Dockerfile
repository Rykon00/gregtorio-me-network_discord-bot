FROM python:3.13-alpine

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY discord_presence.py /app/discord_presence.py

ENV PYTHONUNBUFFERED=1 PRESENCE_ALIVE_FILE=/tmp/alive
USER nobody
# healthy while the gateway acknowledged a heartbeat within the last two and a half minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(0 if time.time() - os.path.getmtime('/tmp/alive') < 150 else 1)"
CMD ["python", "/app/discord_presence.py"]
