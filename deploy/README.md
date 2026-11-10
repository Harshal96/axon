# Deploy Axon (no clone)

Run Axon from prebuilt images. The target host needs only Docker with the
Compose plugin — no git checkout, Python, Node, or local image build.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/Harshal96/axon/main/deploy/setup.sh | sh
```

This creates `./axon-deploy/` with `docker-compose.yml` and a `.env`
containing generated Neo4j/API secrets, pulls
`ghcr.io/harshal96/axon` and `ghcr.io/harshal96/axon-web`, initializes
the Neo4j schema (one-shot `axon-init` service), and starts:

| Service | Default URL | Notes |
| --- | --- | --- |
| Web UI | `http://localhost:8081` | nginx serving the built UI, proxies `/api` |
| API | `http://localhost:8080` | health at `/health` |
| Neo4j | `127.0.0.1:7474` / `:7687` | loopback only; password in `.env` |

Manual equivalent (if you prefer not to pipe a script to `sh`):

```bash
mkdir axon-deploy && cd axon-deploy
curl -O https://raw.githubusercontent.com/Harshal96/axon/main/deploy/docker-compose.yml
curl -O https://raw.githubusercontent.com/Harshal96/axon/main/deploy/.env.example
cp .env.example .env   # replace both change-me secrets with long random values
docker compose pull
docker compose up -d
```

Pin a release instead of tracking `main`: set `AXON_VERSION=0.2.0` in
`.env`, or run the script with `AXON_VERSION=0.2.0` and
`AXON_REF=v0.2.0` in the environment.

## Indexing

The server indexes repository paths on its own filesystem. The compose
file mounts the host directory `./repos` (override `AXON_REPOS_DIR`)
read-only at `/repos` in the API container:

```bash
# host: make the checkout visible to the server (clone, rsync, ...)
cp -r /path/to/my-app repos/my-app        # must contain an axon.yaml

# anywhere the CLI is installed:
axon configure --api-url http://<host>:8080
axon index --repo /repos/my-app
axon jobs watch <id>                      # if you detached with Ctrl-C
```

After updating a checkout, re-run `axon index`; the same commit is
skipped unless `--force`. Incremental state persists in the
`axon_state` volume.

## Update / uninstall

```bash
cd axon-deploy
docker compose pull && docker compose up -d   # update to AXON_VERSION
docker compose down                           # stop, keep graph data
docker compose down -v                        # stop and delete all data
```

## Configuration

Everything lives in `.env`; see `.env.example` for ports, the repos
mount, optional bearer-token auth (`API_AUTH_MODE=dev`, CLI/MCP only —
the web UI cannot send a token yet), Ask model settings, and Neo4j
heap sizing for large monorepos.

Axon is local-first with no auth by default and permissive CORS. Do
not expose the published ports to an untrusted network; put the stack
behind a VPN, SSH tunnel, or authenticated TLS reverse proxy. See the
root `SECURITY.md`.

Images are built by `.github/workflows/publish-images.yml` on pushes
to `main` (`latest` + `sha-*` tags) and on `v*` tags (semver tags).
