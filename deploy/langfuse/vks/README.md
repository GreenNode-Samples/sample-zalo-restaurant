# Langfuse v3 on VKS (official Helm chart, private)

Self-hosted **Langfuse v3** in a VKS cluster of the customer VPC, exposed only on **private addresses** (a NodePort on the
worker nodes; an internal load balancer needs a GreenNode annotation that is not verified here).
The agent (AgentBase Runtime, Private mode) sends traces to `http://<node-private-ip>:30300`; admins open the UI over the
[client-to-site VPN](../../admin-vpn/README.md). Use this instead of [`../vserver`](../vserver/README.md) when the
customer already runs VKS or wants scaling and HA for the data stores.

```
Agent (Runtime, Private) --- private connection ---> worker node private IP :30300 -> NodePort Service -> langfuse-web pods :3000
Admin laptop --- OpenVPN client-to-site (pfSense) -> worker node private IP :30300
                                                     in-cluster only: worker, postgresql, clickhouse, redis(valkey), s3(minio)
```

| File | Purpose |
|---|---|
| `values.yaml` | Values for the official `langfuse/langfuse` chart: existing Secret for all credentials, `ClusterIP` web service, no public ingress |
| `secrets.example.yaml` | Documents the keys of the `langfuse-secrets` Secret (create it with `kubectl create secret`) |
| `service-nodeport.yaml` | `NodePort` Service (30300) for web: private access without a load balancer (a `LoadBalancer` without a verified internal annotation could get a public address, so none is shipped) |
| `networkpolicy.yaml` | Optional: ingress to the web pods only from the agent, VPN and node ranges |

> **Verify the version.** Chart **1.5.41** deploys Langfuse **3.224.1** (v3 line); chart 2.x deploys Langfuse v4. The
> agent uses the Langfuse Python SDK v4, which exports over OpenTelemetry to `/api/public/otel`. Confirm in the
> Langfuse docs which server version the SDK needs, and check the running version with
> `curl http://<node-private-ip>:30300/api/public/health`. If the SDK requires v4, use chart 2.x and re-check every key in
> `values.yaml` with `helm show values`.

## What was verified

`values.yaml` was rendered with `helm template langfuse langfuse/langfuse --version 1.5.41 -f values.yaml` and checked
against the chart's own values and templates: the credential keys resolve to the `langfuse-secrets` Secret, every
Service is `ClusterIP`, ClickHouse runs as a single replica without ZooKeeper, and Langfuse starts with
`CLICKHOUSE_CLUSTER_ENABLED=false`. **Not verified** (needs your cluster or GreenNode): StorageClass names, the internal
load balancer annotation, resource sizing, and how the load balancer presents source addresses. Those places are
marked `UNVERIFIED` or `TODO` in the files.

## Prerequisites

- A VKS cluster in a VPC **privately connected to AgentBase** (ask GreenNode support to activate it), `kubectl` and `helm` 3.
- Worker nodes with outbound access to pull images from `docker.io` (the chart uses `langfuse/*` and `bitnamilegacy/*` images). For an air-gapped
  cluster mirror the images listed by `helm template` into Container Registry.
- A StorageClass with `ReadWriteOnce` block volumes (set `storageClass` in `values.yaml` if there is no default).

## Steps

