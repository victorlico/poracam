#!/bin/bash
# file: poracam_wittypi_power.sh
#
# Helper used by Poracam v0.8.1 to integrate with Witty Pi utilities.sh.
#
# Actions:
#   schedule-startup --target-epoch <epoch>
#   shutdown
#
# Important:
#   Do NOT use `set -u` here. The Witty Pi utilities.sh has functions that
#   inspect optional positional parameters like $1. With nounset enabled,
#   those functions can fail with "variável não associada".

set -o pipefail

ACTION="${1:-}"
shift || true

WITTYPI_DIR=""
TARGET_EPOCH=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --wittypi-dir)
      WITTYPI_DIR="${2:-}"
      shift 2
      ;;
    --target-epoch)
      TARGET_EPOCH="${2:-}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [ -z "$ACTION" ]; then
  echo "Missing action: schedule-startup or shutdown" >&2
  exit 2
fi

if [ -z "$WITTYPI_DIR" ]; then
  for d in /home/fishcam/wittypi /home/pi/wittypi; do
    if [ -f "$d/utilities.sh" ]; then
      WITTYPI_DIR="$d"
      break
    fi
  done
fi

if [ -z "$WITTYPI_DIR" ] || [ ! -f "$WITTYPI_DIR/utilities.sh" ]; then
  echo "Witty Pi utilities.sh not found. WITTYPI_DIR=$WITTYPI_DIR" >&2
  exit 3
fi

# shellcheck source=/dev/null
. "$WITTYPI_DIR/utilities.sh"

if [ "$DRY_RUN" = "1" ]; then
  echo "[DRY-RUN] action=$ACTION wittypi_dir=$WITTYPI_DIR target_epoch=${TARGET_EPOCH:-none}"
  exit 0
fi

HAS_MC="$(is_mc_connected)"
if [ "$HAS_MC" != "1" ]; then
  echo "Witty Pi microcontroller not detected." >&2
  exit 4
fi

case "$ACTION" in
  schedule-startup)
    if [ -z "$TARGET_EPOCH" ]; then
      echo "schedule-startup requires --target-epoch" >&2
      exit 2
    fi
    if ! [[ "$TARGET_EPOCH" =~ ^[0-9]+$ ]]; then
      echo "Invalid target epoch: $TARGET_EPOCH" >&2
      exit 2
    fi

    DATE="$(date -d "@$TARGET_EPOCH" +"%d")"
    HOUR="$(date -d "@$TARGET_EPOCH" +"%H")"
    MINUTE="$(date -d "@$TARGET_EPOCH" +"%M")"
    SECOND="$(date -d "@$TARGET_EPOCH" +"%S")"
    HUMAN="$(date -d "@$TARGET_EPOCH" +"%Y-%m-%d %H:%M:%S %Z")"

    RES="$(check_sys_and_rtc_time || true)"
    if [ -n "$RES" ]; then
      echo "$RES" >&2
      # Do not fail hard: daemon.sh normally syncs RTC to system at startup.
    fi

    clear_shutdown_time
    set_startup_time "$DATE" "$HOUR" "$MINUTE" "$SECOND"

    echo "Scheduled next startup at $HUMAN"
    echo "Witty Pi startup register: $(get_startup_time)"
    ;;

  shutdown)
    echo "Requesting Raspberry Pi shutdown via Witty Pi utilities."
    do_shutdown "$HALT_PIN" "$HAS_MC"
    ;;

  *)
    echo "Unknown action: $ACTION" >&2
    exit 2
    ;;
esac