#!/bin/bash
# file: beforeShutdown.sh
#
# Poracam v0.8.3.2 - Witty Pi shutdown hook
#
# IMPORTANT:
# Poracam already runs sync before triggering the Witty Pi shutdown path.
# Do not run another sync here: beforeShutdown.sh is synchronous inside
# daemon.sh, so any storage stall here prevents do_shutdown() from running.
#
# This hook only records that the official Witty Pi shutdown path was reached.

LOG="/home/fishcam/poracam/poracam_beforeShutdown.log"

mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T') [beforeShutdown] Witty Pi shutdown path reached; no extra sync required" >> "$LOG"

exit 0