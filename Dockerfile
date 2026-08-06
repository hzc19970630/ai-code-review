FROM node:22.18.0-bookworm-slim AS syntax-runtime

WORKDIR /syntax-runtime

COPY syntax_runtime/package.json syntax_runtime/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts

COPY syntax_runtime/runner.cjs ./runner.cjs

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MR_REVIEW_SYNTAX_RUNTIME_DIR=/app/syntax_runtime

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY src ./src
COPY apps ./apps
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
COPY --from=syntax-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=syntax-runtime /syntax-runtime ./syntax_runtime

RUN pip install .

USER app

EXPOSE 8080

CMD ["uvicorn", "mr_review_platform.api:app", "--host", "0.0.0.0", "--port", "8080"]
