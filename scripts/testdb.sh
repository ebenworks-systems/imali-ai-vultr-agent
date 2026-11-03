#!/usr/bin/env bash
# Run a command against a throwaway Postgres made for this run, then remove it.
#
#   scripts/testdb.sh <command> [args ...]
#
# The command gets TEST_DATABASE_URL. It uses the Postgres server binaries on this machine
# (pg_ctl on PATH or under /usr/lib/postgresql/*/bin), or Docker's postgres:16-alpine when
# there are none. Nothing listens on a network port: the server takes a Unix socket in a
# temporary folder.
set -euo pipefail

run_dir=$(mktemp -d "${TMPDIR:-/tmp}/month-end-testdb.XXXXXX")
cleanup() {
  if [[ -n "${pg_bin:-}" && -d "$run_dir/data" ]]; then
    "$pg_bin/pg_ctl" -D "$run_dir/data" -m immediate stop >/dev/null 2>&1 || true
  fi
  if [[ -n "${container:-}" ]]; then
    docker rm -f "$container" >/dev/null 2>&1 || true
  fi
  rm -rf "$run_dir"
}
trap cleanup EXIT

pg_bin=""
if command -v pg_ctl >/dev/null 2>&1; then
  pg_bin=$(dirname "$(command -v pg_ctl)")
else
  for d in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V -r); do
    if [[ -x "$d/pg_ctl" ]]; then pg_bin="$d"; break; fi
  done
fi

if [[ -n "$pg_bin" ]]; then
  "$pg_bin/initdb" -D "$run_dir/data" -U postgres -A trust --no-sync >/dev/null
  "$pg_bin/pg_ctl" -D "$run_dir/data" -l "$run_dir/log" -w \
    -o "-c listen_addresses='' -k $run_dir -c fsync=off -c synchronous_commit=off -c full_page_writes=off" \
    start >/dev/null
  "$pg_bin/createdb" -h "$run_dir" -U postgres month_end_test
  export TEST_DATABASE_URL="postgresql://postgres@/month_end_test?host=$run_dir"
elif command -v docker >/dev/null 2>&1; then
  container="month-end-testdb-$$"
  docker run -d --rm --name "$container" -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=month_end_test \
    -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
  port=$(docker port "$container" 5432/tcp | head -1 | sed 's/.*://')
  for _ in $(seq 1 60); do
    if docker exec "$container" pg_isready -U postgres >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  export TEST_DATABASE_URL="postgresql://postgres@127.0.0.1:$port/month_end_test"
else
  echo "testdb.sh: no Postgres server binaries and no Docker found" >&2
  exit 1
fi

"$@"
