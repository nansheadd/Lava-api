FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# On expose 8080 car Fly proxy écoutera sur ce port interne
EXPOSE 8080

# On démarre Uvicorn sur $PORT (par défaut 8080)
CMD sh -c 'uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}'
