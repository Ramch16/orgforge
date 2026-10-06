# Host OrgForge with Docker and HTTPS

Use one Linux server with Docker Engine and Compose v2.24.4+ (the public override
uses `!reset`). Docker with Caddy is a portable choice for OrgForge's current
single-company SQLite storage and long-lived background employees. A persistent
VM avoids ephemeral filesystems and interrupted worker processes. Start with a
server with at least 2 CPUs, 4GB RAM and adequate project storage; size it for your
actual browser and model workload.

## Local container

Generate separate tokens without printing them or putting them in Git:

```sh
export ORGFORGE_CEO_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ORGFORGE_CTO_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ORGFORGE_PROVIDER=mock
docker compose up --build -d
```

Open `http://127.0.0.1:4700` and sign in using the relevant token from your secret
manager. Tokens must be distinct and at least 32 characters. Mock mode tests the
workflow without provider spending. To use Anthropic, bind `ANTHROPIC_API_KEY`
and set `ORGFORGE_PROVIDER=anthropic`. Add other model/peer/telemetry environment
bindings explicitly in a local Compose override; add matching endpoint/routing
configuration in the company's `org.yaml`. Restart after configuration changes.

The image includes Git, native Chromium, browser support and stdio MCP support.
It runs as UID 10001, stores the company under `/var/lib/orgforge`, initializes
only when no database exists and supplies `/healthz`. The `company` named volume
stores the database, `org.yaml`, Markdown skills and product workspaces. Agent
checks execute locally inside this company container by default. The image does
not include host CLI engines or a Docker daemon/socket; live arena's Docker
isolation requires a separately configured execution environment. Do not assume
hosting alone provides per-agent isolation or independent browser environments.

## Public server

Point a DNS hostname at your server. Bind `ORGFORGE_DOMAIN` and the same secret
credentials through your server's secret management. Then:

```sh
docker compose -f compose.yaml -f deploy/compose.public.yaml config --quiet
docker compose -f compose.yaml -f deploy/compose.public.yaml up --build -d
```

Caddy listens on ports 80/443, obtains/renews certificates and forwards to the
healthy company service. The public override removes the application's direct
published port. Allow inbound 80/443, keep application/provider credentials
private and persist Caddy's certificate volumes. Run exactly one application
instance against the company volume. A domain, server and runtime credentials
must be supplied before public deployment; this repository does not create cloud
accounts, purchase servers, update DNS or publish an image.

## Backups and updates

A consistent database backup can be made while the service runs:

```sh
docker compose exec orgforge python -m orgforge.hosting \
  --backup /var/lib/orgforge/backups/company-2026-10-06.db
```

Output paths must be new. Copy backups off the server. A database-only backup
omits product repositories, packages and `org.yaml`: stop the service and snapshot
or archive the entire company volume for a complete restorable company. Restore
into a fresh volume with ownership suitable for UID 10001. Keep the original
volume until you have checked restored state and projects.

Before an upgrade, back up the volume; rebuild and recreate the application:

```sh
docker compose build orgforge
docker compose up -d orgforge
docker compose logs --tail=100 orgforge
```

Database migrations are additive, but rollback still requires a compatible image
and a pre-upgrade company backup. Never use `down --volumes` on company data you
intend to retain. Scheduler enabled state persists; stopping the process can
interrupt a job, whose lease will eventually expire and retry.

## Builds behind an enterprise proxy

Standard builds need no certificate customization. Docker accepts built-in proxy
build arguments without persisting them as image environment variables. If your
trusted proxy uses a private CA, pass a trusted PEM bundle as a BuildKit secret:

```sh
docker build --build-arg HTTP_PROXY --build-arg HTTPS_PROXY --build-arg NO_PROXY \
  --secret id=build_ca,src=/path/to/trusted-ca-bundle.pem -t orgforge:local .
```

The optional secret is used only for pip's build-time TLS verification. It is not
copied into the image. Configure builder DNS to reach your proxy; restricted
cloud environments may need an explicit hostname mapping. Runtime provider
connections still require their own trusted certificate configuration. Keep TLS
verification enabled.
