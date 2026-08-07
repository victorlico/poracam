#!/bin/bash
# file: beforeShutdown.sh
#
# Defensive Witty Pi beforeShutdown.sh for Poracam v0.8.3.2.
#
# This script is executed by Witty Pi daemon.sh after GPIO-4 is pulled LOW
# and before Linux shutdown is requested.
#
# Poracam already syncs its files before requesting shutdown. This script
# performs one bounded final sync so storage cannot block shutdown forever.

LOG="/home/fishcam/poracam/poracam_beforeShutdown.log"
SYNC_TIMEOUT_SECONDS=20

mkdir -p "$(dirname "$LOG")"

echo "$(date '+%F %T') [beforeShutdown] Witty Pi shutdown path reached" >> "$LOG"
echo "$(date '+%F %T') [beforeShutdown] final sync start (timeout=${SYNC_TIMEOUT_SECONDS}s)" >> "$LOG"

if timeout "${SYNC_TIMEOUT_SECONDS}s" sync; then
    RC=0
    echo "$(date '+%F %T') [beforeShutdown] final sync done rc=0" >> "$LOG"
else
    RC=$?
    echo "$(date '+%F %T') [beforeShutdown] WARNING: final sync ended rc=$RC" >> "$LOG"
fi

# Never block Witty Pi shutdown because of the defensive sync.
exit 0