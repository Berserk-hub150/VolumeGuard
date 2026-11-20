#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
project="vgdemo-$(python3 -c 'import uuid; print(uuid.uuid4().hex)')"
dc() { local file="$1"; shift; docker compose -p "$project" -f "$file" "$@"; }
cleanup() {
  dc changed.yaml down -v >/dev/null
  dc before.yaml down -v >/dev/null
}
trap cleanup EXIT
sql() { dc "$1" exec -T db mariadb --user=root --batch --skip-column-names -e "$2"; }
start() {
  dc "$1" up -d --force-recreate
  for attempt in {1..120}; do
    if dc "$1" exec -T db healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then return; fi
    sleep 1
  done
  echo "MariaDB did not become ready." >&2
  return 1
}
start before.yaml
sql before.yaml 'CREATE DATABASE vg_demo; CREATE TABLE vg_demo.courses (id INT PRIMARY KEY); INSERT INTO vg_demo.courses VALUES (1),(2),(3),(4),(5),(6),(7),(8),(9),(10),(11),(12);'
before="$(sql before.yaml 'SELECT COUNT(*) FROM vg_demo.courses;')"
echo "Original volume: $before sample courses"
test "$before" = 12
start changed.yaml
present="$(sql changed.yaml "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='vg_demo';")"
echo "Changed volume: $present matching demo databases"
test "$present" = 0
start before.yaml
restored="$(sql before.yaml 'SELECT COUNT(*) FROM vg_demo.courses;')"
echo "Original volume reattached: $restored sample courses"
test "$restored" = 12
