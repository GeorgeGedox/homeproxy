#!/usr/bin/env bash
# Boot a built image and check it. Usage: smoke.sh <image> <platform>
#
# Proves: HAProxy serves the first-boot placeholder, /api/v1/healthz answers,
# HAProxy runs as the unprivileged service user, the bundled binaries run on the
# target architecture, killing or stopping the API leaves HAProxy serving, and
# default.pem is created at boot only when missing, and a boot after a cut-short
# apply uses the last promoted release.
# Host ports are chosen by Docker, so parallel checkouts never clash.
set -euo pipefail

image="${1:?usage: smoke.sh <image> <platform>}"
platform="${2:?usage: smoke.sh <image> <platform>}"
name="homeproxy-smoke-${COMPOSE_PROJECT_NAME:-local}-${platform##*/}-$$"
s6=/command

cleanup() {
  if [ "${failed:-1}" -ne 0 ]; then
    echo "--- container log (last 60 lines) ---" >&2
    docker logs --tail 60 "$name" >&2 || true
  fi
  # -v removes the anonymous /data volume the image declares.
  docker rm -f -v "$name" >/dev/null 2>&1 || true
}
failed=1
trap cleanup EXIT

fail() {
  echo "smoke ($platform): FAIL: $*" >&2
  exit 1
}

step() { echo "smoke ($platform): $*"; }

# Print the HTTP status of a URL, or 000 when nothing answers.
status() {
  curl -ks -o /dev/null -m 3 -w '%{http_code}' "$1" || true
}

# Wait until a URL answers 200, for at most the given number of seconds.
wait_for_200() {
  local url="$1" limit="$2" i
  for ((i = 0; i < limit; i++)); do
    if [ "$(status "$url")" = "200" ]; then
      return 0
    fi
    sleep 1
  done
  return 1
}

host_port() {
  docker port "$name" "$1" | head -n 1 | sed 's/.*://'
}

exec_in() {
  docker exec "$name" "$@"
}

step "starting $image"
docker run -d --name "$name" --platform "$platform" \
  -p 127.0.0.1::80 -p 127.0.0.1::8443 "$image" >/dev/null

http_url="http://127.0.0.1:$(host_port 80/tcp)"
# The API serves HTTPS with its self-signed certificate (-k in curl).
api_url="https://127.0.0.1:$(host_port 8443/tcp)/api/v1/healthz"

step "placeholder and healthz"
wait_for_200 "$http_url/" 90 || fail "HAProxy placeholder did not answer 200"
curl -s -m 3 "$http_url/" | grep -q 'HomeProxy' || fail "placeholder page has no HomeProxy text"
wait_for_200 "$api_url" 60 || fail "/api/v1/healthz did not answer 200"
curl -ks -m 3 "$api_url" | grep -q '"status":"ok"' || fail "healthz body is not status ok"

step "HAProxy runs unprivileged and owns the master socket"
pid=$(exec_in "$s6/s6-svstat" -o pid /run/service/haproxy | tr -d '[:space:]')
uid=$(exec_in stat -c %u "/proc/$pid")
[ "$uid" != "0" ] || fail "HAProxy master runs as root"
exec_in test -S /run/haproxy/master.sock || fail "no master socket"
exec_in test -S /run/haproxy/admin.sock || fail "no admin socket"
[ "$(exec_in readlink /data/generated)" = "releases/placeholder" ] || fail "/data/generated is not the placeholder release"

step "the API and the DNS server run as homeproxy, and private data is 0700"
for service in api dns; do
  pid=$(exec_in "$s6/s6-svstat" -o pid "/run/service/$service" | tr -d '[:space:]')
  [ "$(exec_in stat -c %U "/proc/$pid")" = "homeproxy" ] || fail "$service does not run as homeproxy"
done
# Keys, the ACME account, the bouncer's secrets, and stored logs with client addresses and URLs.
for dir in secrets acme tls crowdsec state/logs; do
  [ "$(exec_in stat -c '%a %U' "/data/$dir")" = "700 homeproxy" ] || fail "/data/$dir is not 0700 homeproxy"
done

step "bundled binaries"
exec_in /usr/local/bin/lego --version >/dev/null || fail "lego does not run"
exec_in /usr/local/bin/crowdsec-spoa-bouncer --version >/dev/null || fail "SPOA bouncer does not run"
for page in ban.html ban-with-contact.html captcha.html; do
  exec_in test -s "/var/lib/crowdsec-haproxy-spoa-bouncer/html/$page" || fail "missing bouncer page $page"
