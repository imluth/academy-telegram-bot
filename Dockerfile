# Stage 1: Build stage
FROM python:3.9-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Install only the necessary build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first
COPY requirements.txt .

# Install requirements with specific versions
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip list  # Print installed packages for verification

# Stage 2: Runtime stage
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

WORKDIR /app

# Create logs directory and set permissions
RUN mkdir -p /app/logs && chmod 777 /app/logs

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.9/site-packages/ /usr/local/lib/python3.9/site-packages/
COPY --from=builder /usr/local/bin/ /usr/local/bin/

# Copy application code
COPY . /app/

# Create a non-root user
RUN useradd -m botuser && \
    chown -R botuser:botuser /app

# Switch to non-root user
USER botuser

# Verify installations
RUN python -c "import telegram; import dotenv; import psutil; print('All dependencies verified')"

CMD ["python", "team_bot_v2.py"]