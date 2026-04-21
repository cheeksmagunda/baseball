FROM python:3.12-slim

# Unbuffered stdout/stderr so crash tracebacks flush before the process exits.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (separate layer for better cache reuse).
# Seed runs at startup via FastAPI lifespan hook — not at build time,
# since env vars (BO_ODDS_API_KEY etc.) are not available during build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && mkdir -p db

COPY . .

EXPOSE 8000

CMD ["python", "run.py"]
