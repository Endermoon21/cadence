#!/bin/bash
# slskd (Soulseek) behind Mullvad WireGuard via gluetun, on Unraid (no compose plugin there, same pattern as
# run-frigate.sh). slskd joins gluetun's network namespace, so every Soulseek packet leaves through the tunnel and
# gluetun's firewall drops anything else: if the VPN is down, slskd has no network at all (kill switch). That
# namespace is the real guarantee; Cadence's /v1/vpn/status check only catches an obvious outage.
#
# Secrets (chmod 600, never in git), one file per consumer so none sees another's:
#   $APPDATA/gluetun.env      read by gluetun
#     WIREGUARD_PRIVATE_KEY=...        from a WireGuard config generated on mullvad.net (not the Devices page key)
#     WIREGUARD_ADDRESSES=10.x.x.x/32  the IPv4 Address line of that same config
#   $APPDATA/gluetun-api.key  read only by this script (and given to Cadence as CADENCE_GLUETUN_API_KEY)
#     one line: the output of  docker run --rm qmcgaw/gluetun genkey
#   $APPDATA/slskd.env        read by slskd
#     SLSKD_SLSK_USERNAME=...  SLSKD_SLSK_PASSWORD=...          Soulseek account
#     SLSKD_USERNAME=...  SLSKD_PASSWORD=...                    slskd web UI login (replaces the slskd/slskd default)
#     SLSKD_API_KEY=role=ReadWrite;cidr=10.0.0.0/24,172.16.0.0/12;<32+ random chars>   Cadence uses the last part
#
# Mullvad has no port forwarding (removed 2023), so slskd cannot accept incoming peer connections. Searches and
# downloads still work, but peers that are also firewalled cannot be reached.
#
# Reboot: both containers are restart=unless-stopped. If Docker starts slskd before gluetun is up, slskd fails to
# join the namespace and stays down (fails closed); rerun this script to bring it back.
set -euo pipefail

APPDATA=/mnt/cache/appdata/slskd
DATA=/mnt/user/Movies_TV/_slskd          # downloads/ and incomplete/; Cadence mounts $DATA/downloads read-only
COUNTRY=${MULLVAD_COUNTRY:-USA}
LABEL=cadence.managed=slskd

for f in gluetun.env gluetun-api.key slskd.env; do
  [ -f "$APPDATA/$f" ] || { echo "missing $APPDATA/$f (see header)" >&2; exit 2; }
  [ "$(stat -c %a "$APPDATA/$f")" = 600 ] || { echo "$APPDATA/$f must be chmod 600" >&2; exit 2; }
done

key=$(tr -d '[:space:]"'"'" < "$APPDATA/gluetun-api.key")
[[ "$key" =~ ^[A-Za-z0-9_-]{16,}$ ]] || { echo "$APPDATA/gluetun-api.key must hold one key of 16+ [A-Za-z0-9_-] characters" >&2; exit 2; }

# Only replace containers this script created; never a same-named container from somewhere else.
for c in slskd gluetun-slskd; do
  if docker inspect "$c" >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{ index .Config.Labels "cadence.managed" }}' "$c")" != slskd ]; then
      echo "a container named $c exists that this script did not create; remove or rename it first" >&2; exit 2
    fi
  fi
done

mkdir -p "$APPDATA/app" "$APPDATA/gluetun" "$DATA/downloads" "$DATA/incomplete"
chown -R 99:100 "$APPDATA/app" "$DATA"

# gluetun control server: only the two read routes Cadence needs, behind an API key.
(umask 077; cat > "$APPDATA/gluetun/auth.toml" <<EOF
[[roles]]
name = "cadence"
routes = ["GET /v1/vpn/status", "GET /v1/publicip/ip"]
auth = "apikey"
apikey = "$key"
EOF
)

docker rm -f slskd gluetun-slskd >/dev/null 2>&1 || true

docker run -d --name gluetun-slskd --label "$LABEL" --restart unless-stopped \
  --cap-add NET_ADMIN --device /dev/net/tun \
  --env-file "$APPDATA/gluetun.env" \
  -e VPN_SERVICE_PROVIDER=mullvad -e VPN_TYPE=wireguard -e SERVER_COUNTRIES="$COUNTRY" \
  -e HTTP_CONTROL_SERVER_AUTH_CONFIG_FILEPATH=/gluetun/auth/config.toml \
  -v "$APPDATA/gluetun/auth.toml:/gluetun/auth/config.toml:ro" \
  -p 5030:5030 -p 8010:8000 \
  qmcgaw/gluetun:v3.41.3 >/dev/null

# Start slskd only once gluetun's own healthcheck says the tunnel is up.
for i in $(seq 1 60); do
  [ "$(docker inspect -f '{{ .State.Health.Status }}' gluetun-slskd 2>/dev/null)" = healthy ] && break
  [ "$i" = 60 ] && { echo "gluetun-slskd not healthy after 120 s; slskd not started (docker logs gluetun-slskd)" >&2; exit 1; }
  sleep 2
done

docker run -d --name slskd --label "$LABEL" --restart unless-stopped \
  --network container:gluetun-slskd \
  --user 99:100 \
  --env-file "$APPDATA/slskd.env" \
  -e SLSKD_HTTP_PORT=5030 \
  -e SLSKD_DOWNLOADS_DIR=/downloads -e SLSKD_INCOMPLETE_DIR=/incomplete \
  -e SLSKD_REMOTE_CONFIGURATION=false \
  -v "$APPDATA/app:/app" \
  -v "$DATA/downloads:/downloads" -v "$DATA/incomplete:/incomplete" \
  slskd/slskd:0.26.1 >/dev/null

echo "started. Exit IP: docker logs gluetun-slskd 2>&1 | grep -i 'public ip'"
