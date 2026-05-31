FROM python:3.11-slim

# Install system-level browser dependencies (Chromium + Firefox).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libxshmfence1 \
    libx11-xcb1 \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Firefox system-level dependencies (required by Camoufox/Playwright Firefox).
RUN playwright install-deps firefox

# Fetch Camoufox's patched Firefox binary.
RUN python -m camoufox fetch

# Copy project files.
COPY . .

# Create runtime directories.
RUN mkdir -p data logs

# Run the bot.
CMD ["python", "main.py"]
