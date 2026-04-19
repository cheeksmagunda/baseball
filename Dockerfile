FROM python:3.12-slim

WORKDIR /app

# Copy everything first so pip install can find the package
COPY . .

# Install dependencies and create db directory
# Seed runs at startup via FastAPI lifespan hook — not at build time,
# since env vars (BO_ODDS_API_KEY etc.) are not available during build.
RUN pip install --no-cache-dir . && mkdir -p db

EXPOSE 8000

CMD ["python", "run.py"]
