FROM python:3.11-slim

# System dependencies for playwright/patchright browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
    libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    libx11-xcb1 libxcb1 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir supervisor

# Install project in editable mode
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e .

# Install browser (patchright/playwright)
RUN python -m playwright install --with-deps chromium 2>/dev/null || true
RUN python -m patchright install chromium 2>/dev/null || true

# Copy project files
COPY skills/ skills/
COPY scripts/ scripts/
COPY deploy/supervisord.conf deploy/supervisord.conf
COPY deploy/entrypoint.sh deploy/entrypoint.sh
RUN chmod +x deploy/entrypoint.sh

# Create directories for volumes
RUN mkdir -p /app/logs /app/data/memory /app/cookies

# Expose Gateway port
EXPOSE 8765

ENTRYPOINT ["/app/deploy/entrypoint.sh"]
