FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLBACKEND=Agg

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY backend ./backend
RUN pip install --no-cache-dir .

# Run as a normal user. data/ (KB cache) and exports/ (CSV and PNG files) are
# Docker volumes; a new volume takes this owner from the image.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data /app/exports \
    && chown app:app /app/data /app/exports
USER app

EXPOSE 8000

# The slim image has no curl, so the health check uses Python.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# One worker on purpose: the KB cache is a single DuckDB file and the
# startup crawler runs as a thread inside this process.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
