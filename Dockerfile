FROM python:3.11-slim

RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
# uv workspace members: their manifests must exist for `uv sync --frozen`.
COPY axon-core/pyproject.toml ./axon-core/pyproject.toml
COPY axon-cli/pyproject.toml ./axon-cli/pyproject.toml
COPY axon-mcp/pyproject.toml ./axon-mcp/pyproject.toml
COPY axon ./axon
COPY cli ./cli
RUN uv sync --frozen --no-dev

EXPOSE 8080
CMD ["uv", "run", "python", "-m", "axon.api.app"]
