#!/bin/bash
# file: beforeShutdown.sh
#
# Optional defensive Witty Pi beforeShutdown.sh for Poracam v0.7.9.
# Poracam already syncs files before requesting shutdown; this is only an extra guard.

LOG="/home/fishcam/poracam/poracam_beforeShutdown.log"

echo "$(date '+%F %T') [beforeShutdown] sync start" >> "$LOG"
sync
echo "$(date '+%F %T') [beforeShutdown] sync done" >> "$LOG"

exit 0