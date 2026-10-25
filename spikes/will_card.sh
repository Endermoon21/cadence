#!/bin/bash
# Copy Will Paquin from the Unraid library to the FiiO card (path with a space is backslash-escaped for the remote shell), then format.
LOG=$HOME/music-manager/spikes/out/will_fiio.log; exec >>"$LOG" 2>&1
say() { echo "$(date '+%F %T') $*"; }
while pgrep -f will_fiio.sh >/dev/null || pgrep -f "bin/fiio-format-card" >/dev/null; do sleep 10; done
say "== card copy (fixed quoting)"
[ -d "/Volumes/NO NAME/Music" ] || { say "CARD NOT MOUNTED"; echo CARDDONE-NOCARD; exit 0; }
touch "/Volumes/NO NAME/.wtest" 2>/dev/null && rm "/Volumes/NO NAME/.wtest" || { say "CARD READ-ONLY"; echo CARDDONE-NOCARD; exit 0; }
mkdir -p "/Volumes/NO NAME/Music/Will Paquin"
set -a; . "$HOME/.claude-secrets/credentials.env"; set +a
export SSHPASS="$UNRAID_SSH_PASSWORD"   # sshpass -e reads it from the env, keeping it off the command line
/opt/homebrew/bin/sshpass -e rsync -rt --ignore-existing --exclude='._*' --exclude='.DS_Store' --exclude='*.lrc' \
  -e "ssh -o StrictHostKeyChecking=no" 'root@100.69.34.110:/mnt/user/Movies_TV/Music/Will\ Paquin/' "/Volumes/NO NAME/Music/Will Paquin/" 2>&1 | tail -15
find "/Volumes/NO NAME/Music" -name '._*' -delete 2>/dev/null
say "card Will flac count: $(find '/Volumes/NO NAME/Music/Will Paquin' -name '*.flac' | wc -l | tr -d ' ')"
$HOME/bin/fiio-format-card "/Volumes/NO NAME/Music" 2>&1 | tail -8
say "== card done"; echo CARDDONE
