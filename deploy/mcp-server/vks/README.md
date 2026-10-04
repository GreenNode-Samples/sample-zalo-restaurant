# MCP server on VKS (Kubernetes)

Run the restaurant MCP server (`src/mcp-server`) in a **VKS** cluster of the customer VPC. The Private MCP Gateway
reaches it on a **private address** of the VPC. Use this instead of [`../vserver`](../vserver/README.md) when the
customer already operates VKS.

```
Agent (AgentBase Runtime) -> Private MCP Gateway (172.30.0.0/16) -> private connection
   -> http://<node-private-ip>:30080/mcp  (NodePort, see "Make it reachable from the gateway")
   -> Service restaurant-mcp-server -> 1 Pod :8080 -> PVC (SQLite)
```

**TLS is not part of these manifests.** The pod speaks plain HTTP on `8080`, while the GreenNode documentation describes the
connector endpoint as a full HTTPS URL. Whether the connector accepts `http://`, and how a certificate would be terminated on
VKS (internal load balancer, ingress), is not verified in this repository: see "TLS" below. The vServer variant
([`../vserver`](../vserver/README.md), Caddy on `:8443`) is the documented HTTPS path.

| File | Purpose |
|---|---|
| `namespace.yaml` | Namespace `restaurant-mcp` |
| `secret.example.yaml` | Example `MCP_API_KEYS` secret (prefer `kubectl create secret`) |
| `pvc.yaml` | 5 Gi `ReadWriteOnce` claim for the SQLite database |
| `deployment.yaml` | 1 replica (`Recreate`), non-root, read-only root filesystem, `/health` probes, PVC on `/app/data` |
| `service.yaml` | `ClusterIP` Service: internal to the cluster, cannot create a public address |
| `service-nodeport.yaml` | `NodePort` (30080) Service to apply instead, so the gateway can reach the server on the worker nodes' private IPs |
| `networkpolicy.yaml` | Optional: ingress only from `172.30.0.0/16` (and ranges you add) |

## Why one replica

SQLite is a single-writer file database on a `ReadWriteOnce` volume. Running more than one pod would either fail to
mount the volume or corrupt expectations. For the restaurant workload one pod is sufficient; availability comes from
Kubernetes rescheduling the pod and from backups. If you need HA, replace SQLite with a managed database: that is a
code change in `src/mcp-server/main.py`.

## Prerequisites

- A VKS cluster in a VPC that is **privately connected to AgentBase** (ask GreenNode support to activate it), with
  `kubectl` pointed at it, and DNS resolution enabled on the VPC.
- The cluster and the gateway subnets must not overlap `172.30.0.0/16`.
- A default `StorageClass` (or set `storageClassName` in `pvc.yaml`).

## Steps

```bash
cd deploy/mcp-server/vks

# 1. Build and push the image (from the repo root)
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/zalo-mcp-server:v1 ../../../src/mcp-server
docker push vcr.vngcloud.vn/<repo>/zalo-mcp-server:v1
#    -> update `image:` in deployment.yaml (and add imagePullSecrets for a private repository)

# 2. Namespace
kubectl apply -f namespace.yaml

# 3. Secret: create it from the command line, never commit real values
export MCP_KEY=$(openssl rand -hex 32)       # RECORD IT, you store the same value in Access Control
kubectl -n restaurant-mcp create secret generic restaurant-mcp-secret --from-literal=MCP_API_KEYS="$MCP_KEY"

# 4. Volume, Deployment, Service (ClusterIP: nothing is reachable from outside the cluster yet)
kubectl apply -f pvc.yaml -f deployment.yaml -f service.yaml
kubectl -n restaurant-mcp rollout status deploy/restaurant-mcp-server

# 5. Make it reachable from the gateway: NodePort on the worker nodes (see the next section)
kubectl apply -f service-nodeport.yaml
kubectl get nodes -o wide                                    # INTERNAL-IP = the private address of a worker node

# 6. Optional NetworkPolicy
kubectl apply -f networkpolicy.yaml
```

## Make it reachable from the gateway

