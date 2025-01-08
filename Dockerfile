# Stage 1: Build stage
FROM python:3.9-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Install only the necessary build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Stage 2: Runtime stage
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Create logs directory and set permissions
RUN mkdir -p /app/logs

# Create a non-root user
RUN useradd -m botuser && \
    chown -R botuser:botuser /app && \
    chmod -R 755 /app && \
    chmod -R 777 /app/logs

# Copy application code
COPY team_bot.py .

# Switch to non-root user
USER botuser

CMD ["python", "team_bot.py"]