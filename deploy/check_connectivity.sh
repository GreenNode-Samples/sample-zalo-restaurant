#!/usr/bin/env bash
# Connectivity and exposure checks for the Zalo Restaurant deployment.
# Run from a vServer INSIDE the customer VPC (for example the one that hosts the MCP server, or a
# bastion reached over the VPN). Needs only bash and curl. Every section is optional: it runs only
# when its variables are set.
#
#   MCP server      MCP_HOST=10.20.1.20 [MCP_PORT=8443] [MCP_SCHEME=https] [MCP_API_KEY=<key>]
#                   GET /health, then (with a key) authenticated POST /mcp tools/list (expects 7 tools),
#                   and a request without a key must be rejected (401).
#   Langfuse        LANGFUSE_URL=http://10.20.1.10:3000
#                   GET /api/public/health (prints the Langfuse version).
#   Agent runtime   RUNTIME_URL=http://<private-endpoint>        (endpoint: verify with GreenNode)
#                   GET /health, then GET /ready (memory, gateway path, and the Zalo getMe call, which shows
#                   whether the runtime has outbound egress to bot-api.zaloplatforms.com). When the runtime
#                   sets AGENT_API_KEY, pass it too: AGENT_API_KEY=<key> (sent as X-API-Key to /ready).
#   Webhook proxy   PROXY_URL=https://zalo-webhook.example.com
#                   Only POST /webhook/zalo may pass. Other paths and methods must return 404.
#                   A POST without the Zalo secret must be rejected by the agent (403).
#
# Other variables: INSECURE=1 (accept internal or self-signed certificates, for example Caddy's internal CA),
# TIMEOUT=<seconds, default 5>. For the plain-HTTP alternative set MCP_SCHEME=http MCP_PORT=8080.
#
# Example:
#   MCP_HOST=10.20.1.20 MCP_API_KEY=<key> INSECURE=1 LANGFUSE_URL=http://10.20.1.10:3000 \
#   PROXY_URL=https://zalo-webhook.example.com ./check_connectivity.sh
#
# Exit code: 0 all executed checks passed, 1 at least one failed, 2 usage error.
set -u

MCP_HOST="${MCP_HOST:-}"
MCP_PORT="${MCP_PORT:-8443}"
MCP_SCHEME="${MCP_SCHEME:-https}"
LANGFUSE_URL="${LANGFUSE_URL:-}"
RUNTIME_URL="${RUNTIME_URL:-}"
PROXY_URL="${PROXY_URL:-}"
TIMEOUT="${TIMEOUT:-5}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi
if [[ -z "$MCP_HOST$LANGFUSE_URL$RUNTIME_URL$PROXY_URL" ]]; then
  echo "Nothing to check: set at least one of MCP_HOST, LANGFUSE_URL, RUNTIME_URL, PROXY_URL (see --help)."
  exit 2
fi
command -v curl >/dev/null 2>&1 || { echo "FAIL: curl is required"; exit 2; }

CURL_OPTS=(-sS --max-time "$TIMEOUT")
[[ "${INSECURE:-0}" == "1" ]] && CURL_OPTS+=(-k)

