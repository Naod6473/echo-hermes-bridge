FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bridge.py cleanup.py ./
COPY screen ./screen
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Whisper models and Piper voices are downloaded at first start and kept here.
VOLUME /data
# The Echo downloads the spoken replies from this port.
EXPOSE 8765

ENTRYPOINT ["/entrypoint.sh"]
