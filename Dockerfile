FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY main_render.py .

# Create directory for token cache (will be mounted from Render secret files)
RUN mkdir -p /etc/secrets

# Set environment variable for token cache location
ENV MSAL_CACHE_DIR=/etc/secrets

CMD ["python", "main.py"]