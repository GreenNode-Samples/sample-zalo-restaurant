# Langfuse v3 on vServer (docker compose, private)

Self-hosted **Langfuse v3** in the customer VPC, reachable only on a **private IP**. The agent (AgentBase Runtime in
Private mode) sends traces to `http://<langfuse-private-ip>:3000`. Admins open the UI only through the
[client-to-site VPN](../../admin-vpn/README.md). Nothing here is published to the Internet.

```
Agent (AgentBase Runtime, Private) --- private connection ---> vServer <private-ip>:3000  langfuse-web
Admin laptop --- OpenVPN client-to-site (pfSense) ---------->  vServer <private-ip>:3000  (UI)
                                          internal compose network only:
   langfuse-web, langfuse-worker -> postgres, clickhouse, redis, minio
```

The compose file follows the official Langfuse v3 compose (`langfuse-web`, `langfuse-worker`, `postgres`,
`clickhouse`, `redis`, `minio`) with Langfuse pinned to `3.224.1` (`langfuse/langfuse` and `langfuse/langfuse-worker`, the
release the [VKS Helm chart](../vks/README.md) 1.5.41 deploys) and MinIO pinned by digest. It differs from upstream by publishing only `langfuse-web`, only on the private IP,
by refusing to start with missing secrets, and by adding log rotation.

> **Verify the version.** The agent uses the Langfuse Python SDK v4 (`langfuse>=4,<5`), which exports traces over
> OpenTelemetry to `/api/public/otel`. That endpoint exists only in newer Langfuse v3 releases and the SDK documents a
> minimum server version. Check the Langfuse docs for the minimum server version the SDK requires, then confirm the
> running version with `curl http://<private-ip>:3000/api/public/health` (the JSON contains `version`). Upstream `main`
> now ships Langfuse v4 images; if the SDK requires v4, change both tags to a v4 release after reading the upgrade notes.
> The tags are exact on purpose: `docker compose pull` never moves you to a new release, so upgrade deliberately
> (for example to `3.225.11`, the newest 3.x tag when this was written).

## 1. Sizing

Langfuse's guidance for a single-VM Docker Compose deployment is about **4 vCPU and 16 GiB RAM** (verify against the
current Langfuse docs). ClickHouse is the memory-hungry part. Start with:

| Item | Sample starting point |
|---|---|
| vServer | 4 vCPU, 16 GiB RAM |
| Disk | 100 GiB SSD data volume for Docker (`/var/lib/docker`), grow with trace volume |
| Placement | **private subnet**, no Floating IP |

A restaurant bot produces a few traces per guest message; storage grows slowly. For high volume or HA use the
[VKS variant](../vks/README.md) with managed or clustered stores.

## 2. Network and security group

1. Create the vServer (Ubuntu or Debian + Docker Engine + Compose plugin) in a **private subnet** of the VPC that is
   privately connected to AgentBase.
2. Security group, inbound:

   | Protocol | Port | Source | Purpose |
   |---|---|---|---|
   | TCP | `3000` | **AgentBase source range**: `172.30.0.0/16` (AgentBase VPC) and the subnet you selected for the Private runtime (see below) | Trace ingestion |
   | TCP | `3000` | **VPN client range** (the OpenVPN tunnel network of pfSense) | Admin UI |
   | TCP | `22` | VPN client range only | SSH |

   No rule for `0.0.0.0/0`. Do not attach a Floating IP. The data stores publish no ports, so no rule is needed
   for 5432, 8123, 9000, 6379 or 9000/9001 (MinIO).
3. **AgentBase source range.** A Private runtime runs in the AgentBase VPC `172.30.0.0/16` and reaches your VPC over
   the private connection; it is created with a VPC, Subnet and Route CIDRs (see
   [`../../agent/README.md`](../../agent/README.md)). Its traffic arrives from `172.30.0.0/16`, or from an address of the
   selected subnet if GreenNode source-NATs it. The exact source is not documented: allow both, **verify with
   GreenNode**, read the real source in the web container log, then remove the range that is not used. Use the same
   rule as the MCP server, which is reached by the Private MCP Gateway from the same AgentBase range.
4. If the subnet uses a **Network ACL**, remember it is stateless and evaluated before the security group: allow
   inbound TCP 3000 and the return traffic (ephemeral ports) between the agent subnet, the VPN client range and this
   subnet.

## 3. Install and configure

```bash
git clone <repo> && cd sample-zalo-restaurant/deploy/langfuse/vserver
cp .env.example .env && chmod 600 .env

# Generate every secret separately and paste it into .env:
openssl rand -hex 32     # NEXTAUTH_SECRET, SALT, ENCRYPTION_KEY (64 hex chars)
openssl rand -hex 24     # POSTGRES_PASSWORD, CLICKHOUSE_PASSWORD, REDIS_AUTH, MINIO_ROOT_PASSWORD
# Set LANGFUSE_BIND_ADDR and NEXTAUTH_URL (http://<private-ip>:3000) to the vServer private IP.

docker compose config -q          # fails if a required secret is missing
docker compose up -d
docker compose ps                 # all services running/healthy (first start takes a few minutes: migrations)
curl -s http://<private-ip>:3000/api/public/health
```

