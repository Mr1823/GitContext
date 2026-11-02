FROM node:20-alpine AS frontend-build

WORKDIR /app
COPY package.json package-lock.json ./
COPY frontend/package.json ./frontend/package.json
RUN npm ci
COPY frontend ./frontend
COPY vite.config.js ./vite.config.js
RUN npm --prefix frontend run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GITCONTEXT_DATA_DIR=/data \
    GITCONTEXT_REMOTE_CACHE=/data/remotes

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=frontend-build /app/src/gitcontext/static/build ./src/gitcontext/static/build
RUN pip install --no-cache-dir . \
    && useradd --system --create-home --uid 10001 gitcontext \
    && mkdir -p /data \
    && chown -R gitcontext:gitcontext /data

USER gitcontext
VOLUME ["/data"]
EXPOSE 8765
CMD ["gitcontext", "web", "--host", "0.0.0.0", "--port", "8765", "--no-open"]
