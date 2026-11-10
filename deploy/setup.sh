#!/bin/sh
# Axon one-command install (no source checkout needed).
#
#   curl -fsSL https://raw.githubusercontent.com/Harshal96/axon/main/deploy/setup.sh | sh
#
# Downloads deploy/docker-compose.yml + .env.example into ./axon-deploy,
# generates secrets into .env, pulls the prebuilt images, and starts
# Neo4j + API + web UI. Re-running keeps your existing .env and data.
# Options: -y (non-interactive), AXON_DIR=<dir>, AXON_REF=<git ref>,
# AXON_VERSION=<image tag>. Inspect before piping to sh — it's short.

set -eu

REPO_RAW="https://raw.githubusercontent.com/Harshal96/axon"
AXON_REF="${AXON_REF:-main}"
AXON_DIR="${AXON_DIR:-axon-deploy}"

die() { printf 'error: %s\n' "$1" >&2; exit 1; }

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v docker >/dev/null 2>&1 || die "docker is required: https://docs.docker.com/engine/install/"
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required: https://docs.docker.com/compose/install/"
docker info >/dev/null 2>&1 || die "docker daemon is not running"

gen_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

mkdir -p "$AXON_DIR/repos"
cd "$AXON_DIR"

printf 'Fetching deployment files (ref: %s)...\n' "$AXON_REF"
curl -fsSL "$REPO_RAW/$AXON_REF/deploy/docker-compose.yml" -o docker-compose.yml
curl -fsSL "$REPO_RAW/$AXON_REF/deploy/.env.example" -o .env.example

if [ -f .env ]; then
  printf 'Keeping existing .env\n'
else
  cp .env.example .env
  # Fill the two change-me secrets with generated random values.
  sed -i.bak "s|change-me-generate-a-long-random-password|$(gen_secret)|" .env
  sed -i.bak "s|change-me-generate-a-long-random-token|$(gen_secret)|" .env
  rm -f .env.bak
  printf 'Created .env with generated secrets (keep it private)\n'
fi
printf '%s\n' "$AXON_REF" > .axon-version

if [ -n "${AXON_VERSION:-}" ]; then
  printf 'Pinning images to AXON_VERSION=%s\n' "$AXON_VERSION"
  sed -i.bak "s|^AXON_VERSION=.*|AXON_VERSION=$AXON_VERSION|" .env
  rm -f .env.bak
fi

printf 'Pulling images and starting Axon...\n'
docker compose pull
docker compose up -d --wait || {
  docker compose ps
  die "stack did not become healthy; check: docker compose logs"
}

api_port=$(grep -E '^AXON_API_PORT=' .env | cut -d= -f2)
web_port=$(grep -E '^AXON_WEB_PORT=' .env | cut -d= -f2)
api_port="${api_port:-8080}"
web_port="${web_port:-8081}"

cat <<EOF

Axon is running:
  Web UI:  http://localhost:$web_port
  API:     http://localhost:$api_port/health

Index a repository (the server reads paths under ./repos):
  1. Put a checkout with an axon.yaml in $AXON_DIR/repos/<name>
  2. axon configure --api-url http://localhost:$api_port
     axon index --repo /repos/<name>

Update later with:  cd $AXON_DIR && docker compose pull && docker compose up -d
Stop with:          cd $AXON_DIR && docker compose down   (add -v to delete data)
EOF
