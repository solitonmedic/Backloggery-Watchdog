FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir --no-deps .

FROM base AS test
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
RUN pytest

FROM base AS runtime
RUN groupadd --gid 1001 watchdog \
    && useradd --uid 1001 --gid 1001 --no-create-home --shell /usr/sbin/nologin watchdog \
    && mkdir -p /data \
    && chown 1001:1001 /data
USER 1001:1001
VOLUME ["/data"]
ENTRYPOINT ["backloggery-watchdog"]
CMD ["run"]
