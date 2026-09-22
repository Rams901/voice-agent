#!/usr/bin/env bash
#
# Serve the mock DMS tool server behind a public tunnel, and point Retell at it.
#
# Everything the agent is allowed to *do* lives in tool_server/, which is
# deliberately boring: no model, same input -> same output. Retell has to call it
# over the public internet, so this script starts it, opens a cloudflared tunnel,
# waits until the tunnel genuinely reaches the server, and (with --deploy) re-
# deploys the flow with that URL baked into the six tool definitions.
#
#   ./scripts/dev_tunnel.sh              # serve + tunnel, print the URL
#   ./scripts/dev_tunnel.sh --deploy     # ...then re-deploy the flow/agent
#   ./scripts/dev_tunnel.sh --deploy --simulate
#
# Both processes are children of this script and are killed when you Ctrl-C, so
# nothing is left listening and no tunnel is left open.
#
# TWO WAYS TO TUNNEL
#
#   1. Quick tunnel (default). Gets a random hostname and dies with this script.
#      Zero setup, but the deployed flow keeps pointing at the dead hostname
#      afterwards — which is the "ENOTFOUND <random>.trycloudflare.com" and
#      "example.ngrok.app -> 404" class of failure this script exists to fix.
#      Re-run with --deploy every time, or the agent's tools are unreachable.
#
#   2. Named tunnel (set CLOUDFLARE_TUNNEL_TOKEN + TOOLS_PUBLIC_URL). One stable
#      hostname on your own domain, configured once in the Cloudflare dashboard.
#      Deploy once and it keeps working. This is the mode to use for anything
#      you want to test more than once.
#
#      Cloudflare dashboard -> Zero Trust -> Networks -> Tunnels -> Create a
#      tunnel -> Cloudflared -> copy the token. Then add a Public Hostname
#      route pointing at http://localhost:8000, and set in .env:
#
#        CLOUDFLARE_TUNNEL_TOKEN=eyJh...
#        TOOLS_PUBLIC_URL=https://tools.yourdomain.com

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${TOOLS_PORT:-8000}"
DEPLOY=0
SIMULATE=0
for arg in "$@"; do
  case "$arg" in
    --deploy)   DEPLOY=1 ;;
    --simulate) SIMULATE=1 ;;
    -h|--help)  sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# cloudflared usually fails by exiting with a usage dump or a config complaint.
# "cloudflared exited" alone sends you hunting through a temp file; show the
# reason inline instead.
die_tunnel() {
  local reason="$1" log="${2:-}"
  printf '\033[31m%s\033[0m\n' "$reason" >&2
  if [[ -n "$log" && -s "$log" ]]; then
    echo "--- last lines of cloudflared output ---" >&2
    tail -n 6 "$log" | sed 's/^/  /' >&2
  fi
  exit 1
}

command -v cloudflared >/dev/null 2>&1 \
  || die "cloudflared not found.  Install with: brew install cloudflared"
[[ -f .env ]] \
  || die ".env not found.  Copy .env.example to .env and set RETELL_API_KEY."

SERVER_LOG="$(mktemp -t frontdesk-server)"
TUNNEL_LOG="$(mktemp -t frontdesk-tunnel)"
SERVER_PID=""
TUNNEL_PID=""

