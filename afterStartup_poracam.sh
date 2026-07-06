#!/bin/bash
# file: afterStartup.sh
#
# Suggested Witty Pi afterStartup.sh for Poracam v0.7.3.
#
# It launches Poracam in the background and returns quickly so Witty Pi daemon.sh
# can finish its startup sequence and send SYS_UP. Poracam itself will schedule
# the next Witty Pi startup and request shutdown when recording is complete.

PORACAM_DIR="/home/fishcam/poracam"
PORACAM_SCRIPT="$PORACAM_DIR/poracam_record.py"
LOG="$PORACAM_DIR/poracam_afterStartup.log"

mkdir -p "$PORACAM_DIR"

echo "$(date '+%F %T') [afterStartup] launching Poracam v0.7.3" >> "$LOG"

if [ ! -x "$PORACAM_SCRIPT" ]; then
  echo "$(date '+%F %T') [afterStartup] ERROR: script not executable: $PORACAM_SCRIPT" >> "$LOG"
  exit 1
fi

/usr/bin/python3 "$PORACAM_SCRIPT" --power-control >> "$LOG" 2>&1 &

echo "$(date '+%F %T') [afterStartup] Poracam started in background with PID $!" >> "$LOG"
exit 0