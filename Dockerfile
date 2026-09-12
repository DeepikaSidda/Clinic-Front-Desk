# Clinic Front-Desk Voice Agent — Amazon Bedrock AgentCore Runtime container.
#
# The runtime contract requires an ARM64 image listening on 0.0.0.0:8080 that
# exposes GET /ping, POST /invocations, and (for the bidirectional voice agent)
# WebSocket /ws. All three are served by entrypoint.py from this one process.
#
# Build for ARM64 explicitly — a local amd64 build will be rejected at deploy:
#   docker buildx build --platform linux/arm64 -t clinic-front-desk:latest .
#
# The `voice` extra pulls the AWS Common Runtime + Bedrock bidirectional
# streaming client that the Strands BidiNovaSonicModel needs for real Nova Sonic
# speech-to-speech; `deploy` pulls Starlette + uvicorn for the HTTP/WS surface.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.12-slim AS base

# awscrt ships prebuilt aarch64 wheels, but keep a compiler available so a
# source build still succeeds on a slim base.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy dependency metadata first so the dependency layer caches independently of
# source edits. The src-layout build backend needs the package tree present, so
# copy it alongside rather than after.
COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN python -m pip install --upgrade pip \
    && pip install ".[voice,deploy]"

# The container entrypoint (kept out of the installed package on purpose: it is
# deployment glue, not library code).
COPY entrypoint.py ./

# Drop root: the container needs no write access to its own filesystem.
RUN useradd --create-home --uid 10001 clinic \
    && chown -R clinic:clinic /app
USER clinic

# Required by the AgentCore Runtime HTTP protocol contract.
EXPOSE 8080

# Backend defaults to DynamoDB; override CLINIC_TABLE_NAME / AWS_REGION per
# environment. CLINIC_BACKEND=memory gives a credential-free smoke run.
ENV CLINIC_TABLE_NAME=clinic-front-desk \
    CLINIC_BACKEND=dynamodb \
    CLINIC_ANALYSIS_INTERVAL_HOURS=24 \
    CLINIC_LOG_LEVEL=INFO

# Mirrors the runtime's own /ping probe so `docker run` surfaces the same signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=4).status==200 else 1)"

CMD ["python", "entrypoint.py"]
