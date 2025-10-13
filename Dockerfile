# === Base image ===
FROM python:3.11-slim

# === Environment settings ===
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Jakarta

# === Working directory ===
WORKDIR /app

# === Install system dependencies ===
# (needed for pandas, ta-lib, Flask, and websocket libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# === Copy and install Python dependencies ===
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# === Copy the rest of the app ===
COPY . .

# === Create data directory ===
RUN mkdir -p /app/data

# === Expose Flask port ===
EXPOSE 8080

# === Healthcheck (optional but useful for Render/CloudRun) ===
HEALTHCHECK CMD curl --fail http://localhost:8080/ || exit 1

# === Command to run the bot ===
CMD ["python", "main.py"]
