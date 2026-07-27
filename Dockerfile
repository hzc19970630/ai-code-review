FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

RUN pip install .

USER app

EXPOSE 8080

CMD ["uvicorn", "mr_review_platform.api:app", "--host", "0.0.0.0", "--port", "8080"]
