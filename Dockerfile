FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/bridge.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY vktg ./vktg

VOLUME /data
CMD ["python", "-m", "vktg"]