cleanup() {
  bold "
Shutting down"
  [[ -n "$TUNNEL_PID" ]] && kill "$TUNNEL_PID" 2>/dev/null || true
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

healthy() { curl -fsS --max-time 5 "$1/health" >/dev/null 2>&1; }

# Does the tunnel actually serve the tool server? This is the question worth
# gating on, and it must not be answered by this machine's resolver: macOS
# negative-caches new trycloudflare hostnames, so a healthy tunnel can look dead
# from here while being perfectly reachable for Retell. `frontdesk.net` resolves
# through DoH when the local lookup fails — one implementation, shared with
# `cli doctor`.
tunnel_answers() {
  python -m frontdesk.net "$1/health" >/dev/null 2>&1
}

# --- 1. the tool server ----------------------------------------------------- #
if healthy "http://127.0.0.1:$PORT"; then
  bold "Tool server already listening on :$PORT (leaving it alone)"
else
  bold "Starting tool server on :$PORT"
  python -m uvicorn tool_server.app:app --host 127.0.0.1 --port "$PORT" \
    --log-level warning >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!
  for _ in $(seq 1 40); do
    healthy "http://127.0.0.1:$PORT" && break
    kill -0 "$SERVER_PID" 2>/dev/null || die "Tool server died on startup:
$(cat "$SERVER_LOG")"
    sleep 0.5
  done
  healthy "http://127.0.0.1:$PORT" \
    || die "Tool server not healthy after 20s.  Log: $SERVER_LOG"
  echo "  http://127.0.0.1:$PORT/health -> ok"
fi

# --- 2. the tunnel ---------------------------------------------------------- #
# Read config through the project's own .env loader, so the script agrees with
# the Python side about what is configured (`.env` is not sourced as bash: a
# value with a '#' or a quote in it would silently truncate).
env_var() {
  python - "$1" <<'PY'
import os, sys
from frontdesk import config  # noqa: F401 — importing loads .env as a side effect
print(os.environ.get(sys.argv[1], "").strip())
PY
}

NAMED_TOKEN="$(env_var CLOUDFLARE_TUNNEL_TOKEN)"
FIXED_URL="$(env_var TOOLS_PUBLIC_URL)"
FIXED_URL="${FIXED_URL%/}"
URL=""

if [[ -n "$NAMED_TOKEN" ]]; then
  # A named tunnel's hostname is configured in the Cloudflare dashboard, so it
  # cannot be scraped from cloudflared's log the way a quick tunnel's can.
  [[ -n "$FIXED_URL" ]] || die "CLOUDFLARE_TUNNEL_TOKEN is set but TOOLS_PUBLIC_URL is not.

A named tunnel's hostname lives in the Cloudflare dashboard, not in cloudflared's
output, so the script cannot discover it. Set the public hostname you configured
on the tunnel, e.g.

    TOOLS_PUBLIC_URL=https://tools.yourdomain.com"

  URL="$FIXED_URL"

  if tunnel_answers "$URL"; then
    # Already up. Could be an earlier run, or a real service (launchd, docker).
    # Either way it is not ours, so leave it running and do not kill it on exit.
    bold "Named tunnel already serving $URL (leaving it alone)"
  else
    bold "Opening named tunnel"
    # No --no-autoupdate here: unlike `cloudflared tunnel --url`, the `tunnel
    # run` subcommand rejects it ("flag provided but not defined"), and the
    # failure is a usage dump rather than an obvious error.
    cloudflared tunnel run --token "$NAMED_TOKEN" \
      >"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
  fi
else
  bold "Opening cloudflared quick tunnel (hostname will change on exit)"
  cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate \
    >"$TUNNEL_LOG" 2>&1 &
  TUNNEL_PID=$!

  for _ in $(seq 1 60); do
    URL="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" \
           2>/dev/null | head -1 || true)"
    [[ -n "$URL" ]] && break
    kill -0 "$TUNNEL_PID" 2>/dev/null || die "cloudflared exited.  Log: $TUNNEL_LOG"
    sleep 1
  done
  [[ -n "$URL" ]] || die_tunnel "No tunnel hostname after 60s." "$TUNNEL_LOG"
fi

# A published hostname is not a reachable one. Wait for a real 200 before
# deploying, because shipping a URL that does not yet answer is exactly how you
# get silent 404s in production.
bold "Waiting for $URL to answer"
attempt=0
until tunnel_answers "$URL"; do
  kill -0 "$TUNNEL_PID" 2>/dev/null \
    || die_tunnel "cloudflared exited while waiting for $URL." "$TUNNEL_LOG"

  attempt=$((attempt + 1))
  if (( attempt >= 60 )); then
    die_tunnel "Tunnel never answered (120s): $URL was checked both directly and
through Cloudflare's public resolver.  Is a Public Hostname route for $URL
pointing at http://localhost:$PORT on this tunnel?" "$TUNNEL_LOG"
  fi

  (( attempt % 15 == 0 )) && echo "  still waiting ($((attempt * 2))s)"
  sleep 2
done

# The note from `frontdesk.net` says whether the local resolver was bypassed,
# which is worth surfacing: it is the difference between "your browser will
# work" and "your browser may not, but Retell will".
python -m frontdesk.net "$URL/health" | sed 's/^/  /' || true

bold "Public URL : $URL"
export TOOLS_BASE_URL="$URL"

# --- 3. verify, then hand it to the flow ------------------------------------ #
if [[ "$DEPLOY" == "1" ]]; then
  bold "Doctor"
  python -m frontdesk.cli doctor

  # Reuse what is already deployed rather than trusting defaults: an unset
  # TOOLS_BASE_URL on a PATCH sends "tools": [] and clears every tool.
  MODEL="$(python -c 'import json;print(json.load(open(".frontdesk.json"))["model"])' 2>/dev/null || echo gpt-4.1)"
  VOICE="$(python -c 'import json;print(json.load(open(".frontdesk.json"))["voice_id"])' 2>/dev/null || echo '')"

  bold "Deploying with model=$MODEL voice=${VOICE:-<account default>}"
  if [[ -n "$VOICE" ]]; then
    python -m frontdesk.cli deploy --model "$MODEL" --voice "$VOICE"
  else
    python -m frontdesk.cli deploy --model "$MODEL"
  fi
else
  bold "Tools are reachable at $URL, but this run did not re-deploy."
  echo "  If the deployed flow references a different URL, re-deploy:"
  echo "    ./scripts/dev_tunnel.sh --deploy"
fi

if [[ "$SIMULATE" == "1" ]]; then
  bold "Simulate"
  python -m frontdesk.cli simulate
fi

# --- 4. stay up ------------------------------------------------------------- #
bold "
Live. Tools: $URL"
if [[ -n "$SERVER_PID" || -n "$TUNNEL_PID" ]]; then
  echo "  Ctrl-C to tear down what this script started."
  wait
else
  # Nothing here is ours. With a named tunnel this is the expected steady state:
  # the tunnel is a service, and this script is just a client of it.
  echo "  Server and tunnel were already running; nothing for this script to own."
fi
