FROM bluenviron/mediamtx:1 AS mediamtx

FROM python:3.12-slim

# Set working directory inside the container
WORKDIR /app

# Copy MediaMTX binary from official image
COPY --from=mediamtx /mediamtx /usr/local/bin/mediamtx

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy app directory contents
COPY app/ /app/

# Blink may return HTTP 202 (as well as 412) when a 2FA challenge is pending.
# The pinned blinkpy revision only recognizes 412, so patch that single condition
# without pulling in unrelated changes from newer blinkpy revisions.
RUN sed -i 's/if response.status == 412:/if response.status in [202, 412]:/' \
    /app/blinkpy/blinkpy/api.py

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Make scripts executable
RUN chmod +x /app/startup.sh /app/healthcheck.py

# Create data directory for persistent config and credentials
RUN mkdir -p /data

# Add healthcheck
# Runs every 30 seconds, starts checking after 60 seconds,
# allows 10 seconds for the check to complete,
# marks unhealthy after 3 consecutive failures
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python3 /app/healthcheck.py || exit 1

# Environment variables (can be overridden at runtime)
ENV WEB_PORT=8080 \
    WEB_HOST=0.0.0.0 \
    MYBLINK_CONFIG=/data/config.yaml

# Expose web interface port
EXPOSE 8080

# Use SIGTERM for graceful shutdown
STOPSIGNAL SIGTERM

# Set entry point
CMD ["./startup.sh"]
