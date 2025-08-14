FROM python:3.11-slim
WORKDIR /app

# Déps système: pandoc pour une conversion top (optionnel mais utile)
RUN apt-get update && apt-get install -y --no-install-recommends pandoc \
    && rm -rf /var/lib/apt/lists/*

# Déps Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code de l'app
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
