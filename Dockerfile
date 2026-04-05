FROM python:3.12-slim

WORKDIR /app

# Copy everything first so pip install can find the package
COPY . .

# Install dependencies and create db directory
RUN pip install --no-cache-dir . && mkdir -p db && python -m app.seed

EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
