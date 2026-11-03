#!/usr/bin/env bash
# On the server: write deploy/.env and start the stack.
#   bash deploy/setup-env.sh <site-host> < keys
# The keys arrive on standard input as KEY=value lines (LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
# VULTR_INFERENCE_KEY), so they never sit in a command line or a log. The database password and
# the cookie key are made here and never leave the server.
set -euo pipefail
cd "$(dirname "$0")"
umask 077
host="${1:?site host, e.g. 203-0-113-10.sslip.io}"
touch .env
put() {
  grep -v "^$1=" .env > .env.new || true
  printf '%s=%s\n' "$1" "$2" >> .env.new
  mv .env.new .env
}
has() { grep -q "^$1=." .env; }
put SITE_HOST "$host"
has POSTGRES_PASSWORD || put POSTGRES_PASSWORD "$(openssl rand -hex 24)"
has SECRET_KEY || put SECRET_KEY "$(openssl rand -hex 32)"
has COPY_MINUTES || put COPY_MINUTES 120
has VULTR_INFERENCE_URL || put VULTR_INFERENCE_URL https://api.vultrinference.com/v1
if [ ! -t 0 ]; then
  while IFS='=' read -r k v; do
    case "$k" in
      LLM_BASE_URL|LLM_API_KEY|LLM_MODEL|VULTR_INFERENCE_KEY) [ -n "$v" ] && put "$k" "$v" ;;
    esac
  done
fi
echo "deploy/.env has: $(cut -d= -f1 .env | tr '\n' ' ')"
docker compose up -d --build
docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
