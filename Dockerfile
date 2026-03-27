FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies for building Python packages (psycopg2, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements/ ./requirements/
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements/base.txt -r requirements/prod.txt

# Copy project source
COPY . .

# Use production settings during build steps like collectstatic
ENV DJANGO_SETTINGS_MODULE=hrm_backend.settings.prod

# Collect static files into /app/staticfiles
RUN python manage.py collectstatic --noinput


FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Runtime system dependencies (PostgreSQL client library, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN addgroup --system app && adduser --system --ingroup app app

# Copy installed Python packages and project code from builder
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

RUN chown -R app:app /app
USER app

# Default to production settings; can be overridden at runtime
ENV DJANGO_SETTINGS_MODULE=hrm_backend.settings.prod

EXPOSE 8000

# Basic healthcheck – adjust command/endpoint as needed
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python manage.py check --deploy || exit 1

# Default command: run Django via Gunicorn
CMD ["gunicorn", "hrm_backend.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