done

step "built dashboard"
exec_in test -s /opt/homeproxy/ui/index.html || fail "missing built dashboard at /opt/homeproxy/ui"

step "killing the API leaves HAProxy serving"
exec_in "$s6/s6-svc" -k /run/service/api
[ "$(status "$http_url/")" = "200" ] || fail "HAProxy stopped serving after the API was killed"
wait_for_200 "$api_url" 30 || fail "s6 did not restart the API"

step "stopping the API leaves HAProxy serving"
exec_in "$s6/s6-svc" -d /run/service/api
sleep 3
[ "$(status "$api_url")" != "200" ] || fail "API still answers after it was stopped"
[ "$(status "$http_url/")" = "200" ] || fail "HAProxy stopped serving while the API was down"
exec_in "$s6/s6-svc" -u /run/service/api
wait_for_200 "$api_url" 30 || fail "API did not come back"

# The generated config names default.pem, so init-data must make it before HAProxy starts.
# Docker may hand out new host ports on restart, so read them again.
restart_and_wait() {
  docker restart -t 20 "$name" >/dev/null
  http_url="http://127.0.0.1:$(host_port 80/tcp)"
  wait_for_200 "$http_url/" 90 || fail "HAProxy did not serve after a restart"
}

step "restart keeps default.pem, and recreates it when missing"
pem=/data/certs/default.pem
before=$(exec_in sha256sum "$pem")
restart_and_wait
[ "$(exec_in sha256sum "$pem")" = "$before" ] || fail "restart replaced an existing default.pem"
exec_in rm "$pem"
restart_and_wait
[ "$(exec_in stat -c '%a %U' "$pem")" = "600 homeproxy" ] || fail "default.pem is not 0600 homeproxy"
exec_in grep -q 'BEGIN PRIVATE KEY' "$pem" || fail "default.pem has no private key"
exec_in grep -q 'BEGIN CERTIFICATE' "$pem" || fail "default.pem has no certificate"
[ -z "$(exec_in find /data/certs -name '.default*')" ] || fail "init-data left a temp file in /data/certs"

# The placeholder never loads default.pem, so check HAProxy accepts it in the form the generated config uses.
docker exec -i "$name" sh -c 'cat >/tmp/default-pem-check.cfg' <<'CFG'
frontend check
    mode http
    timeout client 5s
    bind unix@/tmp/default-pem-check.sock ssl crt /data/certs/
    http-request return status 200
CFG
exec_in "$s6/s6-setuidgid" homeproxy haproxy -c -f /tmp/default-pem-check.cfg >/dev/null \
  || fail "HAProxy rejects the recreated default.pem"

# A container killed between the release switch and the promote leaves generated.prev.
# The unconfirmed release has a config HAProxy rejects, so only the switch back lets it serve.
# The promoted release is a copy that first-boot seeding does not recreate.
step "restart after a cut-short apply boots on the last promoted release"
exec_in "$s6/s6-setuidgid" homeproxy sh -c '
  cd /data && cp -r releases/placeholder releases/promoted &&
  mkdir -p releases/unconfirmed/conf.d && echo "bogus_keyword" >releases/unconfirmed/conf.d/broken.cfg &&
  ln -s releases/unconfirmed .generated.tmp && mv -T .generated.tmp generated &&
  ln -s releases/promoted generated.prev' || fail "cannot stage a cut-short apply"
restart_and_wait
[ "$(exec_in readlink /data/generated)" = "releases/promoted" ] || fail "init-data did not switch back to generated.prev"
exec_in test ! -e /data/generated.prev || fail "init-data left generated.prev"

# Once the promote began, /data/config holds the edits of the current release, so it stays live.
step "restart after a committed promote keeps the current release"
exec_in "$s6/s6-setuidgid" homeproxy sh -c '
  cd /data && mkdir -p pending &&
  echo "{\"format\": 1, \"base_files\": {}, \"changes\": {\"routes/smoke-gone.yaml\": \"delete\"}, \"promoting\": true}" >pending/manifest.json &&
  ln -s releases/placeholder generated.prev' || fail "cannot stage a committed promote"
restart_and_wait
[ "$(exec_in readlink /data/generated)" = "releases/promoted" ] || fail "init-data switched back after the promote began"
exec_in test ! -e /data/generated.prev || fail "init-data left generated.prev"

failed=0
step "ok"
