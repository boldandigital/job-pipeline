FROM python:3.12-slim

WORKDIR /app

# System deps for Patchright (Chromium)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 \
    libgtk-3-0 libxshmfence1 \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Patchright
RUN python -m patchright install chromium

COPY . .

# Create data directory
RUN mkdir -p /app/data

ENV DB_PATH=/app/data/jobs.db
ENV JOBSPY_DB_PATH=/app/data/jobspy.db
ENV PYTHONPATH=/app

EXPOSE 8080

# Default: run the dashboard
CMD ["python", "-m", "src.pipeline.dashboard", "--host", "0.0.0.0", "--port", "8080"]