The gateway runs in the AgentBase VPC and needs a **private** address of your VPC. `service.yaml` is `ClusterIP` on purpose: a
`LoadBalancer` Service created without the right annotation may get a public address, and this server must never be on the Internet.

- **NodePort (provided)**: `service-nodeport.yaml` exposes port `30080` on every worker node. The connector URL is
  `http://<node-private-ip>:30080/mcp`. The worker nodes must not be reachable from the Internet on that port (no public IP, or a
  security group that closes it), and their security group must allow TCP `30080` only from the gateway source range.
- **Internal load balancer**: GreenNode's vLB / VKS documentation reviewed for this sample describes no annotation that makes a
  Service load balancer internal, so none is shipped. Get the exact annotation from GreenNode, add it to a `LoadBalancer` Service
  yourself, and check that the address it gets is private before you point the connector at it.

## TLS

The server does not terminate TLS. The documentation describes the connector endpoint as a full HTTPS URL, and an `http://` endpoint
is an unverified alternative: ask GreenNode whether the connector accepts it, and how an internal or custom CA is provided. If HTTPS
is required, terminate it in front of the pod (a reverse proxy such as the Caddy used in [`../vserver`](../vserver/README.md), an
ingress, or a load balancer with a certificate you control) and point the connector at that `https://` URL; none of those is
provided or tested here for VKS.

## TODO before real use

- [ ] **Connector scheme and TLS**: confirm with GreenNode that the connector accepts `http://`, or put a TLS terminator in front (see "TLS").
- [ ] **StorageClass** in `pvc.yaml`.
- [ ] **NetworkPolicy**: confirm the source address seen by the pod (SNAT or not), then adjust the CIDRs.

## Connect to the MCP Gateway

1. **Access Control**: API Key provider `restaurant-mcp-key` with the value of `$MCP_KEY`.
2. **Gateway**: Network mode **Private** (VPC + Subnet of the cluster/LB; DNS resolution on), Inbound Auth = IAM Permissions.
3. **Connector** `restaurant`: Endpoint `http://<node-private-ip>:30080/mcp` (an unverified alternative to the HTTPS URL the docs describe: see "TLS"), Outbound Auth = **API Key**, header key
   `X-Api-Key`, **empty header value prefix** (the console default `Bearer ` would break the key), provider `restaurant-mcp-key`.
4. **Policy Group**: allow the seven `restaurant__*` actions for the agent principal (see the root README).

Verify from a host in the VPC: `MCP_HOST=<node-private-ip> MCP_SCHEME=http MCP_PORT=30080 MCP_API_KEY=$MCP_KEY ../../check_connectivity.sh`
(the script defaults to HTTPS on 8443; use `MCP_SCHEME=https` and your port once a TLS terminator is in place).

## Backups, rotation, updates

```bash
# Backup (consistent copy through the SQLite backup API)
POD=$(kubectl -n restaurant-mcp get pod -l app=restaurant-mcp-server -o jsonpath='{.items[0].metadata.name}')
kubectl -n restaurant-mcp exec "$POD" -- python -c "import sqlite3; s=sqlite3.connect('/app/data/restaurant.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close()"
kubectl -n restaurant-mcp cp "$POD":/app/data/backup.db ./restaurant-$(date +%F).db
kubectl -n restaurant-mcp exec "$POD" -- rm /app/data/backup.db
# Also snapshot the volume if your storage class supports VolumeSnapshots (verify with GreenNode).

# Rotate the key (the env is only read at startup)
kubectl -n restaurant-mcp create secret generic restaurant-mcp-secret \
  --from-literal=MCP_API_KEYS="old_key,new_key" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n restaurant-mcp rollout restart deploy/restaurant-mcp-server
# change the key in Access Control, then repeat with only new_key

# Update the image
kubectl -n restaurant-mcp set image deploy/restaurant-mcp-server mcp=vcr.vngcloud.vn/<repo>/zalo-mcp-server:v2
```

Cleanup: `kubectl delete namespace restaurant-mcp` (this deletes the PVC and the data).