fails=0
passes=0
pass() { printf 'PASS  %s\n' "$1"; passes=$((passes + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; [[ -n "${2:-}" ]] && printf '      -> %s\n' "$2"; fails=$((fails + 1)); }
skip() { printf 'SKIP  %s\n' "$1"; }
note() { printf 'NOTE  %s\n' "$1"; }

# http_code <method> <url> [extra curl args...]  -> prints the status code ("000" when unreachable)
http_code() {
  local method="$1" url="$2"
  shift 2
  curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' -X "$method" "$@" "$url" 2>/dev/null || true
}

# ---------------------------------------------------------------- MCP server
if [[ -n "$MCP_HOST" ]]; then
  BASE="${MCP_SCHEME}://${MCP_HOST}:${MCP_PORT}"
  echo "== MCP server: ${BASE}/mcp"

  code=$(http_code GET "${BASE}/health")
  if [[ "$code" == "200" ]]; then
    pass "[mcp] GET /health -> 200"
  elif [[ "$code" == "000" || -z "$code" ]]; then
    fail "[mcp] GET /health -> no response" \
         "check the route, the security group (source = gateway range / your host), that the container is up, and TLS settings (INSECURE=1 for an internal CA)"
  else
    fail "[mcp] GET /health -> ${code}" "unexpected status: check scheme and port (${MCP_SCHEME}:${MCP_PORT})"
  fi

  MCP_BODY='{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
  MCP_HDRS=(-H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")

  code=$(http_code POST "${BASE}/mcp" "${MCP_HDRS[@]}" -d "$MCP_BODY")
  case "$code" in
    401|503) pass "[mcp] POST /mcp without a key -> ${code} (rejected, fail-closed)" ;;
    000)     fail "[mcp] POST /mcp without a key -> no response" "server unreachable" ;;
    *)       fail "[mcp] POST /mcp without a key -> ${code}" "expected 401 (or 503 when MCP_API_KEYS is unset): the server must not accept anonymous calls" ;;
  esac

  if [[ -z "${MCP_API_KEY:-}" ]]; then
    skip "[mcp] authenticated tools/list (set MCP_API_KEY=<key>)"
  else
    resp=$(curl "${CURL_OPTS[@]}" -X POST "${BASE}/mcp" -H "X-Api-Key: ${MCP_API_KEY}" "${MCP_HDRS[@]}" \
           -d "$MCP_BODY" -w '\n%{http_code}' 2>&1)
    code=$(printf '%s' "$resp" | tail -n1)
    out=$(printf '%s' "$resp" | sed '$d')
    case "$code" in
      200)
        if printf '%s' "$out" | grep -q '"tools"'; then
          n=$(printf '%s' "$out" | grep -o '"name"' | wc -l | tr -d ' ')
          if [[ "$n" == "7" ]]; then
            pass "[mcp] POST /mcp tools/list with key -> 200 (7 tools)"
          else
            fail "[mcp] tools/list returned ${n} tool names (expected 7)" "${out:0:200}"
          fi
        else
          fail "[mcp] POST /mcp -> 200 but no \"tools\" in the response" "${out:0:200}"
        fi ;;
      401) fail "[mcp] POST /mcp with key -> 401" "the key does not match MCP_API_KEYS on the server" ;;
      503) fail "[mcp] POST /mcp with key -> 503" "the server has no MCP_API_KEYS configured (fail-closed)" ;;
      000) fail "[mcp] POST /mcp with key -> no response" "server unreachable" ;;
      *)   fail "[mcp] POST /mcp with key -> ${code}" "${out:0:200}" ;;
    esac
  fi
fi

# ---------------------------------------------------------------- Langfuse
if [[ -n "$LANGFUSE_URL" ]]; then
  LF="${LANGFUSE_URL%/}"
  echo "== Langfuse: ${LF}"
  resp=$(curl "${CURL_OPTS[@]}" -w '\n%{http_code}' "${LF}/api/public/health" 2>&1)
  code=$(printf '%s' "$resp" | tail -n1)
  out=$(printf '%s' "$resp" | sed '$d')
  if [[ "$code" == "200" ]]; then
    ver=$(printf '%s' "$out" | sed -n 's/.*"version"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
    pass "[langfuse] GET /api/public/health -> 200 (version ${ver:-unknown}; verify it is a v3.x release that accepts OpenTelemetry)"
  elif [[ "$code" == "000" || -z "$code" ]]; then
    fail "[langfuse] GET /api/public/health -> no response" \
         "check the private IP, the security group (port 3000 from this host), and that langfuse-web is running"
  else
    fail "[langfuse] GET /api/public/health -> ${code}" "${out:0:200}"
  fi
fi

# ---------------------------------------------------------------- Agent runtime (private endpoint)
if [[ -n "$RUNTIME_URL" ]]; then
  RT="${RUNTIME_URL%/}"
  echo "== Agent runtime: ${RT}"
  code=$(http_code GET "${RT}/health")
  if [[ "$code" == "200" ]]; then
    pass "[agent] GET /health -> 200"
    # Deep readiness: Memory, the gateway tools and Zalo getMe (an outbound call from the runtime).
    ready_hdrs=()
    [[ -n "${AGENT_API_KEY:-}" ]] && ready_hdrs=(-H "X-API-Key: ${AGENT_API_KEY}")
    resp=$(curl "${CURL_OPTS[@]}" --max-time $((TIMEOUT * 4)) ${ready_hdrs[@]+"${ready_hdrs[@]}"} -w '\n%{http_code}' "${RT}/ready" 2>&1)
    code=$(printf '%s' "$resp" | tail -n1)
    out=$(printf '%s' "$resp" | sed '$d')
    case "$code" in
      200) pass "[agent] GET /ready -> 200 (memory and gateway tools respond, LLM key set)" ;;
      401) fail "[agent] GET /ready -> 401" "the runtime sets AGENT_API_KEY: run the script with AGENT_API_KEY=<key>" ;;
      503) fail "[agent] GET /ready -> 503 (degraded)" "${out:0:300}" ;;
      *)   fail "[agent] GET /ready -> ${code:-no response}" "${out:0:200}" ;;
    esac
    bot=$(printf '%s' "$out" | sed -n 's/.*"zalo"[^}]*"bot"[ ]*:[ ]*"\([^"]*\)".*/\1/p')
    if [[ -n "$bot" ]]; then
      pass "[agent] /ready: Zalo getMe ok (bot '${bot}'): outbound egress to bot-api.zaloplatforms.com works"
    else
      note "[agent] /ready: zalo.bot is empty: ZALO_BOT_TOKEN is not set, or getMe failed (no outbound egress? verify with GreenNode). A failure is cached for 30 seconds: run the check again after fixing"
    fi
  elif [[ "$code" == "000" || -z "$code" ]]; then
    fail "[agent] GET /health -> no response" \
         "check the runtime status, its VPC/Subnet/Route CIDRs, and the security group; the private endpoint address is not documented: verify with GreenNode"
  else
    fail "[agent] GET /health -> ${code}" "Inbound Identity or IP Access Control on the runtime may be rejecting this host"
  fi
