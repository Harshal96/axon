# Security policy

## Reporting a vulnerability

Do not open a public issue for a vulnerability. Use GitHub's private
vulnerability reporting for this repository when available, or contact the
maintainer privately through GitHub. Include the affected commit or version,
the deployment shape, and the smallest safe reproduction you can share.

Do not include production credentials, private source code, or third-party
data in a report.

## Supported version

Axon is pre-1.0. Security fixes are targeted at the current `main` branch
and the latest release when one exists.

## Deployment warning

Axon is local-first. The defaults are intended for a trusted developer
machine:

- API authentication is off unless `API_AUTH_MODE=dev` and a token are
  configured.
- CORS is permissive for local web development.
- Indexing and impact endpoints accept repository paths readable by the
  server process.
- Compose files contain development-only Neo4j credentials.

Do not expose the default API server directly to an untrusted network. Put
it behind your own authenticated, network-restricted controls and replace
all development credentials first.
