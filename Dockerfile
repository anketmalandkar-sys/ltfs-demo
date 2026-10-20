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
COPY requirements-dev.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
USER agent
ENTRYPOINT ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# Runtime image (default)
FROM base AS runtime
USER agent
ENTRYPOINT ["python", "-m", "email_agent.main"]
CMD ["--source", "mock"]
