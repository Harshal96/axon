FROM python:3.11-slim

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY axon ./axon
COPY cli ./cli
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "python", "-m", "axon.api.app"]
