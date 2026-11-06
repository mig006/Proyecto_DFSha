# Una sola imagen para meta, DataNode y cliente; el comando decide qué proceso corre.
# Se construye desde la raíz del repo:  docker build -f deploy/Dockerfile .
FROM python:3.12-slim
WORKDIR /app
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src ./src
COPY tests ./tests
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1
