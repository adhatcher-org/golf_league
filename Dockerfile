FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Copy application code and dependency manifests, then install exactly what
# uv.lock pins. --frozen refuses to resolve or update the lock file, so the
# image can never drift from what CI tested.
COPY . .
RUN pip install --no-cache-dir uv && \
    uv sync --frozen --no-dev

# Make the venv created by `uv sync` the default Python/uvicorn on PATH.
ENV PATH="/app/.venv/bin:${PATH}"

# Create a non-root user with a fixed uid/gid (predictable ownership for the
# bind-mounted ./data volume) and give it the app directory, including the
# mount point for the SQLite database.
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /usr/sbin/nologin --create-home appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Health check: probe /readyz, not /healthz, so a container whose migrations
# failed is reported unhealthy even though the process itself is running.
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/readyz').status == 200 else 1)"]

# Run application
CMD ["uvicorn", "golf_league.app:app", "--host", "0.0.0.0", "--port", "8000"]
