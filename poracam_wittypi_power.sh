#!/bin/bash
# file: poracam_wittypi_power.sh
#
# Poracam v0.8.3.2 - corrected Witty Pi integration.
#
# Actions:
#   schedule-startup --target-epoch <epoch>
#   shutdown
#
# IMPORTANT:
# The shutdown action must NOT call do_shutdown() directly.
# It pulls GPIO-4 (HALT pin) LOW so Witty Pi daemon.sh receives the
# shutdown request, runs beforeShutdown.sh and then performs the normal
# Witty Pi shutdown/power-cut sequence.

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
        # Use the Witty Pi daemon's normal shutdown path.
        #
        # daemon.sh waits for a falling edge on HALT_PIN (normally BCM GPIO-4).
        # Pulling this pin LOW causes daemon.sh to:
        #   1. detect the shutdown request;
        #   2. log the event;
        #   3. run beforeShutdown.sh;
        #   4. call the normal Witty Pi shutdown routine;
        #   5. allow the Witty Pi firmware to cut Raspberry Pi power.
        #
        # Calling do_shutdown() directly bypasses steps 1-3 and is intentionally
        # not used here.

        HALT_GPIO="${HALT_PIN:-4}"

        if ! pgrep -f "$WITTYPI_DIR/daemon.sh" >/dev/null 2>&1; then
            echo "Witty Pi daemon.sh is not running; refusing direct Linux shutdown." >&2
            echo "Raspberry Pi will remain powered so the condition can be diagnosed safely." >&2
            exit 5
        fi

        echo "Requesting Raspberry Pi shutdown through Witty Pi daemon via GPIO-$HALT_GPIO."

        gpio -g mode "$HALT_GPIO" out
        gpio -g write "$HALT_GPIO" 0

        # Give daemon.sh a brief opportunity to observe the falling edge.
        sleep 1

        echo "Witty Pi shutdown trigger sent on GPIO-$HALT_GPIO."
        ;;

    *)
        echo "Unknown action: $ACTION" >&2
        exit 2
        ;;
esac

exit 0