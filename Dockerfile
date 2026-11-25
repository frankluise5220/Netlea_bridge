FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    NETLEA_CONFIG=/data/config.json \
    NETLEA_WEB_PORT=8099

WORKDIR /app
RUN pip install --no-cache-dir paho-mqtt websocket-client

COPY bridge.py /app/bridge.py
COPY netlea-light-card.js /app/www/netlea-light-card.js
COPY www/index.html /app/www/index.html
COPY www/disclaimer.html /app/www/disclaimer.html

VOLUME ["/data"]
EXPOSE 8099

CMD ["python", "/app/bridge.py"]