```bash
cd deploy/langfuse/vks

# 1. Namespace
kubectl create namespace langfuse

# 2. Secrets (do not store them in files; keep a copy in a password manager)
kubectl -n langfuse create secret generic langfuse-secrets \
  --from-literal=nextauth-secret="$(openssl rand -hex 32)" \
  --from-literal=salt="$(openssl rand -hex 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=postgres-password="$(openssl rand -hex 24)" \
  --from-literal=postgres-admin-password="$(openssl rand -hex 24)" \
  --from-literal=clickhouse-password="$(openssl rand -hex 24)" \
  --from-literal=redis-password="$(openssl rand -hex 24)" \
  --from-literal=s3-root-user=minio \
  --from-literal=s3-root-password="$(openssl rand -hex 24)"

# 3. Install the official chart, pinned
helm repo add langfuse https://langfuse.github.io/langfuse-k8s
helm repo update
helm template langfuse langfuse/langfuse --version 1.5.41 -n langfuse -f values.yaml >/dev/null   # dry check
helm upgrade --install langfuse langfuse/langfuse --version 1.5.41 -n langfuse -f values.yaml
kubectl -n langfuse get pods -w         # wait for web, worker, postgresql, clickhouse, redis, s3

# 4. Private access: a NodePort on the worker nodes (the chart's own Service stays ClusterIP)
kubectl apply -f service-nodeport.yaml
kubectl get nodes -o wide                              # INTERNAL-IP = the private address to use; nodes must have no public exposure on 30300

# 5. Tell Langfuse its URL: set langfuse.nextauth.url in values.yaml to http://<node-private-ip>:30300
helm upgrade langfuse langfuse/langfuse --version 1.5.41 -n langfuse -f values.yaml

# 6. Optional: network policy (edit the CIDRs first)
kubectl apply -f networkpolicy.yaml

curl -s http://<node-private-ip>:30300/api/public/health
```

Security group of the worker nodes, inbound TCP `30300`: only the **AgentBase source range** (`172.30.0.0/16`
and the subnet selected for the Private runtime, in case of source NAT; verify the real source with GreenNode) and the
**VPN client range**. Nothing from `0.0.0.0/0`.

## Create the project and API keys, configure the agent

1. Over the VPN open `http://<node-private-ip>:30300`, sign up the first user (admin), then set
   `langfuse.features.signUpDisabled: true` in `values.yaml` and `helm upgrade`.
2. Create an organization and a project, then **Settings > API Keys > Create new API key**.
3. On the agent runtime set `LANGFUSE_HOST=http://<node-private-ip>:30300`, `LANGFUSE_PUBLIC_KEY=pk-lf-...`,
   `LANGFUSE_SECRET_KEY=sk-lf-...` (see [`../../agent/README.md`](../../agent/README.md)). Send a message to the bot and
   confirm the trace shows up.

## Backups

| Store | Backup |
|---|---|
| PostgreSQL | `kubectl -n langfuse exec langfuse-postgresql-0 -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U langfuse -Fc postgres_langfuse' > pg-$(date +%F).dump` |
| ClickHouse (traces) | Volume snapshot of the ClickHouse PVC if your storage class supports snapshots (verify with GreenNode), or ClickHouse native `BACKUP` to S3-compatible storage |
| MinIO (event blobs) | Volume snapshot of the MinIO PVC, or `mc mirror` to another bucket |
| Secrets | A copy of `langfuse-secrets` in a password manager (`encryption-key` and `salt` are irreplaceable) |

Test a restore into a scratch namespace before relying on it.

## Upgrades

```bash
helm repo update
helm show values langfuse/langfuse --version <new> > /tmp/new-values.yaml   # diff against your values.yaml
# take backups first, then:
helm upgrade langfuse langfuse/langfuse --version <new> -n langfuse -f values.yaml
```

Read the chart README and the Langfuse release notes first. Subchart upgrades (PostgreSQL, ClickHouse, Valkey, MinIO)
follow their own guides. Do not move from chart 1.x to 2.x (Langfuse v3 to v4) without reading the upgrade notes.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Pods `Pending` | no StorageClass or insufficient node resources (ClickHouse needs several GiB) |
| `helm` error "required value" for a password | a key in `langfuse-secrets` is missing or misnamed |
| UI login redirect loop | `langfuse.nextauth.url` does not match the browser URL |
| Langfuse reachable from the Internet | a node has a public IP and its security group allows 30300: close it (a LoadBalancer Service without a verified internal annotation can do the same) |
| Agent cannot send traces | security group, route to the runtime subnet, wrong `LANGFUSE_HOST`, SDK/server version mismatch |

Cleanup: `helm uninstall langfuse -n langfuse` and `kubectl delete namespace langfuse` (this deletes the PVCs and all traces).
