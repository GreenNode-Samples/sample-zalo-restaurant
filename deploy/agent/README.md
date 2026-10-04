# Agent runtime (AgentBase, Private mode)

AgentBase Agent Runtime runs **only the agent image** (`src/backend`, the root `Dockerfile`). The MCP server and
Langfuse run in the customer VPC ([`../mcp-server`](../mcp-server/vserver/README.md), [`../langfuse`](../langfuse/vserver/README.md)).
The runtime is created in **Private** network mode so it can reach them without the Internet. The reply to the guest is the exception: the runtime calls Zalo itself,
so it also needs outbound HTTPS (see [5. Outbound (egress)](#5-outbound-egress)).

```
Zalo -> [webhook proxy, public] -> Agent Runtime (Private) --+--> Private MCP Gateway -> connector `restaurant` -> MCP server
                                                             +--> Langfuse  http://<langfuse-private-ip>:3000
                                                             +--> Memory, LLM (AgentBase / GreenNode AIP)
Agent Runtime --HTTPS sendMessage--> Zalo Bot Platform (Internet)     the reply hop: outbound, not through the webhook proxy
```

## Prerequisites

- The customer VPC is **privately connected to AgentBase**. Ask GreenNode support to activate it. Only such VPCs appear in
  the runtime's network settings. The customer VPC and on-premises ranges must not overlap AgentBase's `172.30.0.0/16`.
- Console role **Root** or **Admin**.
- An **Identity** for the agent in Access Control (the runtime injects `GREENNODE_CLIENT_ID`, `GREENNODE_CLIENT_SECRET`,
  `GREENNODE_AGENT_IDENTITY`; do not set them yourself).
- Memory with one CUSTOM strategy `customer-profile` (see the root README), an LLM key, and a **Private MCP Gateway**
  with the `restaurant` connector (see the root README, "Deployment steps").
- Langfuse running and a project API key pair (see [`../langfuse`](../langfuse/vserver/README.md)).

## 1. Build and push the image to Container Registry

```bash
# from the repository root; use the repo/robot account of your Container Registry
docker login vcr.vngcloud.vn                          # robot account username + secret
docker build --platform linux/amd64 -t vcr.vngcloud.vn/<repo>/zalo-restaurant-bot:v1 .
docker push vcr.vngcloud.vn/<repo>/zalo-restaurant-bot:v1
```

For a private repository enable **Registry Auth** on the runtime and use the robot account `backendName` as the username.
The image listens on `8080` and serves `GET /health`, as the runtime requires.

## 2. Create the runtime in Private mode

Portal: **https://aiplatform.console.vngcloud.vn** > AgentBase > Agent Runtime > **Deploy a new Agent** > **Custom Agent**.

| Field | Value |
|---|---|
| Name | `zalo-restaurant-bot` |
| Image URL | `vcr.vngcloud.vn/<repo>/zalo-restaurant-bot:v1` |
| Flavor | for example `runtime-s2-general-2x4` (as used in the original sample; size to your load) |
| Min / Max replicas | `1` / `1` to start; raise Max to enable autoscaling |
| Registry Auth | enabled if the repository is private |
| Environment variables | see section 3 |
| **Network settings** | **Private** |
| VPC | the customer VPC (only VPCs privately connected to AgentBase are listed; use the refresh icon for a new one) |
| Subnet | a **private** subnet of that VPC |
| Route CIDRs | CIDRs of the other subnets the runtime must reach, one per line (see below) |

**Route CIDRs** (optional in the console, needed here when the target lives in another subnet than the runtime):

- the subnet of the **Langfuse** vServer / internal load balancer, if different from the runtime subnet;
- the subnet of the **Private MCP Gateway**, if the agent reaches the gateway endpoint inside your VPC and it is in another subnet
  (verify with GreenNode which route covers the gateway endpoint).

The runtime does **not** need a route to the MCP server itself: it calls the gateway, and the gateway calls the server.
Route CIDRs only cover customer subnets: the runtime's calls to the Internet (Zalo, LLM, AgentBase APIs) are a separate question, see [5. Outbound (egress)](#5-outbound-egress).
The API reference in the documentation reviewed for this sample does not list the network fields, so use the Portal.

## 3. Environment variables

> The runtime documentation says environment variables are for **non-sensitive configuration**. The agent code in this
> sample reads every value, including secrets, from environment variables, and the documentation reviewed does not describe
> injecting an Access Control secret into a runtime environment. See "Secrets" below before you go to production.

| Variable | Required | Value | Sensitive |
|---|---|---|---|
| `LLM_API_KEY` | yes | GreenNode AIP API key | **yes** |
| `LLM_MODEL` | yes | for example `z-ai/glm-5.3-flash` | no |
| `LLM_BASE_URL` | optional | default `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1`; may be the Sidecar LLM Proxy `http://localhost:18080` (verify with GreenNode, auth and model names may differ) | no |
| `AGENTBASE_MEMORY_ID` | yes | `memory-...` | no |
| `MEMORY_STRATEGY_ID` | yes | `ltms-...` (the `customer-profile` CUSTOM strategy) | no |
| `MCP_RESTAURANT_URL` | yes | the **connector URL of the Private gateway**: the gateway **Endpoint URL** from its detail page followed by `/restaurant` | no |
| `LANGFUSE_HOST` | yes (for traces) | `http://<langfuse-private-ip>:3000` (vServer private IP), or `http://<node-private-ip>:30300` on VKS ([`../langfuse/vks`](../langfuse/vks/README.md)) | no |
| `LANGFUSE_PUBLIC_KEY` | yes (for traces) | `pk-lf-...` | low |
| `LANGFUSE_SECRET_KEY` | yes (for traces) | `sk-lf-...` | **yes** |
| `ZALO_BOT_TOKEN` | for real Zalo | `<id>:<secret>` from Zalo Bot Creator | **yes** |
| `ZALO_WEBHOOK_SECRET` | **yes**, with `ZALO_BOT_TOKEN` | 8 to 256 characters you choose; same value as in `setWebhook`. Without it the webhook answers `503` | **yes** |
| `ZALO_MAX_WORKERS` | optional | Zalo chats processed at the same time, default `8` | no |
| `ZALO_API_BASE` | default | `https://bot-api.zaloplatforms.com` | no |
| `SERVE_UI` | set `false` | disables the web simulator on the endpoint (Zalo-first) | no |
| `AGENT_API_KEY` | recommended | protects `/invocations`, `/a2a` and `/api/*` with `X-API-Key` | **yes** |
| `A2A_PUBLIC_URL` | optional | public base URL of the runtime for the A2A agent card | no |
| `DEBUG_OPS` | keep `0` | `1` only while reading the principal for the gateway policy | no |

Notes:

- Tracing is off unless all three `LANGFUSE_*` variables are set.
- The **MCP API key** (`X-Api-Key`) is **not** an agent variable. It lives in Access Control and the gateway attaches it
  when calling the MCP server; the agent never sees it.
- With `SERVE_UI=false` the Web Simulator is disabled. For a local simulator run see the root README.

### Secrets

Options, in order of preference. Confirm availability with GreenNode.

1. **Reference Access Control secrets from the runtime**, if the platform supports secret references for runtime
   environment variables. This is not documented in the pages reviewed: **verify with GreenNode**. If supported, store
   `LLM_API_KEY`, `LANGFUSE_SECRET_KEY`, `ZALO_BOT_TOKEN`, `ZALO_WEBHOOK_SECRET` and `AGENT_API_KEY` there.
2. **Avoid the secret** where an alternative exists: the Sidecar LLM Proxy removes the need for a long-lived `LLM_API_KEY` in
   the environment (verify with GreenNode for your runtime version).
3. **Otherwise set them as runtime environment variables explicitly**, and compensate: restrict who can open the runtime in
   the console (Root/Admin only, least privilege), never print them in logs or chat, generate distinct values per
   environment, and rotate on a schedule and whenever someone with access leaves. Treat the runtime configuration page
   as sensitive.
4. A code change to fetch secrets at startup from an external secret store (for example one running in the VPC) is not part of
   this sample.

## 4. Runtime Security Settings

Runtime > **Security Settings** (see "Create Runtime" in the GreenNode docs):

| Setting | Recommendation |
|---|---|
| **IP Access Control** | Add **only the webhook proxy's source address** (its private IP in the VPC, as the runtime sees it) and your admin or CI range for tests. Empty means allow all. What source address a Private-mode runtime sees is not documented: verify with GreenNode, then test with `check_connectivity.sh` |
| **Inbound Identity** | Zalo's webhook calls carry no GreenNode token, and neither does the proxy. Enforcing IAM or JWT on the whole runtime therefore blocks the webhook. Unless GreenNode confirms a path exemption (verify), use **No authorization** together with IP Access Control, the Zalo secret (`ZALO_WEBHOOK_SECRET`) and `AGENT_API_KEY` for REST. Do not leave IP Access Control empty with No authorization |

Whether a Private-mode runtime still exposes a **public endpoint** is not documented: **verify with GreenNode**. If a public
URL exists, IP Access Control is what keeps the Internet away from it, and the webhook proxy is then the only intended path.

## 5. Outbound (egress)

The webhook proxy only carries traffic **in**. The reply goes **out** from the runtime: after the fast `200` ack, a worker thread calls Zalo directly
(`Agent Runtime --HTTPS sendMessage--> Zalo Bot Platform`). The runtime must therefore reach these destinations on **TCP 443**:

| Destination | Used for | Path |
|---|---|---|
| `bot-api.zaloplatforms.com` (`ZALO_API_BASE`) | Zalo `sendMessage` (the reply) and `getMe` (bot name) | Internet |
| The LLM endpoint: `maas-llm-aiplatform-hcm.api.vngcloud.vn` (`LLM_BASE_URL`), unless you use the Sidecar LLM Proxy | LLM calls | Internet (or the sidecar: verify) |
| AgentBase APIs: `agentbase.api.vngcloud.vn` (Memory) and `iam.api.vngcloud.vn` (IAM token for the gateway) | Memory, token | public hostnames |
| The Private MCP Gateway endpoint | tool calls | private network (Route CIDRs, verify) |
| Langfuse `http://<langfuse-private-ip>:3000` | traces | private network (Route CIDRs) |

**Verify with GreenNode: Private runtime outbound Internet egress.** The documentation says a Private runtime is not exposed to the public internet (inbound) and that Route CIDRs reach
customer subnets; it does not say whether the runtime keeps outbound Internet access. Check it first: `GET /ready` (section 6) shows `checks.zalo.bot` only when `getMe` succeeded, and a failed reply is
logged as `sent=False` with a `ConnectError`.

**Fallback if there is no egress (documented only, not implemented in this repository).** Run a forward proxy (for example tinyproxy or squid) on a vServer in the public subnet. Accept connections
only from the runtime's source range and allow only `CONNECT bot-api.zaloplatforms.com:443` (default deny). Then set on the runtime:

```
HTTPS_PROXY=http://<proxy-private-ip>:<port>
NO_PROXY=10.20.0.0/16,172.30.0.0/16,.agentbase-gateway.aiplatform.vngcloud.vn,agentbase.api.vngcloud.vn,iam.api.vngcloud.vn,maas-llm-aiplatform-hcm.api.vngcloud.vn
```

Leave `HTTP_PROXY` unset so plain-HTTP private targets such as Langfuse go direct. List in `NO_PROXY` only the GreenNode hosts that are reachable directly (and use the host of your own gateway Endpoint URL for the gateway suffix); the others must be allowed on the proxy as well.
The agent's HTTP clients read these variables by default: the agent code, the GreenNode SDK (a plain `httpx.Client`, by code inspection) and langchain-openai / openai (httpx). In a local test (httpx 0.28, langchain-openai 1.6)
`HTTPS_PROXY` was honoured and domain entries in `NO_PROXY` bypassed it. **CIDR entries in `NO_PROXY` work for `requests` (the Langfuse OTLP exporter) but not for httpx**, so use hostnames or exact IPs for HTTPS targets.
Not tested through a real proxy on a runtime, and whether the runtime accepts these variables is unverified.

## 6. Verify

1. The runtime reaches **ACTIVE**; read the logs in the console (no `LLM` / memory configuration errors).
2. From a vServer in the customer VPC (an address allowed by IP Access Control):

   ```bash
   RUNTIME_URL=http://<private-endpoint> ../check_connectivity.sh       # GET /health -> 200
   curl -s http://<private-endpoint>/ready                              # memory, gateway path and Zalo getMe (egress)
   ```

   `/ready` returns `200` when Memory and the gateway tools respond and `LLM_API_KEY` is set; `checks.zalo.bot` is the bot name when the `getMe` call to Zalo worked (egress); it never changes the `200` / `503` status. The result is cached for 5 minutes on success and 30 seconds on failure, so after fixing egress it recovers on its own within half a minute. The `getMe` call is not made at all when `ZALO_BOT_TOKEN` is unset.
3. Register the webhook through the proxy ([`../webhook-proxy`](../webhook-proxy/README.md)), then message the bot in Zalo.
4. The reply arrives, a booking appears in `list_bookings`, and a **trace** appears in Langfuse (opened over the VPN).
5. Gateway policy: the runtime's principal must be allowed to call the seven `restaurant__*` actions (set `DEBUG_OPS=1`
   temporarily to read it, then turn it off).

## Updates

Push a new image tag and update the runtime's image: each image update creates a new immutable version, and the default
endpoint points to the latest one. Changing environment variables restarts the runtime.
