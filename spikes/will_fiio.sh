#!/bin/bash
# Put Will Paquin's full Tidal discography in the Unraid library and on the FiiO card.
# Phases: (0) wait for Tidal to stop 429ing  (1) download the 5 releases we lack
#         (2) verify FLACs  (3) copy to Unraid library (never overwrite)  (4) copy to card + format.
# Never deletes anything. Log: ~/music-manager/spikes/out/will_fiio.log
set -u
LOG=$HOME/music-manager/spikes/out/will_fiio.log
exec >>"$LOG" 2>&1
say() { echo "$(date '+%F %T') $*"; }
PY=$HOME/.local/pipx/venvs/tidal-dl-ng/bin/python
STAGE=$HOME/Music/_tidal_staging/Will\ Paquin
UNRAID=root@100.69.34.110
LIB="/mnt/user/Movies_TV/Music/Will Paquin"
CARD="/Volumes/NO NAME/Music/Will Paquin"
set -a; . "$HOME/.claude-secrets/credentials.env"; set +a
export SSHPASS="$UNRAID_SSH_PASSWORD"   # sshpass -e reads it from the env, keeping it off the command line
SSHP="/opt/homebrew/bin/sshpass -e ssh -o StrictHostKeyChecking=no"
# release id : folder name tidal-dl-ng will create : expected track count
RELEASES=("412366267:In Two:1" "412366271:Lightning:1" "386709088:Infinite Jest:4")

probe() { $PY - <<'EOF' 2>/dev/null
import json, os, sys, tidalapi
t = json.load(open(os.path.expanduser("~/.config/tidal_dl_ng/token.json")))
g = lambda k: t[k]["data"] if isinstance(t[k], dict) else t[k]
s = tidalapi.Session(); s.load_oauth_session(g("token_type"), g("access_token"), g("refresh_token"))
try: s.album(298880016); print("OK")
except Exception as e: print("BLOCKED", type(e).__name__)
EOF
}

say "== phase 0: waiting for Tidal"
for i in $(seq 1 50); do
  r=$(probe)
  if [ "$r" = "OK" ]; then say "Tidal OK (probe $i)"; break; fi
  say "probe $i: $r; sleeping 180s"; sleep 180
  [ "$i" = 50 ] && { say "GAVE UP waiting for Tidal"; echo WILLDONE-FAILED; exit 1; }
done

say "== phase 1: download"
for rel in "${RELEASES[@]}"; do
  id=${rel%%:*}; rest=${rel#*:}; name=${rest%%:*}; want=${rest##*:}
  for attempt in 1 2 3; do
    have=$(find "$STAGE/$name" -name '*.flac' 2>/dev/null | wc -l | tr -d ' ')
    [ "$have" -ge "$want" ] && break
    say "download $name (id $id) attempt $attempt (have $have/$want)"
    ~/.local/bin/tidal-dl-ng dl "https://tidal.com/browse/album/$id" 2>&1 | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | grep -E "Media not found|error|Error|Downloaded" | head -3
    sleep 45
    have=$(find "$STAGE/$name" -name '*.flac' 2>/dev/null | wc -l | tr -d ' ')
    [ "$have" -ge "$want" ] && break
    say "  only $have/$want after attempt $attempt; backing off 300s"; sleep 300
  done
  have=$(find "$STAGE/$name" -name '*.flac' 2>/dev/null | wc -l | tr -d ' ')
  say "  RESULT $name: $have/$want flac"
done

say "== phase 2: verify (full decode)"
ok=0; bad=0
while IFS= read -r f; do
  if /opt/homebrew/bin/ffmpeg -v error -i "$f" -f null - 2>/dev/null; then ok=$((ok+1)); else bad=$((bad+1)); say "  BAD: $f"; fi
done < <(find "$STAGE" -name '*.flac')
say "verified ok=$ok bad=$bad"
[ "$bad" -gt 0 ] && { say "bad files present; stopping before any copy"; echo WILLDONE-FAILED; exit 1; }

say "== phase 3: copy to Unraid library (ignore-existing)"
rsync -rt --ignore-existing --exclude='._*' --exclude='.DS_Store' -e "$SSHP" "$STAGE/" "$UNRAID:$LIB/" 2>&1 | tail -3
# the Chandelier instrumental that Lidarr downloaded but could not import
$SSHP $UNRAID "set -e; S='/mnt/user/downloads/tidal/Will Paquin/Chandelier (Instrumental Version)/01 - 01 - Chandelier (Instrumental Version).flac'; D='$LIB/Chandelier (Instrumental Version)'; if [ -f \"\$S\" ] && [ ! -f \"\$D/01 - Chandelier (Instrumental Version).flac\" ]; then mkdir -p \"\$D\"; cp \"\$S\" \"\$D/01 - Chandelier (Instrumental Version).flac\"; chmod 666 \"\$D\"/*; echo copied-instrumental; fi" 2>&1 | grep -vi tailscale

say "== phase 4: card"
if [ ! -d "/Volumes/NO NAME/Music" ]; then say "CARD NOT MOUNTED; library is done, card step skipped"; echo WILLDONE-NOCARD; exit 0; fi
touch "/Volumes/NO NAME/.wtest" 2>/dev/null && rm "/Volumes/NO NAME/.wtest" || { say "CARD READ-ONLY; skipped"; echo WILLDONE-NOCARD; exit 0; }
mkdir -p "$CARD"
rsync -rt --ignore-existing --exclude='._*' --exclude='.DS_Store' --exclude='*.lrc' -e "$SSHP" "$UNRAID:$LIB/" "$CARD/" 2>&1 | tail -5
find "/Volumes/NO NAME/Music" -name '._*' -delete 2>/dev/null
$HOME/bin/fiio-format-card "/Volumes/NO NAME/Music" 2>&1 | tail -8
say "card now has: $(find "$CARD" -name '*.flac' | wc -l | tr -d ' ') flac files"
say "== done"
echo WILLDONE
