# Webhook proxy (public ingress for Zalo)

The agent runs on AgentBase in **Private** network mode, so Zalo's servers cannot call it directly. This folder puts a
small **public reverse proxy in the customer VPC** that exposes **only `POST /webhook/zalo`** and forwards it to the
runtime's private endpoint. Everything else returns `404`.

```
Zalo Bot Platform (Internet)
   -> https://<WEBHOOK_DOMAIN>/webhook/zalo           TLS, 64 KB body limit, per-IP rate limit
   -> vServer with public IP, PUBLIC subnet, Caddy     only POST /webhook/zalo; all else 404
   -> private connection -> Agent Runtime (Private)    private endpoint (verify with GreenNode)
      the agent verifies X-Bot-Api-Secret-Token and returns 200 fast
```

The proxy carries only **inbound** events. The reply to the guest is sent by the runtime itself, directly to `https://bot-api.zaloplatforms.com`
(see [Outbound (egress)](../agent/README.md#5-outbound-egress)): it does not go back through this proxy.

The proxy is a narrow door, not the authentication. The agent still verifies Zalo's secret header on every event
(`403` on a wrong secret). `ZALO_WEBHOOK_SECRET` is mandatory on the runtime: without it the agent answers `503` instead of accepting unsigned events.

| File | Purpose |
|---|---|
| `Caddyfile` | Matches `POST /webhook/zalo` only, request size limit, rate limit, TLS, 404 for the rest |
| `Dockerfile` | Caddy built with the `caddy-ratelimit` module (stock Caddy has no rate limiting) |
| `docker-compose.yml`, `.env.example` | Runs the proxy on a vServer |

## Option A: vServer + Caddy (this folder)

### 1. Network

1. Create a vServer in a **public subnet** of the customer VPC and attach a **Floating (public) IP**. This is the only
   internet-facing VM in the deployment. Keep it minimal: no other services on it.
2. Create a DNS `A` record `zalo-webhook.<your-domain>` pointing to that public IP.
3. Security group, inbound:

   | Protocol | Port | Source | Purpose |
   |---|---|---|---|
   | TCP | `443` | `0.0.0.0/0` | Zalo webhook calls |
   | TCP | `80` | `0.0.0.0/0` | ACME certificate issuance and renewal (can be closed if you use your own certificate) |
   | TCP | `22` | VPN client range only | SSH |

   Outbound: the runtime private endpoint, DNS and the ACME CA (443). This sample does not filter by Zalo source IP
   because no documented Zalo address range was used. Ask Zalo whether a stable range exists before adding such a rule.
4. The proxy must reach the **runtime private endpoint**. That endpoint (hostname or IP, port, scheme) and the routing
   from a customer-VPC vServer to it are **not documented**: ask GreenNode, put the result in `RUNTIME_UPSTREAM`.
   Whether a Private-mode runtime still has a public endpoint is also not documented: **verify with GreenNode**, and
   if it does, restrict it with the runtime's *IP Access Control* (see [`../agent/README.md`](../agent/README.md)).

### 2. Run

```bash
git clone <repo> && cd sample-zalo-restaurant/deploy/webhook-proxy
cp .env.example .env
# edit .env: WEBHOOK_DOMAIN, RUNTIME_UPSTREAM (and optionally MAX_BODY, RATE_EVENTS)

docker compose up -d --build
docker compose logs -f proxy          # Caddy obtains the certificate on the first request/start
```

Settings in `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_DOMAIN` | required | Public DNS name; the certificate is issued for it |
| `RUNTIME_UPSTREAM` | required | Runtime private endpoint, `http://<runtime-private-endpoint>:8080` (the address is not documented: ask GreenNode) |
| `MAX_BODY` | `64KB` | Requests above this size get `413` |
| `RATE_EVENTS` | `6000` | Requests per minute per client IP, above that `429` |

The limit is keyed per client IP, and Zalo delivers **every guest's** message from its own servers, which are only a few
addresses. All guests therefore share one or a few counters: a small limit would answer real guests with `429`, and Zalo
retries a rejected event. `6000` per minute (100 per second) is a flood guard far above any realistic restaurant traffic;
it is not a per-guest limit, and the agent's own throughput is bounded by `ZALO_MAX_WORKERS`. Look at the access log after
a busy day and tune `RATE_EVENTS` from real traffic. The Zalo secret, not this limit, is what authenticates a call.

**Own certificate (enterprise CA or purchased):** mount it (`./certs`) and enable the `tls /certs/fullchain.pem
/certs/privkey.pem` line in the `Caddyfile`. **Header host:** `header_up Host {upstream_hostport}` sends the upstream's
host to the runtime (needed when the runtime endpoint routes by `Host`); verify what the private endpoint expects.

### 3. Check

```bash
PROXY_URL=https://zalo-webhook.<your-domain> ../check_connectivity.sh
# expected: GET / , /health , /api/info, POST /invocations, /a2a, GET and PUT /webhook/zalo -> 404
#           POST /webhook/zalo without the secret -> 403 (forwarded; the agent rejects it)
```

If the last check returns `502/503/504`, the proxy cannot reach the runtime private endpoint.

### 4. Register the webhook with Zalo

Zalo Bot Platform: [setWebhook](https://bot.zaloplatforms.com/docs/apis/setWebhook/). Use the **proxy** URL, not the
runtime URL:

```bash
curl -X POST "https://bot-api.zaloplatforms.com/bot${ZALO_BOT_TOKEN}/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://zalo-webhook.<your-domain>/webhook/zalo","secret_token":"<ZALO_WEBHOOK_SECRET>"}'
# Expected: "verification":{"ok":true,"outcome":"webhook.ok"}
```

`<ZALO_WEBHOOK_SECRET>` must be the same value you set on the runtime. Then message the bot in the Zalo app. Re-check
with `getWebhookInfo` and `testWebhook`. Run this from an admin machine: the token and secret are credentials, do not
paste them into shared chats or shell history you keep (use `read -s`).

### Operations

| Task | Command |
|---|---|
| Logs (JSON access log) | `docker compose logs -f proxy` |
| Update Caddy | `docker compose build --pull && docker compose up -d` |
| Change limits | edit `.env`, `docker compose up -d` |
| Certificates | stored in the `caddy_data` volume: do not delete it |

## Option B: GreenNode vLB / ALB with a path rule

An **Application Load Balancer** can replace the vServer. The GreenNode ALB documents routing by host and path,
SSL/TLS termination, X-Forwarded headers and health checks. The VKS Ingress docs show TLS termination on port 443 and
`pathType: Exact` rules.

Sketch:

1. Create an ALB with a **public** address in the customer VPC (scheme: external; verify the option name in the console).
2. HTTPS listener on 443 with your certificate (certificate upload as described in the vLB docs).
3. A pool whose member is the runtime private endpoint, and an L7 policy **path equals `/webhook/zalo`** forwarding to
   that pool. The Zalo webhook URL is then `https://<alb-domain>/webhook/zalo`.
4. Every other request must not reach the runtime.

Open questions for GreenNode (the public documentation reviewed does not answer them, so do not rely on Option B until
confirmed):

- Can the runtime's private endpoint be an ALB pool member (an address outside the VPC's vServers)?
- Is there a **default fixed `404` response** (or "reject") for requests that match no policy? Without it, unmatched paths
  fall to the default pool and the runtime would be exposed.
- Can the L7 policy match the **HTTP method** (`POST`) as well as the path?
- Does the ALB offer **request size limits** and **rate limiting**? If not, the Caddy proxy (Option A) is the safer choice,
  or place Option A behind the ALB.

## Hardening checklist

- [ ] Only ports 443 (and 80 for ACME) are open to the Internet; SSH only from the VPN range.
- [ ] `check_connectivity.sh` with `PROXY_URL` passes: every other path returns 404.
- [ ] `ZALO_WEBHOOK_SECRET` is set on the runtime and the same value is registered with `setWebhook`.
- [ ] The runtime's *IP Access Control* allows only this proxy's private source address (verify what the runtime sees).
- [ ] The vServer is patched and runs nothing else; Docker logs rotate (configured in `docker-compose.yml`).
- [ ] `MAX_BODY` and `RATE_EVENTS` reviewed against real Zalo traffic.
