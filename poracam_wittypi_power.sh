#!/bin/bash
# file: poracam_wittypi_power.sh
#
# Poracam v0.8.3.3 - Witty Pi integration with terminal shutdown trigger.
#
# Actions:
#   schedule-startup --target-epoch <epoch>
#   shutdown
#
# Shutdown behavior:
#   - never calls do_shutdown() directly;
#   - asks Witty Pi daemon.sh to shutdown through GPIO-4;
#   - the GPIO-4 write is the terminal command of this wrapper (exec), so the
#     wrapper performs no sleep/echo/file activity after the falling edge.

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
        fi

        clear_shutdown_time
        set_startup_time "$DATE" "$HOUR" "$MINUTE" "$SECOND"

        echo "Scheduled next startup at $HUMAN"
        echo "Witty Pi startup register: $(get_startup_time)"
        ;;

    shutdown)
        HALT_GPIO="${HALT_PIN:-4}"
        GPIO_BIN="$(command -v gpio || true)"

        if [ -z "$GPIO_BIN" ]; then
            echo "gpio command not found; shutdown not triggered." >&2
            exit 5
        fi

        if ! pgrep -f "$WITTYPI_DIR/daemon.sh" >/dev/null 2>&1; then
            echo "Witty Pi daemon.sh is not running; shutdown not triggered." >&2
            exit 6
        fi

        echo "Requesting Raspberry Pi shutdown through Witty Pi daemon via GPIO-$HALT_GPIO."

        "$GPIO_BIN" -g mode "$HALT_GPIO" out

        # TERMINAL ACTION. Successful exec replaces this shell with the GPIO
        # command. No command in this wrapper runs after GPIO-4 goes LOW.
        exec "$GPIO_BIN" -g write "$HALT_GPIO" 0
        ;;

    *)
        echo "Unknown action: $ACTION" >&2
        exit 2
        ;;
esac

exit 0