fi

# ---------------------------------------------------------------- Webhook proxy
if [[ -n "$PROXY_URL" ]]; then
  PX="${PROXY_URL%/}"
  echo "== Webhook proxy: ${PX}"

  reachable=1
  code=$(http_code GET "${PX}/")
  if [[ "$code" == "000" || -z "$code" ]]; then
    fail "[proxy] ${PX} -> no response" "check DNS, the public IP, security group 80/443 and the TLS certificate (INSECURE=1 to skip verification)"
    reachable=0
  fi

  if [[ $reachable -eq 1 ]]; then
    for spec in "GET /" "GET /health" "GET /api/info" "POST /invocations" "POST /a2a" "GET /webhook/zalo" "PUT /webhook/zalo"; do
      method="${spec%% *}"
      path="${spec#* }"
      code=$(http_code "$method" "${PX}${path}" -d '{}')
      if [[ "$code" == "404" ]]; then
        pass "[proxy] ${method} ${path} -> 404 (not exposed)"
      else
        fail "[proxy] ${method} ${path} -> ${code}" "only POST /webhook/zalo may be forwarded; every other request must return 404"
      fi
    done

    code=$(http_code POST "${PX}/webhook/zalo" -H "Content-Type: application/json" -d '{"event_name":"connectivity.check"}')
    case "$code" in
      403) pass "[proxy] POST /webhook/zalo without the secret -> 403 (forwarded, rejected by the agent)" ;;
      404) fail "[proxy] POST /webhook/zalo -> 404" "the route is not forwarded: check the Caddyfile / load balancer path rule" ;;
      502|503|504) fail "[proxy] POST /webhook/zalo -> ${code}" "the proxy cannot reach the runtime private endpoint (check RUNTIME_UPSTREAM, routes and security groups), or the runtime answered 503 because ZALO_BOT_TOKEN / ZALO_WEBHOOK_SECRET is not set" ;;
      429) fail "[proxy] POST /webhook/zalo -> 429" "rate limit hit: wait a minute and retry" ;;
      2*)  fail "[proxy] POST /webhook/zalo without the secret -> ${code}" "the agent accepted an unsigned request: set ZALO_WEBHOOK_SECRET on the runtime" ;;
      *)   fail "[proxy] POST /webhook/zalo -> ${code:-no response}" "unexpected status" ;;
    esac
  fi
fi

echo "-----------------------------------------------"
if [[ $fails -eq 0 ]]; then
  echo "RESULT: PASS (${passes} checks)"
  exit 0
fi
echo "RESULT: FAIL (${fails} failed, ${passes} passed)"
exit 1
