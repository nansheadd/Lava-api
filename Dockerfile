FROM python:3.11-slim
WORKDIR /app

# Déps système: pandoc pour une conversion top (optionnel mais utile)
# + bibliothèques nécessaires pour les navigateurs headless Chromium/Chrome.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        pandoc \
        chromium \
        chromium-driver \
        fonts-liberation \
        libasound2 \
        libatk1.0-0 \
        libcairo2 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnss3 \
        libxkbcommon0 \
    && rm -rf /var/lib/apt/lists/*

# Déps Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Télécharge les binaires Playwright/Selenium nécessaires pour les exports.
RUN playwright install chromium

# Code de l'app
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
