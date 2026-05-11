FROM python:3.11-slim

WORKDIR /app

# Install all system dependencies including unzip
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    libssl-dev \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Download Telegram local Bot API server binary
RUN curl -L https://github.com/tdlib/telegram-bot-api/releases/download/v7.3/telegram-bot-api-amd64-linux.zip \
    -o /tmp/tgapi.zip \
    && unzip /tmp/tgapi.zip -d /usr/local/bin/ \
    && chmod +x /usr/local/bin/telegram-bot-api \
    && rm /tmp/tgapi.zip

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source
COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

CMD ["python", "main.py"]
