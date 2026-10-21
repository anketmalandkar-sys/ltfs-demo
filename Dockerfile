FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 agent
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY email_agent ./email_agent
COPY config ./config
COPY data ./data
RUN mkdir -p /app/output && chown agent:agent /app/output

# Test image: docker compose run --rm tests
FROM base AS test
COPY requirements-dev.txt requirements-eval.txt pyproject.toml .env.example ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY evals ./evals
USER agent
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# Runtime image (default). Your .env and the keystore password arrive as build secrets, which are never stored in
# a layer. The image gets an encrypted keystore with the secrets, and a copy of .env where each secret is a
# reference (HF_TOKEN=keystore:HF_TOKEN). CONFIG_HASH changes with .env or the password, so this step reruns.
FROM base AS runtime
ARG CONFIG_HASH=none
RUN --mount=type=secret,id=dotenv --mount=type=secret,id=keystore_password \
    echo "config ${CONFIG_HASH}" && \
    python -m email_agent.keystore build /run/secrets/dotenv /run/secrets/keystore_password /app/keystore.enc /app/.env
# At runtime the password comes from the Docker secret keystore_password.
ENV KEYSTORE_PATH=/app/keystore.enc KEYSTORE_PASSWORD_FILE=/run/secrets/keystore_password
USER agent
ENTRYPOINT ["python", "-m", "email_agent.main"]
CMD ["--source", "mock"]

# Evals image: docker compose run --rm evals. The runtime image (same keystore) plus DeepEval and the evals.
FROM runtime AS evals
USER root
COPY requirements-eval.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-eval.txt
COPY evals ./evals
USER agent
ENTRYPOINT ["python", "-m", "pytest", "evals", "-q", "--tb=short", "-p", "no:cacheprovider"]
CMD []