Keep `.env` out of git and store a copy of the secrets in your password manager. **Losing `ENCRYPTION_KEY` or `SALT`
makes existing encrypted data and API keys unusable.**

## 4. Create the project and API keys

Open `http://<private-ip>:3000` **through the VPN** (see the admin VPN README), then:

1. Sign up the first user (this becomes the admin). Immediately set `AUTH_DISABLE_SIGNUP=true` in `.env` and run
   `docker compose up -d` to block further self-service sign-ups; invite colleagues from the UI instead.
2. Create an **Organization** and a **Project** (for example `zalo-restaurant-bot`).
3. Project **Settings > API Keys > Create new API key**. Copy the `pk-lf-...` public key and the `sk-lf-...` secret key
   (shown once).

Alternative without clicking: uncomment the `LANGFUSE_INIT_*` values in `.env` (org, project, API keys, admin user)
before the **first** start. They apply only when the database is empty.

## 5. Point the agent runtime at it

On the runtime (see [`../../agent/README.md`](../../agent/README.md)):

| Variable | Value |
|---|---|
| `LANGFUSE_HOST` | `http://<langfuse-private-ip>:3000` |
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...` |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...` (a secret: use Access Control where supported, see the agent README) |

If any of the three is missing the agent disables tracing automatically. After the runtime restarts, send a message
to the bot and confirm a trace appears (name, user, session, model and token usage) in the Langfuse UI. Test
reachability from the VPC with `../../check_connectivity.sh`.

## 6. Backups

State lives in four named volumes: `langfuse_postgres_data` (users, projects, API keys, prompts),
`langfuse_clickhouse_data` (traces and observations), `langfuse_minio_data` (raw event blobs, media) and
`langfuse_redis_data` (queues, disposable). Back up PostgreSQL, ClickHouse and MinIO together. A snapshot of the
vServer data disk is the simplest complete backup; the commands below add logical copies.

```bash
mkdir -p backups && ts=$(date +%F-%H%M)

# PostgreSQL (logical dump)
docker compose exec -T postgres pg_dump -U postgres -Fc postgres > backups/postgres-$ts.dump

# ClickHouse + MinIO (cold copy of the volumes; stop writers first for a consistent snapshot)
docker compose stop langfuse-web langfuse-worker
for v in clickhouse_data minio_data; do
  docker run --rm -v langfuse_$v:/data:ro -v "$PWD/backups":/backup alpine \
    tar czf /backup/$v-$ts.tgz -C /data .
done
docker compose start langfuse-web langfuse-worker
```

Copy `backups/` off the host (Object Storage) and test a restore periodically. Restore PostgreSQL with
`pg_restore -U postgres -d postgres --clean --if-exists`, and the volumes by extracting the tarballs into empty
volumes while the stack is stopped.

## 7. Upgrades

1. Read the Langfuse release notes and the self-hosting upgrade guide.
2. Back up (section 6).
3. Edit the image tags in `docker-compose.yml`, then:

   ```bash
   docker compose pull
   docker compose up -d
   docker compose logs -f langfuse-web langfuse-worker    # migrations run on start
   curl -s http://<private-ip>:3000/api/public/health
   ```
4. Roll back by restoring the backup and the previous tags: database migrations are not reversible.

Also diff `docker-compose.yml` against the upstream compose of the target release for new required variables.

## Media and batch export

MinIO is intentionally **not published**. The agent sends text traces, which only need MinIO internally. Features
that hand a presigned MinIO URL to the **browser** (media upload and playback, batch export downloads) will not work
until MinIO is reachable from the admin laptop. If you need them, publish `9000` on the private IP, allow it from the
VPN range only, and set `LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT` and `LANGFUSE_S3_BATCH_EXPORT_EXTERNAL_ENDPOINT` to
`http://<private-ip>:9000` (see the upstream compose for the variable names).

## Hardening checklist

- [ ] Only `LANGFUSE_BIND_ADDR:3000` is listening (`ss -ltnp`); no Floating IP; security group as above.
- [ ] `AUTH_DISABLE_SIGNUP=true` after the admin exists; admins use strong passwords (SSO is available in Langfuse; see its docs).
- [ ] `.env` is `chmod 600`, secrets stored in a password manager, `ENCRYPTION_KEY` and `SALT` backed up.
- [ ] Daily backups copied off the host; restore tested.
- [ ] Images pinned to exact tags in production; `docker compose pull` only during a maintenance window.
- [ ] Host patched; Docker log rotation is already configured in the compose file.
- [ ] Admin access only over the VPN (see [`../../admin-vpn/README.md`](../../admin-vpn/README.md)).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `docker compose` refuses with "set ..." | a required variable is empty in `.env` |
| UI login loops or redirects to the wrong host | `NEXTAUTH_URL` does not match the URL opened in the browser |
| Web container restarts, "migration" errors in logs | ClickHouse or PostgreSQL not ready, or wrong password after editing `.env` on an existing volume (passwords are set at first start; change them inside the databases too) |
| Agent traces missing | `LANGFUSE_*` incomplete on the runtime; security group blocks the agent path; SDK/server version mismatch (verify the version); wrong `LANGFUSE_HOST` (no trailing path) |
| `401` from `/api/public/otel` | wrong API key pair (public/secret from different projects) |
| High memory | ClickHouse; resize the vServer or move to VKS |
