FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Firefox ESR (works on arm64), system libs and fonts
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl unzip \
      firefox-esr \
      libglib2.0-0 libnss3 libxss1 libasound2 \
      fonts-liberation fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install geckodriver from GitHub (multi-arch)
ARG GECKODRIVER_VERSION=0.35.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64)  gd_arch='linux64' ;; \
      arm64)  gd_arch='linux-aarch64' ;; \
      *) echo "Unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -sSL -o /tmp/geckodriver.tar.gz \
      "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${gd_arch}.tar.gz"; \
    tar -C /usr/local/bin -xzf /tmp/geckodriver.tar.gz geckodriver; \
    chmod +x /usr/local/bin/geckodriver; \
    rm /tmp/geckodriver.tar.gz; \
    geckodriver --version

# IMPORTANT: disable Firefox content sandbox in containers (kernel blocks userns)
ENV MOZ_DISABLE_CONTENT_SANDBOX=1
# Default browser for the app
ENV SELENIUM_BROWSER=firefox

# python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# app code
COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
