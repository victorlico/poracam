#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poracam_record.py — Poracam v0.8.3.4

Novidades da v0.8.3.4:
- Adiciona recuperação automática para falha transitória de enumeração/montagem do pendrive.
- Após uma falha inicial, agenda até 3 novos power-cycles de recuperação, espaçados em 3 min.
- Zera automaticamente o contador de recuperação assim que o armazenamento externo reaparece.
- Mantém bloqueado o fallback de gravação no cartão SD.
- Evita cair para plughw quando hw está apenas temporariamente ocupado; prioriza novo retry de hw.
- Preserva timeout USB de 90 s, áudio hw direto com buffer de 2 s e shutdown terminal validado.
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_NAME = "poracam"
PROJECT_VERSION = "0.8.3.4"

# ============================================================
# Developer/internal configuration
# ============================================================
# These parameters are intentionally not exposed in the user config.txt.
# The user-facing configuration should remain simple and robust.

DEFAULT_SESSION_NAME = "fishcam"

MAX_RECORD_DURATION_PER_SEGMENT_S = 600  # 10 minutes per file
ENABLE_SEGMENT_SPLIT = True

DEFAULT_MEDIA_DIR = "/home/fishcam/poracam/media"
MAX_STORAGE_PERCENT = 95.0
MIN_FREE_MB_BEFORE_RECORDING = 300
STOP_SCHEDULING_WHEN_STORAGE_FULL = True

# v0.8.3.4: production-oriented diagnostics.
# Full metadata remains enabled by default while the system is still being validated.
METADATA_ENABLED = False
LIGHT_LOG_ENABLED = True
TRASH_DETECTION_ENABLED = True
TRASH_WARNING_MIN_MB = 50
TRASH_DIR_NAMES = (".Trash", ".Trash-1000", ".Trashes", "$RECYCLE.BIN", "RECYCLER", "System Volume Information")

# v0.8.3.4: optional time/date adjustment through a one-shot file on the USB drive.
# File must be placed beside PORACAM/config.txt.
TIME_SET_ENABLED = True
TIME_SET_FILE_NAMES = ("SET_TIME.txt", "set_time.txt", "datetime.txt", "data_hora.txt")
TIME_SET_DONE_FILE_NAME = "time_set_last_ok.txt"
TIME_SET_ERROR_SUFFIX = ".error"

# v0.8.3.4: initial campaign check and LED status.
FIELD_CHECK_ENABLED = True
FIELD_CHECK_DURATION_S = 30
READY_STATUS_FILE_NAME = "PRONTO_PARA_CAMPO.txt"
LED_STATUS_ENABLED = True
LED_NAME = "led0"
LED_OK_BRIGHTNESS = "1"
LED_OFF_BRIGHTNESS = "0"
LED_CHECK_DELAY_MS = 1000
LED_ERROR_DELAY_MS = 120

# v0.8.3.4: allow slow/re-enumerating USB storage enough time to become available.
# The loop exits immediately when PORACAM/config.txt is found, so 90 s is only the maximum recovery window.
EXTERNAL_CONFIG_WAIT_TIMEOUT_S = 90
EXTERNAL_CONFIG_RETRY_INTERVAL_S = 2
EXTERNAL_CONFIG_UDEV_SETTLE_TIMEOUT_S = 4
EXTERNAL_CONFIG_TRY_MANUAL_MOUNT = True

# v0.8.3.4: if external storage was selected, verify it is still mounted before recording
# and before writing metadata/status. This avoids writing to a stale /media directory
# if the USB storage resets/disappears mid-cycle.
EXTERNAL_STORAGE_VERIFY_BEFORE_RECORDING = True
EXTERNAL_STORAGE_VERIFY_BEFORE_METADATA = True

# v0.8.3.4: in autonomous/Witty Pi mode, never silently record to local SD
# when the PORACAM pendrive is missing. This prevents losing field data on the Pi.
REQUIRE_EXTERNAL_STORAGE_IN_POWER_CONTROL = True

# v0.8.3.4: tolerate transient USB enumeration failures without turning one bad boot
# into the end of an autonomous campaign. The first failed boot may schedule retry #1;
# up to 3 recovery boots are allowed. A successful external-storage boot resets the state.
EXTERNAL_STORAGE_RECOVERY_ENABLED = True
EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES = 3
EXTERNAL_STORAGE_RECOVERY_RETRY_DELAY_S = 180
EXTERNAL_STORAGE_RECOVERY_STATE_FILE = "/home/fishcam/poracam/status/external_storage_retry.json"

EXTERNAL_MOUNT_BASE_DIRS = [
    "/media/fishcam",
    "/mnt",
]
EXTERNAL_MOUNT_FS_TYPES = {"vfat", "exfat", "ext4"}

AUDIO_DEVICE = "auto"
AUDIO_FORMAT = "S16_LE"
AUDIO_RATE_HZ = 44100
AUDIO_CHANNELS = 1
AUDIO_BUFFER_TIME_US = 2000000  # 2 s; validated with hw: capture + 1080p20 raspivid

# v0.8.3.4: prefer the direct ALSA hardware PCM discovered by `arecord -l`.
# The validated USB interface accepts S16_LE / 44.1 kHz / mono directly on hw:<card>,<device>.
# plughw is kept only as a fallback because it produced capture overruns during camera recording.
AUDIO_PROBE_SECONDS = 1

# USB audio devices can take a few seconds to appear after Witty Pi powers the Raspberry.
# Keep this internal: the user config.txt remains simple.
AUDIO_READY_TIMEOUT_S = 45
AUDIO_READY_RETRY_INTERVAL_S = 3

KEEP_H264 = False

CAMERA_CONFLICT_POLICY = "stop_known_processes"
CAMERA_STARTUP_CHECK_S = 0.5
CAMERA_STOP_TIMEOUT_S = 5.0

START_DELAY_S = 0.1
EXTRA_TIMEOUT_S = 60.0

# Witty Pi power-control integration.
# Kept out of config.txt: the user still edits only recording time, cycle period and video quality.
WITTYPI_POWER_CONTROL_ENABLED_BY_DEFAULT = False  # enabled by --power-control, used by afterStartup wrapper
WITTYPI_DIR_CANDIDATES = [
    "/home/fishcam/wittypi",
    "/home/pi/wittypi",
]
WITTYPI_POWER_SCRIPT_NAME = "poracam_wittypi_power.sh"
MINIMUM_OFF_TIME_S = 60
SHUTDOWN_DELAY_S = 5
REQUIRE_STARTUP_SCHEDULE_BEFORE_SHUTDOWN = True

VIDEO_PROFILES: Dict[str, Dict[str, int]] = {
    # Lower storage use and lighter processing.
    "low": {
        "width": 1280,
        "height": 720,
        "fps": 20,
        "bitrate": 2500000,
    },
    # 1080p with reduced frame rate/bitrate for a compromise between image and autonomy.
    "balanced": {
        "width": 1920,
        "height": 1080,
        "fps": 20,
        "bitrate": 3500000,
    },
    # Validated 1080p profile with better image quality.
    "high": {
        "width": 1920,
        "height": 1080,
        "fps": 25,
        "bitrate": 4000000,
    },
}

DEFAULT_CONFIG: Dict[str, Any] = {
    # User-facing fields
    "session_name": DEFAULT_SESSION_NAME,
    "record_duration_min": 1.0,
    "cycle_period_min": 5.0,
    "video_quality": "balanced",

    # Runtime fields derived from user-facing fields
    "duration": 60,
    "cycle_period_s": 300,
    "segment_duration_s": MAX_RECORD_DURATION_PER_SEGMENT_S,
    "enable_segment_split": ENABLE_SEGMENT_SPLIT,

    # Internal/developer fields
    "run_mode": "single",
    "media_dir": DEFAULT_MEDIA_DIR,
    "width": VIDEO_PROFILES["balanced"]["width"],
    "height": VIDEO_PROFILES["balanced"]["height"],
    "fps": VIDEO_PROFILES["balanced"]["fps"],
    "bitrate": VIDEO_PROFILES["balanced"]["bitrate"],
    "audio_device": AUDIO_DEVICE,
    "audio_format": AUDIO_FORMAT,
    "audio_buffer_time_us": AUDIO_BUFFER_TIME_US,
    "keep_h264": KEEP_H264,
    "start_delay": START_DELAY_S,
    "extra_timeout": EXTRA_TIMEOUT_S,
    "max_storage_percent": MAX_STORAGE_PERCENT,
    "prefer_external_storage": True,
    "allow_local_fallback": True,
    "require_external_storage_in_power_control": REQUIRE_EXTERNAL_STORAGE_IN_POWER_CONTROL,
    "metadata_enabled": METADATA_ENABLED,
    "light_log_enabled": LIGHT_LOG_ENABLED,
    "trash_detection_enabled": TRASH_DETECTION_ENABLED,
    "time_set_enabled": TIME_SET_ENABLED,
    "field_check_enabled": FIELD_CHECK_ENABLED,
    "power_control_enabled": WITTYPI_POWER_CONTROL_ENABLED_BY_DEFAULT,
    "power_dry_run": False,
    "shutdown_after_recording": True,
    "minimum_off_time_s": MINIMUM_OFF_TIME_S,
    "shutdown_delay_s": SHUTDOWN_DELAY_S,
    "require_startup_schedule_before_shutdown": REQUIRE_STARTUP_SCHEDULE_BEFORE_SHUTDOWN,
    "wittypi_dir": "",
    "wittypi_power_script": "",
    "camera_conflict_policy": CAMERA_CONFLICT_POLICY,
    "camera_startup_check_s": CAMERA_STARTUP_CHECK_S,
    "camera_stop_timeout_s": CAMERA_STOP_TIMEOUT_S,
}

# User-facing keys accepted in config.txt.
# Technical parameters are intentionally not accepted from config.txt in v0.8.3.3.
CONFIG_KEY_ALIASES = {
    "session_name": "session_name",
    "record_duration_min": "record_duration_min",
    "record_minutes": "record_duration_min",
    "cycle_period_min": "cycle_period_min",
    "cycle_minutes": "cycle_period_min",
    "video_quality": "video_quality",
    "quality": "video_quality",
}


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def which_or_fail(command: str) -> str:
    found = shutil.which(command)
    if not found:
        raise RuntimeError(f"Comando não encontrado no PATH: {command}")
    return found


def append_log(log_file: Path, message: str) -> None:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"[{iso_now()}] {message}\n")
        log.flush()


def parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in ("1", "true", "yes", "y", "sim", "s", "on"):
        return True
    if normalized in ("0", "false", "no", "n", "nao", "não", "off"):
        return False
    raise ValueError(f"Valor booleano inválido: {value}")


def parse_value(key: str, raw_value: str) -> Any:
    value = raw_value.strip()

    if key in ("record_duration_min", "cycle_period_min"):
        return float(value.replace(",", "."))

    if key == "video_quality":
        return value.strip().lower()

    if key == "session_name":
        return value.strip()

    return value


def find_external_config_path() -> Optional[Path]:
    patterns = [
        "/media/*/*/PORACAM/config.txt",
        "/mnt/*/PORACAM/config.txt",
    ]
    candidates: List[Path] = []
    for pattern in patterns:
        for match in glob.glob(pattern):
            p = Path(match)
            if p.exists() and p.is_file():
                candidates.append(p.resolve())
    candidates = sorted(set(candidates), key=lambda p: str(p))
    return candidates[0] if candidates else None


def sanitize_mount_name(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    safe = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    result = "".join(safe).strip("._")
    return result[:64]


def run_command_capture(command: List[str], timeout_s: int = 10) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or f"timeout after {timeout_s}s"
    except Exception as exc:
        return 1, "", str(exc)


def udev_settle_for_storage(warnings: List[str]) -> None:
    udevadm = shutil.which("udevadm")
    if not udevadm:
        return
    rc, out, err = run_command_capture(
        [udevadm, "settle", f"--timeout={int(EXTERNAL_CONFIG_UDEV_SETTLE_TIMEOUT_S)}"],
        timeout_s=int(EXTERNAL_CONFIG_UDEV_SETTLE_TIMEOUT_S) + 2,
    )
    if rc != 0:
        warnings.append(f"udevadm settle retornou código {rc}: {(out + err).strip()[-300:]}")


def get_fishcam_uid_gid() -> Tuple[Optional[str], Optional[str]]:
    try:
        import pwd
        pw = pwd.getpwnam("fishcam")
        return str(pw.pw_uid), str(pw.pw_gid)
    except Exception:
        return None, None


def iter_lsblk_devices(warnings: List[str]) -> List[Dict[str, Any]]:
    lsblk = shutil.which("lsblk")
    if not lsblk:
        warnings.append("lsblk não encontrado; montagem manual de pendrive indisponível.")
        return []

    rc, out, err = run_command_capture(
        [lsblk, "-J", "-o", "NAME,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINT,RM"],
        timeout_s=8,
    )
    if rc != 0:
        warnings.append(f"lsblk falhou com código {rc}: {(out + err).strip()[-500:]}")
        return []

    try:
        data = json.loads(out)
    except Exception as exc:
        warnings.append(f"falha ao interpretar saída JSON do lsblk: {exc}")
        return []

    devices: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any]) -> None:
        devices.append(node)
        for child in node.get("children") or []:
            walk(child)

    for dev in data.get("blockdevices") or []:
        walk(dev)

    return devices


def is_probably_usb_partition(dev: Dict[str, Any]) -> bool:
    name = str(dev.get("name") or "")
    dtype = str(dev.get("type") or "")
    fstype = str(dev.get("fstype") or "").lower()
    mountpoint = dev.get("mountpoint")
    rm = dev.get("rm")

    if dtype != "part":
        return False
    if not fstype or fstype not in EXTERNAL_MOUNT_FS_TYPES:
        return False
    if mountpoint:
        return False

    # Avoid touching the Raspberry system SD card.
    if name.startswith("/dev/mmcblk"):
        return False

    # Common USB storage names. RM is not always reliable, so /dev/sd* is allowed.
    if name.startswith("/dev/sd") or name.startswith("sd"):
        return True

    try:
        if int(rm) == 1:
            return True
    except Exception:
        pass

    return False


def block_device_path(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        return name
    if name.startswith("/dev/"):
        return name
    return f"/dev/{name}"


def mount_options_for_fstype(fstype: str) -> List[str]:
    fstype = (fstype or "").lower()
    if fstype in ("vfat", "exfat"):
        uid, gid = get_fishcam_uid_gid()
        opts = ["rw", "umask=0002"]
        if uid and gid:
            opts.extend([f"uid={uid}", f"gid={gid}"])
        return ["-o", ",".join(opts)]
    return []


def attempt_manual_mount_external_storage(warnings: List[str]) -> None:
    if not EXTERNAL_CONFIG_TRY_MANUAL_MOUNT:
        return

    if os.geteuid() != 0:
        warnings.append("montagem manual de pendrive ignorada: processo não está rodando como root.")
        return

    mount_cmd = shutil.which("mount")
    if not mount_cmd:
        warnings.append("comando mount não encontrado; montagem manual de pendrive indisponível.")
        return

    devices = iter_lsblk_devices(warnings)
    candidates = [dev for dev in devices if is_probably_usb_partition(dev)]

    if not candidates:
        return

    for dev in candidates:
        name = str(dev.get("name") or "")
        dev_path = block_device_path(name)
        fstype = str(dev.get("fstype") or "").lower()
        label = sanitize_mount_name(str(dev.get("label") or ""))
        uuid = sanitize_mount_name(str(dev.get("uuid") or ""))
        basename = sanitize_mount_name(Path(name).name)
        mount_name = label or uuid or basename
        if not mount_name:
            continue

        mounted = False
        for base in EXTERNAL_MOUNT_BASE_DIRS:
            base_path = Path(base)
            try:
                base_path.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                warnings.append(f"não foi possível criar base de montagem {base_path}: {exc}")
                continue

            target = base_path / mount_name
            try:
                target.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                warnings.append(f"não foi possível criar ponto de montagem {target}: {exc}")
                continue

            cmd = [mount_cmd] + mount_options_for_fstype(fstype) + [dev_path, str(target)]
            rc, out, err = run_command_capture(cmd, timeout_s=10)
            msg = (out + err).strip()
            if rc == 0:
                warnings.append(f"pendrive montado manualmente: {dev_path} -> {target}")
                mounted = True
                break

            # If already mounted by a race between lsblk and mount, this is not fatal.
            if "already mounted" in msg.lower() or "já está montado" in msg.lower():
                warnings.append(f"pendrive já estava montado durante tentativa manual: {dev_path}")
                mounted = True
                break

            warnings.append(f"tentativa de montar {dev_path} em {target} falhou com código {rc}: {msg[-300:]}")

        if mounted and find_external_config_path() is not None:
            return


def wait_for_external_config_path(warnings: List[str]) -> Optional[Path]:
    """
    Wait briefly for PORACAM/config.txt on external storage.

    This avoids a common Witty Pi boot race:
    afterStartup starts Poracam before the USB pendrive has been automounted,
    so older versions immediately fell back to local storage.
    """
    deadline = time.monotonic() + float(EXTERNAL_CONFIG_WAIT_TIMEOUT_S)
    attempt = 0

    while True:
        attempt += 1

        external = find_external_config_path()
        if external is not None:
            if attempt > 1:
                warnings.append(f"armazenamento externo encontrado após {attempt} tentativas: {external}")
            return external

        warnings.append(f"armazenamento externo ainda não encontrado na tentativa {attempt}; tentando acelerar montagem USB.")

        udev_settle_for_storage(warnings)

        external = find_external_config_path()
        if external is not None:
            warnings.append(f"armazenamento externo encontrado após udevadm settle: {external}")
            return external

        attempt_manual_mount_external_storage(warnings)

        external = find_external_config_path()
        if external is not None:
            warnings.append(f"armazenamento externo encontrado após tentativa de montagem manual: {external}")
            return external

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            warnings.append(
                "armazenamento externo não encontrado após "
                f"{EXTERNAL_CONFIG_WAIT_TIMEOUT_S}s; usando fallback local se disponível."
            )
            return None

        sleep_s = min(float(EXTERNAL_CONFIG_RETRY_INTERVAL_S), remaining)
        time.sleep(sleep_s)



def external_mount_root_from_config(config_path: Optional[str]) -> Optional[Path]:
    if not config_path:
        return None
    try:
        p = Path(config_path).expanduser().resolve()
    except Exception:
        p = Path(config_path)
    parts = list(p.parts)
    if "PORACAM" not in parts:
        return None
    idx = parts.index("PORACAM")
    if idx <= 0:
        return None
    try:
        return Path(*parts[:idx])
    except Exception:
        return None


def is_path_the_external_mountpoint(path: Path) -> bool:
    """Return True only if path itself is a mount target, not merely under /."""
    findmnt = shutil.which("findmnt")
    if findmnt:
        rc, out, err = run_command_capture([findmnt, "-T", str(path), "-n", "-o", "TARGET"], timeout_s=5)
        if rc == 0:
            target = out.strip().splitlines()[0] if out.strip() else ""
            try:
                return Path(target).resolve() == path.resolve()
            except Exception:
                return target == str(path)
    try:
        return path.exists() and os.path.ismount(str(path))
    except Exception:
        return False


def verify_external_storage_alive(
    *,
    external_storage_used: bool,
    config_source: Optional[str],
    warnings: List[str],
    context: str,
) -> Tuple[bool, Optional[Path]]:
    if not external_storage_used:
        return True, None

    mount_root = external_mount_root_from_config(config_source)
    if mount_root is None:
        warnings.append(f"{context}: não foi possível inferir ponto de montagem externo a partir de {config_source}")
        return False, None

    cfg = mount_root / "PORACAM" / "config.txt"
    if is_path_the_external_mountpoint(mount_root) and cfg.exists():
        return True, mount_root

    warnings.append(f"{context}: ponto externo não está montado/estável: {mount_root}; tentando recuperar.")
    udev_settle_for_storage(warnings)
    attempt_manual_mount_external_storage(warnings)

    if is_path_the_external_mountpoint(mount_root) and cfg.exists():
        warnings.append(f"{context}: ponto externo recuperado: {mount_root}")
        return True, mount_root

    alt = find_external_config_path()
    if alt is not None:
        alt_root = external_mount_root_from_config(str(alt))
        if alt_root and is_path_the_external_mountpoint(alt_root):
            warnings.append(f"{context}: armazenamento externo reapareceu em outro caminho: {alt}")
            return True, alt_root

    warnings.append(f"{context}: armazenamento externo indisponível após tentativa de recuperação.")
    return False, mount_root


def save_recovery_metadata(metadata: Dict[str, Any], reason: str) -> Optional[str]:
    try:
        recovery_dir = Path(DEFAULT_MEDIA_DIR) / "recovery_metadata"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        path = recovery_dir / f"{now_stamp()}_poracam_recovery_metadata.json"
        recovery = dict(metadata)
        recovery["recovery_reason"] = reason
        recovery["recovery_saved_at"] = iso_now()
        path.write_text(json.dumps(recovery, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(path)
    except Exception:
        return None


def external_storage_recovery_state_path() -> Path:
    return Path(EXTERNAL_STORAGE_RECOVERY_STATE_FILE)


def load_external_storage_recovery_state() -> Dict[str, Any]:
    path = external_storage_recovery_state_path()
    default = {
        "consecutive_failures": 0,
        "retries_scheduled": 0,
        "last_failure_time": None,
        "last_success_time": None,
        "state_file": str(path),
    }
    try:
        if not path.exists():
            return default
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default
        state = dict(default)
        state.update(raw)
        state["consecutive_failures"] = max(0, int(state.get("consecutive_failures", 0)))
        state["retries_scheduled"] = max(0, int(state.get("retries_scheduled", 0)))
        return state
    except Exception:
        return default


def write_external_storage_recovery_state(state: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    path = external_storage_recovery_state_path()
    try:
        ensure_dir(path.parent)
        payload = dict(state)
        payload["state_file"] = str(path)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
        return True, None
    except Exception as exc:
        return False, str(exc)


def register_external_storage_recovery_failure(warnings: List[str]) -> Dict[str, Any]:
    state = load_external_storage_recovery_state()
    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    state["last_failure_time"] = iso_now()
    ok, err = write_external_storage_recovery_state(state)
    state["persisted"] = ok
    state["persist_error"] = err
    if not ok:
        warnings.append(f"não foi possível persistir contador de recuperação USB: {err}")
    return state


def mark_external_storage_recovery_retry_scheduled(warnings: List[str]) -> Dict[str, Any]:
    state = load_external_storage_recovery_state()
    state["retries_scheduled"] = int(state.get("retries_scheduled", 0)) + 1
    ok, err = write_external_storage_recovery_state(state)
    state["persisted"] = ok
    state["persist_error"] = err
    if not ok:
        warnings.append(f"não foi possível persistir retry de recuperação USB: {err}")
    return state


def reset_external_storage_recovery_state(warnings: List[str]) -> None:
    path = external_storage_recovery_state_path()
    if not path.exists():
        return
    previous = load_external_storage_recovery_state()
    failures = int(previous.get("consecutive_failures", 0))
    retries = int(previous.get("retries_scheduled", 0))
    try:
        path.unlink()
        warnings.append(
            f"armazenamento externo recuperado; contador USB zerado "
            f"(falhas consecutivas anteriores={failures}, retries agendados={retries})."
        )
    except Exception as exc:
        # Best effort fallback: write an explicit zero state if unlink is not possible.
        zero = {
            "consecutive_failures": 0,
            "retries_scheduled": 0,
            "last_failure_time": previous.get("last_failure_time"),
            "last_success_time": iso_now(),
            "state_file": str(path),
        }
        ok, err = write_external_storage_recovery_state(zero)
        if ok:
            warnings.append(f"armazenamento externo recuperado; contador USB zerado por sobrescrita ({exc}).")
        else:
            warnings.append(f"falha ao zerar contador de recuperação USB: unlink={exc}; write={err}")


def find_local_config_path() -> Optional[Path]:
    candidates = [
        Path.cwd() / "config.txt",
        Path(__file__).resolve().parent / "config.txt",
        Path("/home/fishcam/poracam/config.txt"),
        Path("/home/pi/poracam/config.txt"),
    ]
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def determine_config_path(args: argparse.Namespace) -> Tuple[Optional[Path], str, bool, List[str]]:
    warnings: List[str] = []
    if args.config is not None:
        return Path(args.config).expanduser().resolve(), "cli", False, warnings
    if not args.ignore_external_storage:
        external = wait_for_external_config_path(warnings)
        if external is not None:
            return external, "external", True, warnings
    local = find_local_config_path()
    if local is not None:
        return local, "local", False, warnings
    warnings.append("Nenhum config.txt encontrado; usando defaults internos.")
    return None, "none", False, warnings


def load_config_file(path: Optional[Path]) -> Tuple[Dict[str, Any], Optional[str], List[str], bool]:
    warnings: List[str] = []
    media_dir_was_defined = False
    if path is None:
        return {}, None, warnings, media_dir_was_defined
    if not path.exists():
        warnings.append(f"Arquivo de configuração não encontrado: {path}")
        return {}, None, warnings, media_dir_was_defined
    config: Dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            original = line.rstrip("\n")
            stripped = original.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                warnings.append(f"{path}:{line_number}: linha ignorada sem '=': {original}")
                continue
            raw_key, raw_value = stripped.split("=", 1)
            raw_key = raw_key.strip()
            raw_value = raw_value.strip()
            if not raw_key:
                warnings.append(f"{path}:{line_number}: chave vazia ignorada")
                continue
            normalized_key = raw_key.lower()
            if normalized_key not in CONFIG_KEY_ALIASES:
                warnings.append(f"{path}:{line_number}: chave desconhecida ignorada: {raw_key}")
                continue
            canonical_key = CONFIG_KEY_ALIASES[normalized_key]
            try:
                parsed = parse_value(canonical_key, raw_value)
            except Exception as exc:
                raise ValueError(f"{path}:{line_number}: erro ao ler '{raw_key}': {exc}")
            if canonical_key == "delete_h264_after_mp4":
                config["keep_h264"] = not bool(parsed)
            else:
                config[canonical_key] = parsed
            if canonical_key == "media_dir":
                media_dir_was_defined = True
    return config, str(path), warnings, media_dir_was_defined



def sanitize_session_name(value: str) -> str:
    """
    Sanitizes the session/campaign name for filenames.

    Keeps only ASCII letters, numbers, '-' and '_'.
    Empty values fall back to DEFAULT_SESSION_NAME.
    """
    raw = str(value or "").strip()
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "_", raw)
    sanitized = sanitized.strip("_-")
    if not sanitized:
        sanitized = DEFAULT_SESSION_NAME
    return sanitized[:40]


def minutes_to_seconds(value: float, field_name: str) -> int:
    if value <= 0:
        raise ValueError(f"{field_name} precisa ser maior que zero.")
    return int(round(value * 60.0))


def apply_user_config_to_runtime(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Converts the simplified user-facing config into the internal runtime config.
    """
    session_name = sanitize_session_name(str(config.get("session_name", DEFAULT_SESSION_NAME)))

    record_duration_min = float(config.get("record_duration_min", 1.0))
    cycle_period_min = float(config.get("cycle_period_min", 5.0))
    video_quality = str(config.get("video_quality", "high")).strip().lower()

    if video_quality not in VIDEO_PROFILES:
        raise ValueError(
            "video_quality precisa ser uma das opções: "
            + ", ".join(sorted(VIDEO_PROFILES.keys()))
            + f". Recebido: {video_quality}"
        )

    duration_s = minutes_to_seconds(record_duration_min, "record_duration_min")
    cycle_period_s = minutes_to_seconds(cycle_period_min, "cycle_period_min")

    if cycle_period_s <= duration_s:
        raise ValueError(
            "cycle_period_min precisa ser maior que record_duration_min para haver "
            "tempo de processamento, desligamento e economia de energia. "
            f"Recebido: record_duration_min={record_duration_min}, "
            f"cycle_period_min={cycle_period_min}"
        )

    profile = VIDEO_PROFILES[video_quality]

    config["session_name"] = session_name
    config["record_duration_min"] = record_duration_min
    config["cycle_period_min"] = cycle_period_min
    config["video_quality"] = video_quality

    config["duration"] = duration_s
    config["cycle_period_s"] = cycle_period_s

    config["segment_duration_s"] = MAX_RECORD_DURATION_PER_SEGMENT_S
    config["enable_segment_split"] = ENABLE_SEGMENT_SPLIT

    config["width"] = int(profile["width"])
    config["height"] = int(profile["height"])
    config["fps"] = int(profile["fps"])
    config["bitrate"] = int(profile["bitrate"])

    # Re-assert internal/developer constants so they cannot be changed from config.txt.
    config["run_mode"] = "single"
    config["audio_device"] = AUDIO_DEVICE
    config["audio_format"] = AUDIO_FORMAT
    config["audio_buffer_time_us"] = AUDIO_BUFFER_TIME_US
    config["keep_h264"] = KEEP_H264
    config["start_delay"] = START_DELAY_S
    config["extra_timeout"] = EXTRA_TIMEOUT_S
    config["max_storage_percent"] = MAX_STORAGE_PERCENT
    config["prefer_external_storage"] = True
    config["allow_local_fallback"] = True
    config["minimum_off_time_s"] = MINIMUM_OFF_TIME_S
    config["shutdown_delay_s"] = SHUTDOWN_DELAY_S
    config["require_startup_schedule_before_shutdown"] = REQUIRE_STARTUP_SCHEDULE_BEFORE_SHUTDOWN
    config["camera_conflict_policy"] = CAMERA_CONFLICT_POLICY
    config["camera_startup_check_s"] = CAMERA_STARTUP_CHECK_S
    config["camera_stop_timeout_s"] = CAMERA_STOP_TIMEOUT_S

    return config




def led_path() -> Path:
    return Path("/sys/class/leds") / LED_NAME


def led_write(filename: str, value: str) -> bool:
    if not LED_STATUS_ENABLED:
        return False
    try:
        path = led_path() / filename
        if not path.exists():
            return False
        path.write_text(str(value))
        return True
    except Exception:
        return False


def led_solid_on() -> None:
    """LED on means Raspberry is powered and no blocking field-check error is active."""
    led_write("trigger", "none")
    led_write("brightness", LED_OK_BRIGHTNESS)


def led_off() -> None:
    led_write("trigger", "none")
    led_write("brightness", LED_OFF_BRIGHTNESS)


def led_checking() -> None:
    """Slow blink while initial campaign check is in progress."""
    if not LED_STATUS_ENABLED:
        return
    if not led_write("trigger", "timer"):
        return
    led_write("delay_on", str(LED_CHECK_DELAY_MS))
    led_write("delay_off", str(LED_CHECK_DELAY_MS))


def led_error_blink() -> None:
    """Fast blink; intentionally leaves the kernel LED timer active."""
    if not LED_STATUS_ENABLED:
        return
    if not led_write("trigger", "timer"):
        return
    led_write("delay_on", str(LED_ERROR_DELAY_MS))
    led_write("delay_off", str(LED_ERROR_DELAY_MS))


def write_ready_status_file(
    status_dir: Path,
    ready: bool,
    version: str,
    checks: List[Tuple[str, bool, str]],
    action: str,
    error_message: Optional[str] = None,
) -> None:
    ensure_dir(status_dir)
    path = status_dir / READY_STATUS_FILE_NAME
    lines = [
        f"PRONTO PARA CAMPO: {'SIM' if ready else 'NAO'}",
        f"Data/hora: {iso_now()}",
        f"Versao: {version}",
        "",
        "Verificacoes:",
    ]
    for label, ok, detail in checks:
        status = "OK" if ok else "ERRO"
        if detail:
            lines.append(f"[{status}] {label}: {detail}")
        else:
            lines.append(f"[{status}] {label}")
    if error_message:
        lines.extend(["", f"Erro: {error_message}"])
    lines.extend(["", f"Acao: {action}"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def consume_or_mark_time_file_after_check(config: Dict[str, Any], ok: bool, warnings: List[str]) -> None:
    raw = config.get("_poracam_time_set_file")
    if not raw:
        return
    time_file = Path(str(raw))
    if ok:
        try:
            if time_file.exists():
                time_file.unlink()
                warnings.append(f"TIME SET OK: arquivo consumido apos checklist de campo: {time_file.name}")
        except Exception as exc:
            warnings.append(f"Checklist OK, mas nao foi possivel apagar {time_file}: {exc}")
    else:
        try:
            if time_file.exists():
                error_path = time_file.with_name(time_file.name + TIME_SET_ERROR_SUFFIX)
                if error_path.exists():
                    error_path.unlink()
                time_file.rename(error_path)
                warnings.append(f"Checklist falhou; arquivo de hora renomeado para indicar erro: {error_path}")
        except Exception as exc:
            warnings.append(f"Checklist falhou; nao foi possivel renomear arquivo de hora: {exc}")



def write_field_check_failure_status(
    *,
    status_dir: Path,
    config: Dict[str, Any],
    config_source_type: str,
    external_storage_used: bool,
    storage_info: Optional[Dict[str, Any]],
    audio_resolution: Optional[Dict[str, Any]],
    error_message: str,
    action: str = "Nao fechar o case. Corrigir o problema indicado, desligar a alimentacao e reiniciar o equipamento.",
) -> None:
    checks = build_basic_ready_checks(
        config_source_type=config_source_type,
        external_storage_used=external_storage_used,
        storage_info=storage_info,
        audio_resolution=audio_resolution,
    )
    if config.get("_poracam_field_check_requested"):
        checks.append((
            "Ajuste de data/hora por SET_TIME.txt",
            bool(config.get("_poracam_time_set_ok", False)),
            str(config.get("_poracam_time_set_requested_datetime", "")),
        ))
    write_ready_status_file(
        status_dir,
        False,
        PROJECT_VERSION,
        checks,
        action,
        error_message,
    )


def build_basic_ready_checks(
    config_source_type: str,
    external_storage_used: bool,
    storage_info: Optional[Dict[str, Any]],
    audio_resolution: Optional[Dict[str, Any]],
) -> List[Tuple[str, bool, str]]:
    checks: List[Tuple[str, bool, str]] = []
    checks.append(("Configuracao carregada do pendrive", config_source_type == "external", config_source_type))
    checks.append(("Armazenamento externo em uso", bool(external_storage_used), str(external_storage_used)))
    if storage_info is not None:
        checks.append(("Espaco livre suficiente", float(storage_info.get("free_mb", 0)) >= float(MIN_FREE_MB_BEFORE_RECORDING), f"{storage_info.get('free_mb')} MB livres"))
    else:
        checks.append(("Espaco livre suficiente", False, "nao verificado"))
    if audio_resolution is not None:
        checks.append(("Audio detectado", bool(audio_resolution.get("selected")), str(audio_resolution.get("selected"))))
    else:
        checks.append(("Audio detectado", False, "nao verificado"))
    return checks


def find_time_set_file(config_source: Optional[str], source_type: str) -> Optional[Path]:
    """Find a one-shot time set command file beside PORACAM/config.txt."""
    if not TIME_SET_ENABLED:
        return None
    if source_type != "external" or not config_source:
        return None
    config_path = Path(config_source)
    poracam_root = config_path.parent
    for name in TIME_SET_FILE_NAMES:
        candidate = poracam_root / name
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def parse_time_set_file(path: Path) -> str:
    """
    Read the first non-empty, non-comment line from the time set file.

    Accepted examples:
      2026-07-17 11:48:00 -04
      2026-07-17 11:48:00 -0400
      2026-07-17T11:48:00-04:00
      2026-07-17 11:48:00
    """
    text = path.read_text(encoding="utf-8").splitlines()
    for raw in text:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Also accept key=value style.
        if "=" in line:
            key, value = line.split("=", 1)
            if key.strip().lower() in ("time", "datetime", "date_time", "data_hora", "data"):
                line = value.strip()
            else:
                continue
        return line
    raise ValueError(f"Arquivo de data/hora vazio ou sem linha válida: {path}")


def validate_time_set_value(value: str) -> str:
    """
    Validate and normalize a user-supplied datetime string for `date -s`.

    This intentionally accepts a small set of explicit formats to avoid
    accidental parsing of ambiguous dates.
    """
    raw = value.strip()
    patterns = [
        r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}$",
        r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*[+-]\d{2}$",
        r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\s*[+-]\d{4}$",
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$",
    ]
    if not any(re.match(pattern, raw) for pattern in patterns):
        raise ValueError(
            "Formato de data/hora inválido. Use, por exemplo: "
            "2026-07-17 11:48:00 -04"
        )

    # Basic semantic validation for the date/time part.
    normalized_for_dt = raw.replace("T", " ")
    dt_part = normalized_for_dt[:19]
    dt.datetime.strptime(dt_part, "%Y-%m-%d %H:%M:%S")

    return raw


def detect_wittypi_dir(config: Dict[str, Any]) -> Optional[Path]:
    configured = str(config.get("wittypi_dir", "") or "").strip()
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    candidates.extend(WITTYPI_DIR_CANDIDATES)

    for candidate in candidates:
        path = Path(candidate).expanduser()
        if (path / "utilities.sh").exists():
            return path.resolve()
    return None


def set_system_time_and_rtc(datetime_value: str, wittypi_dir: Optional[Path]) -> Tuple[bool, List[str]]:
    """
    Set Linux system time and, if available, write system time into Witty Pi RTC.
    Returns (ok, messages).
    """
    messages: List[str] = []

    # Stop NTP so it does not immediately fight the manual field time.
    rc, out, err = run_quiet(["/usr/bin/timedatectl", "set-ntp", "false"], timeout_s=15)
    messages.append(f"timedatectl set-ntp false: rc={rc}; msg={(out + err).strip()[-300:]}")
    # Do not fail only because timedatectl is unavailable on a minimal image.

    date_bin = shutil.which("date") or "/bin/date"
    rc, out, err = run_quiet([date_bin, "-s", datetime_value], timeout_s=15)
    messages.append(f"date -s: rc={rc}; msg={(out + err).strip()[-300:]}")
    if rc != 0:
        return False, messages

    if wittypi_dir is not None:
        utilities = wittypi_dir / "utilities.sh"
        cmd = f'. "{utilities}"; system_to_rtc; check_sys_and_rtc_time'
        rc, out, err = run_quiet(["/bin/bash", "-c", cmd], timeout_s=30)
        messages.append(f"Witty Pi system_to_rtc: rc={rc}; msg={(out + err).strip()[-1000:]}")
        if rc != 0:
            return False, messages
    else:
        messages.append("Witty Pi utilities.sh não encontrado; RTC não foi atualizado.")
        return False, messages

    return True, messages


def handle_time_set_command(config: Dict[str, Any], config_source: Optional[str], source_type: str, warnings: List[str]) -> List[str]:
    """
    Process a one-shot USB time set file, if present.

    In v0.8.3.4, SET_TIME.txt also marks the beginning of a new campaign:
      - set system time;
      - write system time to Witty Pi RTC;
      - request a short field check recording;
      - consume SET_TIME.txt only after the field check succeeds.
    """
    time_file = find_time_set_file(config_source, source_type)
    if time_file is None:
        return warnings

    config["_poracam_field_check_requested"] = True
    config["_poracam_time_set_file"] = str(time_file)

    poracam_root = time_file.parent
    status_dir = poracam_root / "status"
    try:
        ensure_dir(status_dir)
    except Exception:
        pass

    try:
        raw_value = parse_time_set_file(time_file)
        datetime_value = validate_time_set_value(raw_value)
        wittypi_dir = detect_wittypi_dir(config)
        ok, messages = set_system_time_and_rtc(datetime_value, wittypi_dir)

        if not ok:
            msg = "Falha ao aplicar SET_TIME: " + " | ".join(messages)
            warnings.append(msg)
            config["_poracam_blocking_startup_error"] = msg
            error_path = time_file.with_name(time_file.name + TIME_SET_ERROR_SUFFIX)
            try:
                if error_path.exists():
                    error_path.unlink()
                time_file.rename(error_path)
                warnings.append(f"Arquivo de hora renomeado para indicar erro: {error_path}")
            except Exception as rename_exc:
                warnings.append(f"Não foi possível renomear arquivo de hora com erro: {rename_exc}")
            return warnings

        done_file = status_dir / TIME_SET_DONE_FILE_NAME
        done_lines = [
            f"Poracam time set: ok",
            f"Applied at: {iso_now()}",
            f"Requested datetime: {datetime_value}",
            f"Command file pending field check: {time_file}",
            f"Witty Pi dir: {wittypi_dir}",
            "Messages:",
        ]
        done_lines.extend(f"- {m}" for m in messages)
        done_file.write_text("\n".join(done_lines) + "\n", encoding="utf-8")

        config["_poracam_time_set_ok"] = True
        config["_poracam_time_set_requested_datetime"] = datetime_value
        warnings.append(
            f"TIME SET OK: data/hora aplicada a partir de {time_file.name}; "
            "checklist inicial de campo sera executado antes de consumir o arquivo."
        )
        return warnings

    except Exception as exc:
        msg = f"Falha ao processar arquivo de data/hora {time_file}: {exc}"
        warnings.append(msg)
        config["_poracam_blocking_startup_error"] = msg
        error_path = time_file.with_name(time_file.name + TIME_SET_ERROR_SUFFIX)
        try:
            if error_path.exists():
                error_path.unlink()
            time_file.rename(error_path)
            warnings.append(f"Arquivo de hora renomeado para indicar erro: {error_path}")
        except Exception as rename_exc:
            warnings.append(f"Não foi possível renomear arquivo de hora com erro: {rename_exc}")
        return warnings


def merge_config(args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[str], str, bool, List[str]]:
    final_config = dict(DEFAULT_CONFIG)
    config_path, source_type, external_used, warnings = determine_config_path(args)

    file_config, config_source, file_warnings, _media_dir_was_defined = load_config_file(config_path)
    warnings.extend(file_warnings)
    final_config.update(file_config)

    # User-facing CLI overrides, useful for developer tests.
    cli_overrides = {
        "session_name": args.session_name,
        "record_duration_min": args.record_duration_min,
        "cycle_period_min": args.cycle_period_min,
        "video_quality": args.video_quality,
    }
    for key, value in cli_overrides.items():
        if value is not None:
            final_config[key] = value

    final_config = apply_user_config_to_runtime(final_config)

    # Storage path is not exposed to the user:
    # external config -> use PORACAM/media on that device;
    # local config/default -> use internal DEFAULT_MEDIA_DIR.
    if external_used and config_path is not None:
        poracam_root = config_path.parent
        final_config["media_dir"] = str(poracam_root / "media")
        warnings.append(f"armazenamento externo detectado; usando automaticamente {final_config['media_dir']}")
    else:
        final_config["media_dir"] = DEFAULT_MEDIA_DIR

    if args.ignore_external_storage:
        final_config["prefer_external_storage"] = False

    # Power control is intentionally not user-facing in config.txt.
    # It is enabled by the Witty Pi afterStartup wrapper using --power-control.
    if args.power_control:
        final_config["power_control_enabled"] = True
    if args.no_power_control:
        final_config["power_control_enabled"] = False
    if args.power_dry_run:
        final_config["power_dry_run"] = True
        final_config["power_control_enabled"] = True
    if args.wittypi_dir:
        final_config["wittypi_dir"] = args.wittypi_dir

    power_control_requires_external = (
        bool(final_config.get("power_control_enabled", False))
        and bool(final_config.get("require_external_storage_in_power_control", REQUIRE_EXTERNAL_STORAGE_IN_POWER_CONTROL))
        and not args.ignore_external_storage
    )

    if power_control_requires_external and external_used:
        # Any successful external-storage boot ends the transient recovery streak.
        reset_external_storage_recovery_state(warnings)

    if power_control_requires_external and not external_used:
        final_config["_poracam_external_required_missing"] = True
        warnings.append(
            "modo power-control requer armazenamento externo; "
            "fallback local foi bloqueado para evitar gravar dados no cartão SD."
        )

        if EXTERNAL_STORAGE_RECOVERY_ENABLED:
            recovery = register_external_storage_recovery_failure(warnings)
            failures = int(recovery.get("consecutive_failures", 0))
            persisted = bool(recovery.get("persisted", False))
            final_config["_poracam_storage_recovery_state"] = recovery

            # failure #1 schedules recovery retry #1; failures 2 and 3 schedule retries #2/#3.
            # failure #4 means the initial failure + all three recovery boots have failed.
            if persisted and failures <= EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES:
                final_config["_poracam_storage_recovery_pending"] = True
                final_config["_poracam_storage_recovery_retry_number"] = failures
                final_config["_poracam_storage_recovery_delay_s"] = EXTERNAL_STORAGE_RECOVERY_RETRY_DELAY_S
                final_config["_poracam_stop_scheduling"] = False
                final_config["_poracam_stop_scheduling_reason"] = ""
                warnings.append(
                    "armazenamento externo ausente; recuperação automática armada: "
                    f"retry {failures}/{EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES} em "
                    f"{EXTERNAL_STORAGE_RECOVERY_RETRY_DELAY_S}s."
                )
            else:
                final_config["_poracam_storage_recovery_pending"] = False
                final_config["_poracam_stop_scheduling"] = True
                if persisted:
                    final_config["_poracam_stop_scheduling_reason"] = "external_storage_recovery_exhausted"
                    warnings.append(
                        "armazenamento externo permaneceu ausente após a falha inicial e "
                        f"{EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES} retries; novos startups serão interrompidos."
                    )
                else:
                    final_config["_poracam_stop_scheduling_reason"] = "external_storage_recovery_state_unavailable"
                    warnings.append(
                        "contador de recuperação USB não pôde ser persistido; por segurança, "
                        "novos startups serão interrompidos para evitar retry infinito."
                    )
        else:
            final_config["_poracam_stop_scheduling"] = True
            final_config["_poracam_stop_scheduling_reason"] = "external_storage_missing"

    return final_config, config_source, source_type, external_used, warnings



def run_quiet(command: List[str], timeout_s: int = 10) -> Tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", exc.stderr or f"timeout after {timeout_s}s"
    except Exception as exc:
        return 1, "", str(exc)


def parse_arecord_capture_devices(output: str) -> List[Tuple[int, int]]:
    """Return unique physical ALSA capture addresses as (card, device)."""
    devices: List[Tuple[int, int]] = []
    pattern = re.compile(r"card\s+(\d+):.*device\s+(\d+):", re.IGNORECASE)
    for line in output.splitlines():
        match = pattern.search(line)
        if match:
            addr = (int(match.group(1)), int(match.group(2)))
            if addr not in devices:
                devices.append(addr)
    return devices


def audio_transport_for_device(device: str) -> str:
    dev = str(device or "").strip().lower()
    if dev.startswith("hw:"):
        return "direct-hardware"
    if dev.startswith("plughw:"):
        return "alsa-plug-fallback"
    if dev == "default":
        return "alsa-default-fallback"
    return "custom"


def probe_audio_device(device: str, log_file: Path) -> Tuple[bool, str]:
    probe_path = Path("/tmp/poracam_audio_probe.wav")
    try:
        if probe_path.exists():
            probe_path.unlink()
    except Exception:
        pass

    cmd = [
        "/usr/bin/arecord",
        "-D",
        device,
        "-f",
        AUDIO_FORMAT,
        "-r",
        str(AUDIO_RATE_HZ),
        "-c",
        str(AUDIO_CHANNELS),
        "--buffer-time",
        str(AUDIO_BUFFER_TIME_US),
        "-d",
        str(AUDIO_PROBE_SECONDS),
        str(probe_path),
    ]

    rc, out, err = run_quiet(cmd, timeout_s=AUDIO_PROBE_SECONDS + 8)

    try:
        if probe_path.exists():
            probe_path.unlink()
    except Exception:
        pass

    msg = (out + "\n" + err).strip()
    if rc == 0:
        append_log(log_file, f"Audio probe OK: {device}")
        return True, msg

    append_log(log_file, f"Audio probe failed: {device}; rc={rc}; msg={msg}")
    return False, msg


def audio_error_is_busy(message: str) -> bool:
    text = str(message or "").lower()
    return (
        "device or resource busy" in text
        or "dispositivo ou recurso está ocupado" in text
        or "dispositivo ou recurso esta ocupado" in text
        or "resource busy" in text
    )


def resolve_audio_device(config: Dict[str, Any], log_file: Path) -> Dict[str, Any]:
    """
    Resolve the audio capture device with retry.

    v0.8.3.4 rationale:
    after Witty Pi powers the Raspberry, the USB audio interface may not be immediately
    enumerated by ALSA. Prefer direct hw:<card>,<device> access because simultaneous
    camera/audio tests showed overruns with plughw while hw remained stable. Keep retry
    and plughw/default only as fallbacks.
    """
    requested = str(config.get("audio_device", AUDIO_DEVICE)).strip()
    auto_mode = requested.lower() in ("auto", "")

    result: Dict[str, Any] = {
        "requested": requested,
        "selected": None,
        "method": "auto-hw-first-retry" if auto_mode else "fixed-retry",
        "transport": None,
        "buffer_time_us": AUDIO_BUFFER_TIME_US,
        "ready_timeout_s": AUDIO_READY_TIMEOUT_S,
        "retry_interval_s": AUDIO_READY_RETRY_INTERVAL_S,
        "arecord_l": None,
        "candidates": [],
        "probe": [],
        "attempts": [],
    }

    deadline = time.monotonic() + float(AUDIO_READY_TIMEOUT_S)
    attempt = 0
    last_error = ""

    while True:
        attempt += 1
        attempt_info: Dict[str, Any] = {
            "attempt": attempt,
            "timestamp": iso_now(),
            "arecord_l": None,
            "candidates": [],
            "probe": [],
        }

        if not auto_mode:
            candidates = [requested]
        else:
            candidates: List[str] = []

            rc, out, err = run_quiet(["/usr/bin/arecord", "-l"], timeout_s=10)
            arecord_l_text = (out + "\n" + err).strip()
            attempt_info["arecord_l"] = arecord_l_text[-2000:]
            result["arecord_l"] = arecord_l_text[-2000:]

            if rc == 0:
                parsed = parse_arecord_capture_devices(out)
                direct = [f"hw:{card},{device}" for card, device in parsed]
                plug = [f"plughw:{card},{device}" for card, device in parsed]
                candidates.extend(direct)
                candidates.extend(plug)
                if parsed:
                    append_log(
                        log_file,
                        f"Audio attempt {attempt}: arecord -l found physical capture addresses: {parsed}; "
                        f"direct candidates first: {direct}",
                    )
                else:
                    append_log(log_file, f"Audio attempt {attempt}: arecord -l returned no capture devices.")
            else:
                append_log(log_file, f"Audio attempt {attempt}: arecord -l failed; rc={rc}; msg={arecord_l_text}")

            # Conservative fallbacks for images where arecord -l is temporarily incomplete.
            # Direct hardware remains preferred; plug/default are only fallback paths.
            candidates.extend([
                "hw:1,0", "plughw:1,0",
                "hw:0,0", "plughw:0,0",
                "default",
            ])

        unique_candidates: List[str] = []
        seen = set()
        for dev in candidates:
            if dev and dev not in seen:
                seen.add(dev)
                unique_candidates.append(dev)

        attempt_info["candidates"] = unique_candidates
        result["candidates"] = unique_candidates

        busy_direct_addresses = set()
        for dev in unique_candidates:
            if dev.startswith("plughw:"):
                address = dev.split(":", 1)[1]
                if address in busy_direct_addresses:
                    append_log(
                        log_file,
                        f"Audio probe skipped: {dev}; matching hw:{address} was temporarily busy, "
                        "so direct hardware will be retried instead of falling back to ALSA plug.",
                    )
                    continue

            ok, msg = probe_audio_device(dev, log_file)
            probe_item = {"device": dev, "ok": ok, "message": msg[-500:]}
            attempt_info["probe"].append(probe_item)
            result["probe"].append(probe_item)
            if ok:
                transport = audio_transport_for_device(dev)
                result["selected"] = dev
                result["transport"] = transport
                result["attempts"].append(attempt_info)
                config["audio_device"] = dev
                config["_poracam_audio_transport"] = transport
                append_log(log_file, f"Selected audio device after {attempt} attempt(s): {dev}")
                append_log(log_file, f"Audio transport: {transport}")
                append_log(log_file, f"Audio buffer requested: {AUDIO_BUFFER_TIME_US} us")
                return result

            if dev.startswith("hw:") and audio_error_is_busy(msg):
                busy_direct_addresses.add(dev.split(":", 1)[1])
                append_log(log_file, f"Audio direct hardware temporarily busy: {dev}; preserving hw preference for next retry.")
            last_error = msg

        result["attempts"].append(attempt_info)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        sleep_s = min(float(AUDIO_READY_RETRY_INTERVAL_S), remaining)
        append_log(log_file, f"Audio not ready yet after attempt {attempt}; retrying in {sleep_s:.1f}s.")
        time.sleep(sleep_s)

    raise RuntimeError(
        "Nenhum dispositivo de áudio funcionou após "
        f"{AUDIO_READY_TIMEOUT_S}s de espera. "
        "Rode `arecord -l` e teste manualmente "
        "`arecord -D hw:<card>,<device> -f S16_LE -r 44100 -c 1 -d 10 teste.wav`. "
        f"Último erro: {last_error}"
    )


def run_command(command: List[str], log_file: Path, check: bool = True) -> subprocess.CompletedProcess:
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n[{iso_now()}] RUN: {' '.join(map(str, command))}\n")
        log.flush()
        t0 = time.monotonic()
        result = subprocess.run(command, stdout=log, stderr=log, check=False)
        elapsed = time.monotonic() - t0
        log.write(f"[{iso_now()}] RETURN CODE: {result.returncode}\n")
        log.write(f"[{iso_now()}] ELAPSED: {elapsed:.3f} s\n")
        log.flush()
    if check and result.returncode != 0:
        raise RuntimeError(f"Comando falhou com return code {result.returncode}: {command}")
    return result


def start_process(command: List[str], log_file: Path) -> subprocess.Popen:
    log = log_file.open("ab", buffering=0)
    log.write(f"\n[{iso_now()}] START: {' '.join(map(str, command))}\n".encode("utf-8"))
    proc = subprocess.Popen(command, stdout=log, stderr=log, preexec_fn=os.setsid)
    proc._poracam_log_handle = log  # type: ignore[attr-defined]
    return proc


def close_proc_log(proc: subprocess.Popen) -> None:
    handle = getattr(proc, "_poracam_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def stop_process(proc: subprocess.Popen, name: str, log_file: Path, timeout: float = 5.0) -> None:
    if proc.poll() is not None:
        close_proc_log(proc)
        return
    append_log(log_file, f"STOP requested for {name}, pid={proc.pid}")
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        append_log(log_file, f"{name} did not stop after SIGTERM; sending SIGKILL")
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=timeout)
    finally:
        close_proc_log(proc)


KNOWN_CAMERA_PROCESS_NAMES = ["raspimjpeg", "motion", "raspivid", "raspistill"]


def list_processes_by_name(names: List[str]) -> List[Dict[str, Any]]:
    """Lista processos que podem estar usando a câmera."""
    current_pid = os.getpid()
    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    matches: List[Dict[str, Any]] = []
    if result.returncode != 0:
        return matches
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == current_pid:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) >= 3 else ""
        for name in names:
            if comm == name or f"/{name}" in args or name in args.split():
                matches.append({"pid": pid, "comm": comm, "args": args, "matched_name": name})
                break
    return matches


def stop_pid(pid: int, log_file: Path, timeout: float) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        append_log(log_file, f"Permission denied when trying to stop pid={pid}")
        return False

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        append_log(log_file, f"Permission denied when trying to kill pid={pid}")
        return False

    time.sleep(0.2)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


def handle_camera_conflicts(config: Dict[str, Any], log_file: Path) -> Dict[str, Any]:
    """Aplica política de conflito de câmera antes de iniciar a gravação."""
    policy = str(config["camera_conflict_policy"]).lower()
    timeout = float(config["camera_stop_timeout_s"])
    before = list_processes_by_name(KNOWN_CAMERA_PROCESS_NAMES)
    actions: List[Dict[str, Any]] = []

    if before:
        append_log(log_file, f"Camera conflict candidates found: {before}")
    else:
        append_log(log_file, "No known camera conflict process found")

    if policy == "ignore":
        return {"policy": policy, "known_process_names": KNOWN_CAMERA_PROCESS_NAMES, "before": before, "actions": actions, "after": list_processes_by_name(KNOWN_CAMERA_PROCESS_NAMES)}

    if policy == "error_only":
        if before:
            raise RuntimeError(f"Câmera possivelmente ocupada por processo conhecido: {before}")
        return {"policy": policy, "known_process_names": KNOWN_CAMERA_PROCESS_NAMES, "before": before, "actions": actions, "after": list_processes_by_name(KNOWN_CAMERA_PROCESS_NAMES)}

    if policy == "stop_known_processes":
        for proc in before:
            pid = int(proc["pid"])
            append_log(log_file, f"Stopping camera conflict process pid={pid}, comm={proc['comm']}")
            stopped = stop_pid(pid, log_file, timeout=timeout)
            actions.append({"pid": pid, "comm": proc["comm"], "stopped": stopped})
        after = list_processes_by_name(KNOWN_CAMERA_PROCESS_NAMES)
        if after:
            raise RuntimeError(f"Não foi possível liberar processos conhecidos da câmera: {after}")
        return {"policy": policy, "known_process_names": KNOWN_CAMERA_PROCESS_NAMES, "before": before, "actions": actions, "after": after}

    raise ValueError(f"camera_conflict_policy inválida: {policy}")


def file_info(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"exists": False, "size_bytes": 0}
    stat = path.stat()
    return {
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime": dt.datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def remove_file(path: Path, log_file: Path) -> bool:
    if not path.exists():
        append_log(log_file, f"File not found for removal: {path}")
        return False
    path.unlink()
    append_log(log_file, f"Removed temporary file: {path}")
    return True


class ExternalStorageRequiredError(RuntimeError):
    """Raised when autonomous mode requires a PORACAM pendrive but none is available."""
    pass


class StorageFullError(RuntimeError):
    """Raised when storage is too full to safely start another recording cycle."""
    pass

class FieldCheckError(RuntimeError):
    """Raised when the initial field/campaign readiness check fails."""
    pass


def get_storage_info(path: Path) -> Dict[str, Any]:
    usage = shutil.disk_usage(str(path))
    used_bytes = usage.total - usage.free
    used_percent = (used_bytes / usage.total) * 100 if usage.total else 0.0
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": used_bytes,
        "free_bytes": usage.free,
        "used_percent": round(used_percent, 3),
        "free_mb": round(usage.free / (1024 * 1024), 3),
    }



def directory_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except Exception:
            return 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except Exception:
                continue
    except Exception:
        return total
    return total


def find_trash_usage(storage_root: Path) -> List[Dict[str, Any]]:
    """Find common Linux/macOS/Windows trash directories on the storage root."""
    results: List[Dict[str, Any]] = []
    try:
        root = Path(storage_root).expanduser().resolve()
    except Exception:
        root = Path(storage_root)

    candidates: List[Path] = []
    for name in TRASH_DIR_NAMES:
        candidates.append(root / name)

    # Also catch .Trash-1000, .Trash-1001, etc.
    try:
        for child in root.iterdir():
            if child.name.startswith(".Trash-") and child not in candidates:
                candidates.append(child)
    except Exception:
        pass

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            size_b = directory_size_bytes(candidate)
            results.append({
                "path": str(candidate),
                "size_bytes": size_b,
                "size_mb": round(size_b / (1024 * 1024), 3),
            })

    results.sort(key=lambda item: item.get("size_bytes", 0), reverse=True)
    return results


def storage_root_for_path(path: Path) -> Path:
    """Return the mount/storage root associated with media_dir/PORACAM path."""
    current = Path(path).expanduser().resolve()
    if current.name == "media" and current.parent.name == "PORACAM":
        return current.parent.parent
    if current.name == "PORACAM":
        return current.parent
    for parent in current.parents:
        if parent.name == "PORACAM":
            return parent.parent
    return current


def enrich_storage_with_trash(info: Dict[str, Any], media_dir: Path) -> Dict[str, Any]:
    if not TRASH_DETECTION_ENABLED:
        return info
    enriched = dict(info)
    root = storage_root_for_path(media_dir)
    trash = find_trash_usage(root)
    trash_total_b = sum(int(item.get("size_bytes", 0)) for item in trash)
    enriched["storage_root"] = str(root)
    enriched["trash"] = trash
    enriched["trash_total_bytes"] = trash_total_b
    enriched["trash_total_mb"] = round(trash_total_b / (1024 * 1024), 3)
    if enriched["trash_total_mb"] >= TRASH_WARNING_MIN_MB:
        enriched["trash_warning"] = (
            f"Lixeira do armazenamento ocupa {enriched['trash_total_mb']} MB. "
            "Esvazie a lixeira do pendrive ou use o script LIMPAR_PORACAM."
        )
    return enriched


def append_light_log(light_log_file: Path, event: str, **fields: Any) -> None:
    if not LIGHT_LOG_ENABLED:
        return
    try:
        ensure_dir(light_log_file.parent)
        parts = [iso_now(), event]
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).replace("\n", " ").replace("\r", " ")
            parts.append(f"{key}={text}")
        with light_log_file.open("a", encoding="utf-8") as f:
            f.write(" ".join(parts) + "\n")
    except Exception:
        pass


def write_metadata_file(path: Path, metadata: Dict[str, Any], log_file: Optional[Path] = None) -> None:
    if not METADATA_ENABLED:
        if log_file is not None:
            append_log(log_file, "Metadata disabled by METADATA_ENABLED=False; JSON not written.")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


def check_storage_or_fail(path: Path, max_storage_percent: float) -> Dict[str, Any]:
    info = enrich_storage_with_trash(get_storage_info(path), path)
    trash_msg = ""
    if info.get("trash_total_mb", 0) >= TRASH_WARNING_MIN_MB:
        trash_msg = (
            f" Possível causa: lixeira ocupa {info.get('trash_total_mb')} MB "
            f"em {info.get('storage_root')}."
        )
    if info["used_percent"] >= max_storage_percent:
        raise StorageFullError(
            f"Uso do armazenamento acima do limite: {info['used_percent']}% usado, limite={max_storage_percent}%."
            + trash_msg
        )
    if float(info["free_mb"]) < float(MIN_FREE_MB_BEFORE_RECORDING):
        raise StorageFullError(
            "Espaço livre abaixo do mínimo para iniciar gravação: "
            f"{info['free_mb']} MB livre, mínimo={MIN_FREE_MB_BEFORE_RECORDING} MB."
            + trash_msg
        )
    return info


def sync_filesystem(log_file: Path) -> float:
    t0 = time.monotonic()
    try:
        run_command(["sync"], log_file, check=False)
    except Exception as exc:
        append_log(log_file, f"WARNING: sync failed: {exc}")
    return time.monotonic() - t0


def validate_config(config: Dict[str, Any]) -> None:
    if int(config["duration"]) <= 0:
        raise ValueError("duration/record_duration_s precisa ser maior que zero.")
    if int(config["segment_duration_s"]) <= 0:
        raise ValueError("segment_duration_s precisa ser maior que zero.")

    if int(config["cycle_period_s"]) <= 0:
        raise ValueError("cycle_period_s precisa ser maior que zero.")
    if int(config["cycle_period_s"]) < int(config["duration"]):
        raise ValueError(f"cycle_period_s precisa ser maior ou igual a record_duration_s/duration. Recebido: cycle_period_s={config['cycle_period_s']}, duration={config['duration']}")
    if str(config["run_mode"]).lower() != "single":
        raise ValueError(f"Na v0.8.3.4, apenas run_mode=single é suportado. Recebido: run_mode={config['run_mode']}")
    for key in ("width", "height", "fps", "bitrate"):
        if int(config[key]) <= 0:
            raise ValueError(f"{key} precisa ser maior que zero.")
    if float(config["start_delay"]) < 0:
        raise ValueError("start_delay não pode ser negativo.")
    if float(config["extra_timeout"]) < 0:
        raise ValueError("extra_timeout não pode ser negativo.")
    if float(config["camera_startup_check_s"]) < 0:
        raise ValueError("camera_startup_check_s não pode ser negativo.")
    if float(config["camera_stop_timeout_s"]) < 0:
        raise ValueError("camera_stop_timeout_s não pode ser negativo.")
    if not str(config["media_dir"]).strip():
        raise ValueError("media_dir não pode ser vazio.")
    if not str(config["audio_device"]).strip():
        raise ValueError("audio_device não pode ser vazio.")
    if not str(config["audio_format"]).strip():
        raise ValueError("audio_format não pode ser vazio.")
    max_storage_percent = float(config["max_storage_percent"])
    if max_storage_percent <= 0 or max_storage_percent > 100:
        raise ValueError("max_storage_percent precisa estar entre 0 e 100.")
    policy = str(config["camera_conflict_policy"]).lower()
    if policy not in ("error_only", "stop_known_processes", "ignore"):
        raise ValueError("camera_conflict_policy precisa ser error_only, stop_known_processes ou ignore.")


def write_status_files(status_dir: Path, metadata: Dict[str, Any], status: str, error_message: Optional[str]) -> None:
    ensure_dir(status_dir)
    last_run_file = status_dir / "last_run.json"
    last_error_file = status_dir / "last_error.txt"
    status_txt_file = status_dir / "poracam_status.txt"
    metadata_enabled = bool(metadata.get("settings", {}).get("metadata_enabled", METADATA_ENABLED))
    metadata_display = metadata.get("paths", {}).get("metadata") if metadata_enabled else "desabilitado"
    summary = {
        "project": metadata.get("project"),
        "version": metadata.get("version"),
        "last_status": status,
        "last_start_time": metadata.get("start_time"),
        "last_end_time": metadata.get("end_time"),
        "video_mp4": metadata.get("paths", {}).get("video_mp4"),
        "audio_wav": metadata.get("paths", {}).get("audio_wav"),
        "metadata": metadata_display,
        "config_source": metadata.get("config_source"),
        "config_source_type": metadata.get("config_source_type"),
        "external_storage_used": metadata.get("external_storage_used"),
        "error": error_message,
    }
    with last_run_file.open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    lines = [
        f"Poracam status: {status}",
        f"Versao: {metadata.get('version')}",
        f"Inicio: {metadata.get('start_time')}",
        f"Fim: {metadata.get('end_time')}",
        f"Config: {metadata.get('config_source')}",
        f"Origem config: {metadata.get('config_source_type')}",
        f"Armazenamento externo: {metadata.get('external_storage_used')}",
        f"Video: {metadata.get('paths', {}).get('video_mp4')}",
        f"Audio: {metadata.get('paths', {}).get('audio_wav')}",
        f"Metadata: {metadata_display}",
    ]
    storage_before = metadata.get("storage", {}).get("before") or {}
    storage_after = metadata.get("storage", {}).get("after") or {}
    trash_warning = storage_after.get("trash_warning") or storage_before.get("trash_warning")
    if trash_warning:
        lines.append(f"Aviso armazenamento: {trash_warning}")
    if error_message:
        lines.append(f"Erro: {error_message}")
    with status_txt_file.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if error_message:
        with last_error_file.open("w", encoding="utf-8") as f:
            f.write(f"{iso_now()}\n{error_message}\n")
    elif last_error_file.exists():
        last_error_file.unlink()


def build_segment_plan(total_duration: int, segment_duration: int, enable_split: bool) -> List[int]:
    if not enable_split or total_duration <= segment_duration:
        return [total_duration]

    plan: List[int] = []
    remaining = total_duration
    while remaining > 0:
        current = min(segment_duration, remaining)
        plan.append(current)
        remaining -= current
    return plan


def segment_file_paths(stamp: str, segment_count: int, index: int, video_dir: Path, audio_dir: Path, temp_dir: Path) -> Tuple[Path, Path, Path, str]:
    if segment_count <= 1:
        suffix = ""
        label = "single"
    else:
        label = f"part{index:03d}"
        suffix = f"_{label}"

    temp_h264 = temp_dir / f"{stamp}{suffix}_video.h264"
    video_mp4 = video_dir / f"{stamp}{suffix}_video.mp4"
    audio_wav = audio_dir / f"{stamp}{suffix}_audio.wav"
    return temp_h264, video_mp4, audio_wav, label


def record_one_segment(
    *,
    config: Dict[str, Any],
    segment_index: int,
    segment_count: int,
    segment_duration: int,
    stamp: str,
    video_dir: Path,
    audio_dir: Path,
    temp_dir: Path,
    log_file: Path,
    raspivid: str,
    arecord: str,
    ffmpeg: str,
) -> Dict[str, Any]:
    temp_h264, video_mp4, audio_wav, label = segment_file_paths(
        stamp, segment_count, segment_index, video_dir, audio_dir, temp_dir
    )

    video_proc: Optional[subprocess.Popen] = None
    audio_proc: Optional[subprocess.Popen] = None
    audio_started = False
    h264_deleted = False

    timing: Dict[str, Optional[float]] = {
        "camera_startup_check_s": None,
        "recording_s": None,
        "video_mp4_processing_s": None,
        "delete_h264_s": None,
        "sync_s": None,
        "total_s": None,
    }

    segment_t0 = time.monotonic()
    segment_start = iso_now()

    video_cmd = [
        raspivid,
        "-n",
        "-t", str(int(segment_duration * 1000)),
        "-w", str(int(config["width"])),
        "-h", str(int(config["height"])),
        "-fps", str(int(config["fps"])),
        "-b", str(int(config["bitrate"])),
        "-o", str(temp_h264),
    ]

    audio_cmd = [
        arecord,
        "-D", str(config["audio_device"]),
        "-f", str(config["audio_format"]),
        "-r", str(int(config.get("audio_rate_hz", AUDIO_RATE_HZ))),
        "-c", str(int(config.get("audio_channels", AUDIO_CHANNELS))),
        "--buffer-time", str(int(config.get("audio_buffer_time_us", AUDIO_BUFFER_TIME_US))),
        "-d", str(int(segment_duration)),
        str(audio_wav),
    ]

    ffmpeg_cmd = [
        ffmpeg,
        "-y",
        "-framerate", str(int(config["fps"])),
        "-i", str(temp_h264),
        "-c:v", "copy",
        str(video_mp4),
    ]

    result: Dict[str, Any] = {
        "index": segment_index,
        "count": segment_count,
        "label": label,
        "status": "unknown",
        "start_time": segment_start,
        "end_time": None,
        "duration_requested_s": segment_duration,
        "duration_actual_s": None,
        "paths": {
            "video_mp4": str(video_mp4),
            "audio_wav": str(audio_wav),
            "temp_h264": str(temp_h264),
        },
        "commands": {
            "video": video_cmd,
            "audio": audio_cmd,
            "ffmpeg_video_mp4": ffmpeg_cmd,
        },
        "timing": timing,
        "files": {},
        "audio_started": False,
        "h264_deleted": False,
        "error": None,
    }

    try:
        append_log(log_file, f"Segment {segment_index}/{segment_count} started: duration={segment_duration}s, label={label}")
        append_log(log_file, f"Segment temporary H264: {temp_h264}")
        append_log(log_file, f"Segment final MP4: {video_mp4}")
        append_log(log_file, f"Segment audio WAV: {audio_wav}")

        recording_t0 = time.monotonic()

        video_proc = start_process(video_cmd, log_file)
        startup_t0 = time.monotonic()
        camera_startup_check_s = float(config["camera_startup_check_s"])
        if camera_startup_check_s > 0:
            time.sleep(camera_startup_check_s)
        timing["camera_startup_check_s"] = round(time.monotonic() - startup_t0, 3)

        if video_proc.poll() is not None:
            video_rc_early = video_proc.returncode
            close_proc_log(video_proc)
            raise RuntimeError(
                "raspivid falhou durante checagem inicial da câmera; "
                f"return code {video_rc_early}. Áudio não foi iniciado."
            )

        if float(config["start_delay"]) > 0:
            time.sleep(float(config["start_delay"]))

        audio_proc = start_process(audio_cmd, log_file)
        audio_started = True
        result["audio_started"] = True

        video_rc = video_proc.wait(timeout=segment_duration + float(config["extra_timeout"]) + camera_startup_check_s)
        audio_rc = audio_proc.wait(timeout=segment_duration + float(config["extra_timeout"]))

        close_proc_log(video_proc)
        close_proc_log(audio_proc)

        timing["recording_s"] = round(time.monotonic() - recording_t0, 3)
        append_log(log_file, f"Segment {segment_index}/{segment_count} video return code: {video_rc}")
        append_log(log_file, f"Segment {segment_index}/{segment_count} audio return code: {audio_rc}")
        append_log(log_file, f"Segment {segment_index}/{segment_count} recording elapsed: {timing['recording_s']} s")

        if video_rc != 0:
            raise RuntimeError(f"raspivid terminou com erro: return code {video_rc}")
        if audio_rc != 0:
            raise RuntimeError(f"arecord terminou com erro: return code {audio_rc}")
        if not temp_h264.exists() or temp_h264.stat().st_size <= 0:
            raise RuntimeError(f"Arquivo temporário H264 não foi criado corretamente: {temp_h264}")
        if not audio_wav.exists() or audio_wav.stat().st_size <= 0:
            raise RuntimeError(f"Arquivo de áudio WAV não foi criado corretamente: {audio_wav}")

        mp4_t0 = time.monotonic()
        run_command(ffmpeg_cmd, log_file, check=True)
        timing["video_mp4_processing_s"] = round(time.monotonic() - mp4_t0, 3)

        if not video_mp4.exists() or video_mp4.stat().st_size <= 0:
            raise RuntimeError(f"Arquivo MP4 final não foi criado corretamente: {video_mp4}")

        append_log(log_file, f"Segment {segment_index}/{segment_count} MP4 elapsed: {timing['video_mp4_processing_s']} s")

        if not bool(config["keep_h264"]):
            delete_t0 = time.monotonic()
            h264_deleted = remove_file(temp_h264, log_file)
            timing["delete_h264_s"] = round(time.monotonic() - delete_t0, 3)
        else:
            append_log(log_file, "Keeping H264 temporary file because keep_h264=true")
            timing["delete_h264_s"] = 0.0

        timing["sync_s"] = round(sync_filesystem(log_file), 3)
        result["status"] = "ok"

    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        append_log(log_file, f"Segment {segment_index}/{segment_count} ERROR: {exc}")

        if video_proc is not None and video_proc.poll() is None:
            stop_process(video_proc, "raspivid", log_file)
        if audio_proc is not None and audio_proc.poll() is None:
            stop_process(audio_proc, "arecord", log_file)

        timing["sync_s"] = round(sync_filesystem(log_file), 3)

    finally:
        result["end_time"] = iso_now()
        result["duration_actual_s"] = round(time.monotonic() - segment_t0, 3)
        timing["total_s"] = result["duration_actual_s"]
        result["timing"] = timing
        result["h264_deleted"] = h264_deleted
        result["files"] = {
            "video_mp4": file_info(video_mp4),
            "audio_wav": file_info(audio_wav),
            "temp_h264": file_info(temp_h264),
        }
        result["audio_started"] = audio_started

    return result



def find_wittypi_dir(config: Dict[str, Any]) -> Optional[Path]:
    configured = str(config.get("wittypi_dir") or "").strip()
    candidates: List[str] = []
    if configured:
        candidates.append(configured)
    candidates.extend(WITTYPI_DIR_CANDIDATES)

    for candidate in candidates:
        path = Path(candidate).expanduser().resolve()
        if (path / "utilities.sh").exists():
            return path
    return None


def find_power_script() -> Optional[Path]:
    """
    Locate the Bash wrapper used to interact with Witty Pi utilities.sh.

    The normal installation places poracam_wittypi_power.sh next to this Python file.
    """
    here = Path(__file__).resolve().parent
    candidate = here / WITTYPI_POWER_SCRIPT_NAME
    if candidate.exists():
        return candidate
    fallback = Path("/home/fishcam/poracam") / WITTYPI_POWER_SCRIPT_NAME
    if fallback.exists():
        return fallback
    return None


def compute_next_cycle_start_epoch(start_epoch: int, cycle_period_s: int, minimum_off_time_s: int, now_epoch: Optional[int] = None) -> int:
    """
    cycle_period_s is interpreted as the period between recording starts.

    The next startup target is start_epoch + N*cycle_period_s, advanced until it is
    at least now + minimum_off_time_s. This preserves fixed-period sampling when
    processing time varies.
    """
    if now_epoch is None:
        now_epoch = int(time.time())

    if cycle_period_s <= 0:
        raise ValueError("cycle_period_s precisa ser maior que zero.")
    if minimum_off_time_s < 0:
        minimum_off_time_s = 0

    target = int(start_epoch) + int(cycle_period_s)
    min_target = int(now_epoch) + int(minimum_off_time_s)

    while target < min_target:
        target += int(cycle_period_s)

    return target


def epoch_to_local_iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(int(epoch)).astimezone().isoformat(timespec="seconds")


def run_power_wrapper(
    action: str,
    target_epoch: Optional[int],
    wittypi_dir: Optional[Path],
    dry_run: bool,
    log_file: Path,
) -> Dict[str, Any]:
    script = find_power_script()
    result: Dict[str, Any] = {
        "action": action,
        "ok": False,
        "dry_run": dry_run,
        "script": str(script) if script else None,
        "wittypi_dir": str(wittypi_dir) if wittypi_dir else None,
        "target_epoch": target_epoch,
        "target_time": epoch_to_local_iso(target_epoch) if target_epoch else None,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "error": None,
    }

    if script is None:
        result["error"] = f"{WITTYPI_POWER_SCRIPT_NAME} não encontrado ao lado do poracam_record.py."
        append_log(log_file, f"POWER ERROR: {result['error']}")
        return result

    if wittypi_dir is None:
        result["error"] = "Diretório do Witty Pi com utilities.sh não encontrado."
        append_log(log_file, f"POWER ERROR: {result['error']}")
        return result

    cmd = [str(script), action, "--wittypi-dir", str(wittypi_dir)]
    if target_epoch is not None:
        cmd.extend(["--target-epoch", str(int(target_epoch))])
    if dry_run:
        cmd.append("--dry-run")

    append_log(log_file, f"POWER command: {' '.join(cmd)}")

    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60)
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout.strip()
        result["stderr"] = proc.stderr.strip()
        result["ok"] = proc.returncode == 0
        if result["stdout"]:
            append_log(log_file, f"POWER stdout: {result['stdout']}")
        if result["stderr"]:
            append_log(log_file, f"POWER stderr: {result['stderr']}")
        if not result["ok"]:
            result["error"] = f"wrapper retornou código {proc.returncode}"
            append_log(log_file, f"POWER ERROR: {result['error']}")
    except Exception as exc:
        result["error"] = str(exc)
        append_log(log_file, f"POWER ERROR: {result['error']}")

    return result


def prepare_power_control(
    config: Dict[str, Any],
    status: str,
    recording_start_epoch: int,
    log_file: Path,
) -> Dict[str, Any]:
    """
    Schedule the next Witty Pi startup, but DO NOT request shutdown yet.

    Shutdown is intentionally deferred until every Poracam file/status/log write
    has finished. The actual GPIO-4 shutdown trigger is performed later by
    execute_terminal_shutdown(), which is the terminal action of the process.

    For normal successful cycles, the next startup is based on
    recording_start_epoch + cycle_period_s, not on end-of-processing time.
    For external-storage recovery boots, the target is based on now + the
    dedicated recovery retry delay because no recording occurred.
    """
    enabled = bool(config.get("power_control_enabled", False))
    dry_run = bool(config.get("power_dry_run", False))
    shutdown_after_recording = bool(config.get("shutdown_after_recording", True))
    cycle_period_s = int(config["cycle_period_s"])
    minimum_off_time_s = int(config.get("minimum_off_time_s", MINIMUM_OFF_TIME_S))
    shutdown_delay_s = int(config.get("shutdown_delay_s", SHUTDOWN_DELAY_S))
    require_schedule = bool(config.get("require_startup_schedule_before_shutdown", True))

    now_epoch = int(time.time())
    recovery_pending = bool(config.get("_poracam_storage_recovery_pending", False))
    recovery_retry_number = int(config.get("_poracam_storage_recovery_retry_number", 0) or 0)
    recovery_delay_s = int(config.get("_poracam_storage_recovery_delay_s", EXTERNAL_STORAGE_RECOVERY_RETRY_DELAY_S))

    if recovery_pending:
        # Missing-storage recovery is based on now, because no recording took place in this boot.
        # Ensure the target still leaves the configured minimum off-time after shutdown.
        target_epoch = max(now_epoch + recovery_delay_s, now_epoch + minimum_off_time_s)
    else:
        target_epoch = compute_next_cycle_start_epoch(
            start_epoch=recording_start_epoch,
            cycle_period_s=cycle_period_s,
            minimum_off_time_s=minimum_off_time_s,
            now_epoch=now_epoch,
        )

    wittypi_dir = find_wittypi_dir(config)

    power: Dict[str, Any] = {
        "enabled": enabled,
        "dry_run": dry_run,
        "shutdown_after_recording": shutdown_after_recording,
        "status_when_called": status,
        "recording_start_epoch": int(recording_start_epoch),
        "recording_start_time": epoch_to_local_iso(recording_start_epoch),
        "now_epoch": now_epoch,
        "now_time": epoch_to_local_iso(now_epoch),
        "cycle_period_s": cycle_period_s,
        "minimum_off_time_s": minimum_off_time_s,
        "shutdown_delay_s": shutdown_delay_s,
        "next_startup_epoch": target_epoch,
        "next_startup_time": epoch_to_local_iso(target_epoch),
        "wittypi_dir": str(wittypi_dir) if wittypi_dir else None,
        "schedule_result": None,
        "shutdown_result": None,
        "shutdown_requested": False,
        "shutdown_pending": False,
        "shutdown_request_mode": "terminal_exec",
        "shutdown_skipped_reason": None,
        "external_storage_recovery_pending": recovery_pending,
        "external_storage_recovery_retry_number": recovery_retry_number if recovery_pending else None,
        "external_storage_recovery_max_retries": EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES,
        "external_storage_recovery_delay_s": recovery_delay_s if recovery_pending else None,
    }

    if not enabled:
        power["shutdown_skipped_reason"] = "power_control_disabled"
        append_log(log_file, "POWER: disabled; not scheduling startup or shutdown.")
        return power

    stop_scheduling = bool(config.get("_poracam_stop_scheduling", False))
    stop_scheduling_reason = str(config.get("_poracam_stop_scheduling_reason", "")) if stop_scheduling else None
    power["stop_scheduling"] = stop_scheduling
    power["stop_scheduling_reason"] = stop_scheduling_reason

    if stop_scheduling:
        power["schedule_result"] = {
            "ok": True,
            "skipped": True,
            "reason": stop_scheduling_reason,
            "message": "Next startup intentionally not scheduled.",
        }
        power["next_startup_epoch"] = None
        power["next_startup_time"] = None
        append_log(log_file, f"POWER: startup scheduling skipped intentionally: {stop_scheduling_reason}")
    else:
        schedule_result = run_power_wrapper(
            action="schedule-startup",
            target_epoch=target_epoch,
            wittypi_dir=wittypi_dir,
            dry_run=dry_run,
            log_file=log_file,
        )
        power["schedule_result"] = schedule_result
        if recovery_pending and schedule_result.get("ok"):
            recovery_warnings: List[str] = []
            recovery_state = mark_external_storage_recovery_retry_scheduled(recovery_warnings)
            power["external_storage_recovery_state"] = recovery_state
            append_log(
                log_file,
                f"POWER: external-storage recovery retry {recovery_retry_number}/"
                f"{EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES} scheduled for {power['next_startup_time']}.",
            )
            for recovery_warning in recovery_warnings:
                append_log(log_file, f"POWER WARNING: {recovery_warning}")

    if not shutdown_after_recording:
        power["shutdown_skipped_reason"] = "shutdown_after_recording_false"
        return power

    if (not stop_scheduling) and require_schedule and not power["schedule_result"].get("ok"):
        power["shutdown_skipped_reason"] = "startup_schedule_failed"
        append_log(log_file, "POWER: startup schedule failed; shutdown skipped to avoid losing recovery.")
        return power

    # Important: only mark shutdown as pending here. No GPIO is touched yet.
    # The trigger must happen after metadata/status/light-log/final sync.
    power["shutdown_pending"] = True
    append_log(
        log_file,
        "POWER: startup scheduling complete; shutdown deferred until all Poracam writes are finalized.",
    )
    return power


def execute_terminal_shutdown(
    config: Dict[str, Any],
    power: Optional[Dict[str, Any]],
    log_file: Path,
) -> bool:
    """
    Execute shutdown as the terminal Poracam action.

    For a real shutdown this function deliberately uses os.execv() to replace
    the Python process with poracam_wittypi_power.sh. Therefore, after the
    Witty Pi GPIO-4 trigger is issued successfully, no Python finally block,
    metadata write, status write, LED update, log append or sync can run.

    Returns only when no real shutdown is requested, in dry-run mode, or if the
    terminal exec fails before the wrapper can be started.
    """
    if not power or not bool(power.get("shutdown_pending", False)):
        return False

    dry_run = bool(power.get("dry_run", False))
    shutdown_delay_s = int(power.get("shutdown_delay_s", config.get("shutdown_delay_s", SHUTDOWN_DELAY_S)))

    wittypi_dir_raw = power.get("wittypi_dir")
    wittypi_dir = Path(str(wittypi_dir_raw)) if wittypi_dir_raw else find_wittypi_dir(config)

    if dry_run:
        append_log(log_file, "POWER: terminal shutdown dry-run; GPIO-4 will not be triggered.")
        result = run_power_wrapper(
            action="shutdown",
            target_epoch=None,
            wittypi_dir=wittypi_dir,
            dry_run=True,
            log_file=log_file,
        )
        power["shutdown_result"] = result
        power["shutdown_requested"] = bool(result.get("ok"))
        if not power["shutdown_requested"]:
            power["shutdown_skipped_reason"] = "shutdown_wrapper_failed"
        return power["shutdown_requested"]

    script = find_power_script()
    if script is None:
        power["shutdown_skipped_reason"] = "shutdown_wrapper_missing"
        append_log(log_file, f"POWER ERROR: {WITTYPI_POWER_SCRIPT_NAME} not found; terminal shutdown skipped.")
        return False

    if wittypi_dir is None:
        power["shutdown_skipped_reason"] = "wittypi_dir_missing"
        append_log(log_file, "POWER ERROR: Witty Pi directory not found; terminal shutdown skipped.")
        return False

    # Everything that needs persistence must be written BEFORE this point.
    append_log(
        log_file,
        "POWER: all Poracam writes finalized; performing final filesystem sync before terminal shutdown.",
    )
    if shutdown_delay_s > 0:
        append_log(
            log_file,
            f"POWER: terminal shutdown armed; GPIO-4 will be triggered after {shutdown_delay_s}s with no further Poracam writes.",
        )

    # Final filesystem flush. Do not log anything after a successful os.sync().
    try:
        os.sync()
    except AttributeError:
        # Python/Linux normally provides os.sync(). Fallback is intentionally
        # silent because writing a post-sync log would dirty the filesystem again.
        subprocess.run(["sync"], check=False)
    except Exception:
        # Same principle: keep the shutdown path terminal and avoid new disk I/O.
        subprocess.run(["sync"], check=False)

    if shutdown_delay_s > 0:
        time.sleep(shutdown_delay_s)

    argv = [str(script), "shutdown", "--wittypi-dir", str(wittypi_dir)]

    try:
        # Successful exec never returns. The wrapper becomes the current process.
        os.execv(str(script), argv)
    except Exception as exc:
        # Safe to log here: exec failed, so GPIO-4 was not triggered by this call.
        power["shutdown_skipped_reason"] = "terminal_exec_failed"
        power["shutdown_result"] = {
            "ok": False,
            "error": str(exc),
            "script": str(script),
            "wittypi_dir": str(wittypi_dir),
        }
        append_log(log_file, f"POWER ERROR: terminal shutdown exec failed before GPIO-4 trigger: {exc}")
        return False

def record(config: Dict[str, Any], config_source: Optional[str], config_source_type: str, external_storage_used: bool, config_warnings: List[str]) -> int:
    validate_config(config)
    total_t0 = time.monotonic()
    start_iso = iso_now()
    start_epoch = int(time.time())
    stamp_base = now_stamp()
    session_name = str(config.get("session_name", DEFAULT_SESSION_NAME))
    stamp = f"{stamp_base}_{session_name}" if session_name else stamp_base
    duration = int(config["duration"])
    segment_duration = int(config["segment_duration_s"])
    enable_segment_split = bool(config["enable_segment_split"])
    segment_plan = build_segment_plan(duration, segment_duration, enable_segment_split)
    segment_count = len(segment_plan)

    media_dir = Path(str(config["media_dir"])).expanduser().resolve()
    poracam_root = media_dir.parent
    status_dir = poracam_root / "status"

    video_dir = media_dir / "video"
    audio_dir = media_dir / "audio"
    temp_dir = media_dir / "temp"
    log_dir = media_dir / "logs"
    metadata_dir = media_dir / "metadata"
    light_log_dir = poracam_root / "logs"
    for directory in [video_dir, audio_dir, temp_dir, log_dir, metadata_dir, status_dir, light_log_dir]:
        ensure_dir(directory)

    log_file = log_dir / f"{stamp}_poracam.log"
    light_log_file = light_log_dir / f"poracam_{dt.datetime.now().strftime('%Y%m%d')}.log"
    metadata_file = metadata_dir / f"{stamp}_metadata.json"

    status = "unknown"
    error_message: Optional[str] = None
    stop_scheduling_reason: Optional[str] = None
    storage_before: Optional[Dict[str, Any]] = None
    storage_after: Optional[Dict[str, Any]] = None
    camera_conflicts: Optional[Dict[str, Any]] = None
    segments: List[Dict[str, Any]] = []
    audio_resolution: Dict[str, Any] = {"requested": AUDIO_DEVICE, "selected": None, "method": "unresolved"}
    field_check_requested = bool(config.get("_poracam_field_check_requested", False)) and bool(FIELD_CHECK_ENABLED)
    field_check_ok = False
    field_check_failure = False

    timing: Dict[str, Optional[float]] = {
        "storage_check_s": None,
        "camera_conflict_check_s": None,
        "segments_recording_s": None,
        "segments_mp4_processing_s": None,
        "segments_sync_s": None,
        "total_s": None,
    }

    first_temp_h264, first_video_mp4, first_audio_wav, _ = segment_file_paths(
        stamp, segment_count, 1, video_dir, audio_dir, temp_dir
    )

    metadata: Dict[str, Any] = {
        "project": PROJECT_NAME,
        "version": PROJECT_VERSION,
        "status": status,
        "start_time": start_iso,
        "end_time": None,
        "duration_requested_s": duration,
        "duration_actual_s": None,
        "config_source": config_source,
        "config_source_type": config_source_type,
        "external_storage_used": external_storage_used,
        "external_mount_root": None,
        "config_warnings": config_warnings,
        "media_dir": str(media_dir),
        "status_dir": str(status_dir),
        "paths": {
            "video_mp4": str(first_video_mp4),
            "audio_wav": str(first_audio_wav),
            "temp_h264": str(first_temp_h264),
            "log": str(log_file),
            "light_log": str(light_log_file),
            "metadata": str(metadata_file),
            "ready_status": str(status_dir / READY_STATUS_FILE_NAME),
        },
        "settings": {
            "session_name": str(config.get("session_name", DEFAULT_SESSION_NAME)),
            "record_duration_min": float(config.get("record_duration_min", duration / 60.0)),
            "cycle_period_min": float(config.get("cycle_period_min", int(config["cycle_period_s"]) / 60.0)),
            "video_quality": str(config.get("video_quality", "high")),
            "run_mode": str(config["run_mode"]),
            "duration": duration,
            "segment_duration_s": segment_duration,
            "enable_segment_split": enable_segment_split,
            "segment_count": segment_count,
            "segment_plan_s": segment_plan,
            "cycle_period_s": int(config["cycle_period_s"]),
            "width": int(config["width"]),
            "height": int(config["height"]),
            "fps": int(config["fps"]),
            "bitrate": int(config["bitrate"]),
            "audio_device": str(config["audio_device"]),
            "audio_format": str(config["audio_format"]),
            "audio_buffer_time_us": int(config.get("audio_buffer_time_us", AUDIO_BUFFER_TIME_US)),
            "metadata_enabled": bool(config.get("metadata_enabled", METADATA_ENABLED)),
            "keep_h264": bool(config["keep_h264"]),
            "delete_h264_after_mp4": not bool(config["keep_h264"]),
            "start_delay_s": float(config["start_delay"]),
            "extra_timeout_s": float(config["extra_timeout"]),
            "max_storage_percent": float(config["max_storage_percent"]),
            "min_free_mb_before_recording": MIN_FREE_MB_BEFORE_RECORDING,
            "stop_scheduling_when_storage_full": STOP_SCHEDULING_WHEN_STORAGE_FULL,
            "require_external_storage_in_power_control": REQUIRE_EXTERNAL_STORAGE_IN_POWER_CONTROL,
            "prefer_external_storage": bool(config["prefer_external_storage"]),
            "allow_local_fallback": bool(config["allow_local_fallback"]),
            "external_config_wait_timeout_s": EXTERNAL_CONFIG_WAIT_TIMEOUT_S,
            "external_config_retry_interval_s": EXTERNAL_CONFIG_RETRY_INTERVAL_S,
            "external_config_try_manual_mount": EXTERNAL_CONFIG_TRY_MANUAL_MOUNT,
            "external_storage_recovery_enabled": EXTERNAL_STORAGE_RECOVERY_ENABLED,
            "external_storage_recovery_max_retries": EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES,
            "external_storage_recovery_retry_delay_s": EXTERNAL_STORAGE_RECOVERY_RETRY_DELAY_S,
            "external_storage_recovery_state_file": EXTERNAL_STORAGE_RECOVERY_STATE_FILE,
            "external_storage_verify_before_recording": EXTERNAL_STORAGE_VERIFY_BEFORE_RECORDING,
            "external_storage_verify_before_metadata": EXTERNAL_STORAGE_VERIFY_BEFORE_METADATA,
            "power_control_enabled": bool(config.get("power_control_enabled", False)),
            "power_dry_run": bool(config.get("power_dry_run", False)),
            "minimum_off_time_s": int(config.get("minimum_off_time_s", MINIMUM_OFF_TIME_S)),
            "shutdown_delay_s": int(config.get("shutdown_delay_s", SHUTDOWN_DELAY_S)),
            "camera_conflict_policy": str(config["camera_conflict_policy"]),
            "camera_startup_check_s": float(config["camera_startup_check_s"]),
            "camera_stop_timeout_s": float(config["camera_stop_timeout_s"]),
        },
        "audio": audio_resolution,
        "camera": {"conflicts": None, "audio_started": False},
        "storage": {"before": None, "after": None},
        "external_storage_recovery": config.get("_poracam_storage_recovery_state"),
        "segments": [],
        "timing": timing,
        "files": {},
        "h264_deleted": None,
        "power": None,
        "error": None,
    }

    try:
        if field_check_requested:
            led_checking()
        else:
            led_solid_on()

        if EXTERNAL_STORAGE_VERIFY_BEFORE_RECORDING and external_storage_used:
            ok_ext, mount_root = verify_external_storage_alive(
                external_storage_used=external_storage_used,
                config_source=config_source,
                warnings=config_warnings,
                context="pré-gravação",
            )
            metadata["external_mount_root"] = str(mount_root) if mount_root else None
            if not ok_ext:
                raise RuntimeError(
                    "Armazenamento externo foi selecionado, mas não está montado/estável antes da gravação. "
                    "Abortando para evitar gravar em diretório local disfarçado de /media."
                )

        append_log(log_file, f"Poracam v{PROJECT_VERSION} recording started")
        append_log(log_file, f"Config source: {config_source or 'internal defaults / CLI only'}")
        append_log(log_file, f"Config source type: {config_source_type}")
        append_log(log_file, f"External storage used: {external_storage_used}")
        append_light_log(
            light_log_file,
            "START",
            version=PROJECT_VERSION,
            session=session_name,
            config_source_type=config_source_type,
            external_storage=external_storage_used,
            media_dir=media_dir,
        )
        for warning in config_warnings:
            append_log(log_file, f"CONFIG WARNING: {warning}")

        if config.get("_poracam_blocking_startup_error"):
            field_check_failure = True
            config["_poracam_stop_scheduling"] = True
            config["_poracam_stop_scheduling_reason"] = "field_check_blocking_error"
            config["shutdown_after_recording"] = False
            checks = build_basic_ready_checks(config_source_type, external_storage_used, None, None)
            checks.append(("Ajuste de data/hora por SET_TIME.txt", False, str(config.get("_poracam_blocking_startup_error"))))
            write_ready_status_file(
                status_dir,
                False,
                PROJECT_VERSION,
                checks,
                "Nao fechar o case. Corrigir SET_TIME.txt/configuracao e reiniciar o equipamento.",
                str(config.get("_poracam_blocking_startup_error")),
            )
            raise FieldCheckError(str(config.get("_poracam_blocking_startup_error")))

        if bool(config.get("_poracam_external_required_missing", False)):
            raise ExternalStorageRequiredError(
                "Armazenamento externo PORACAM não encontrado em modo power-control. "
                "Fallback local bloqueado; gravação não iniciada."
            )

        try:
            audio_resolution = resolve_audio_device(config, log_file)
        except Exception as audio_exc:
            audio_resolution = {
                "requested": str(config.get("audio_device")),
                "selected": None,
                "method": "failed",
                "error": str(audio_exc),
            }
            metadata["audio"] = audio_resolution
            if field_check_requested:
                raise FieldCheckError(str(audio_exc))
            raise
        metadata["audio"] = audio_resolution
        metadata["settings"]["audio_device"] = str(config["audio_device"])
        metadata["settings"]["audio_transport"] = audio_resolution.get("transport")
        append_log(log_file, f"Audio device requested: {audio_resolution.get('requested')}")
        append_log(log_file, f"Audio device selected: {audio_resolution.get('selected')}")

        storage_t0 = time.monotonic()
        storage_before = check_storage_or_fail(media_dir, float(config["max_storage_percent"]))
        timing["storage_check_s"] = round(time.monotonic() - storage_t0, 3)
        append_log(log_file, f"Storage before recording: {storage_before['used_percent']}% used, {storage_before['free_mb']} MB free")
        if storage_before.get("trash_warning"):
            append_log(log_file, f"STORAGE WARNING: {storage_before.get('trash_warning')}")
            append_light_log(light_log_file, "STORAGE_WARNING", warning=storage_before.get("trash_warning"))

        camera_t0 = time.monotonic()
        camera_conflicts = handle_camera_conflicts(config, log_file)
        timing["camera_conflict_check_s"] = round(time.monotonic() - camera_t0, 3)

        raspivid = which_or_fail("raspivid")
        arecord = which_or_fail("arecord")
        ffmpeg = which_or_fail("ffmpeg")

        append_log(log_file, f"Media dir: {media_dir}")
        append_log(log_file, f"Status dir: {status_dir}")
        append_log(log_file, f"Run mode: {config['run_mode']}")
        append_log(log_file, f"Duration requested: {duration} s")
        append_log(log_file, f"Segment split enabled: {enable_segment_split}")
        append_log(log_file, f"Segment duration: {segment_duration} s")
        append_log(log_file, f"Segment plan: {segment_plan}")
        append_log(log_file, f"Cycle period configured: {config['cycle_period_s']} s")
        append_log(log_file, f"Camera conflict policy: {config['camera_conflict_policy']}")

        if field_check_requested:
            append_log(log_file, f"FIELD CHECK: starting {FIELD_CHECK_DURATION_S}s check recording")
            append_light_log(light_log_file, "FIELD_CHECK_START", duration_s=FIELD_CHECK_DURATION_S)

            checks = build_basic_ready_checks(config_source_type, external_storage_used, storage_before, audio_resolution)
            checks.append(("Ajuste de data/hora por SET_TIME.txt", bool(config.get("_poracam_time_set_ok", False)), str(config.get("_poracam_time_set_requested_datetime", ""))))

            try:
                check_segment = record_one_segment(
                    config=config,
                    segment_index=1,
                    segment_count=1,
                    segment_duration=FIELD_CHECK_DURATION_S,
                    stamp=f"{stamp}_check",
                    video_dir=video_dir,
                    audio_dir=audio_dir,
                    temp_dir=temp_dir,
                    log_file=log_file,
                    raspivid=raspivid,
                    arecord=arecord,
                    ffmpeg=ffmpeg,
                )
                segments.append(check_segment)
                check_video = Path(str(check_segment.get("paths", {}).get("video_mp4", "")))
                check_audio = Path(str(check_segment.get("paths", {}).get("audio_wav", "")))
                checks.append(("Video MP4 de teste", check_segment.get("status") == "ok" and check_video.exists() and check_video.stat().st_size > 0, str(check_video)))
                checks.append(("Audio WAV de teste", check_segment.get("status") == "ok" and check_audio.exists() and check_audio.stat().st_size > 0, str(check_audio)))
                checks.append(("Arquivo temporario limpo", bool(check_segment.get("h264_deleted")), str(check_segment.get("paths", {}).get("temp_h264"))))
                if check_segment.get("status") != "ok":
                    raise FieldCheckError(f"Gravacao curta de teste falhou: {check_segment.get('error')}")

                if not all(ok for _label, ok, _detail in checks):
                    failed = [label for label, ok, _detail in checks if not ok]
                    raise FieldCheckError("Checklist inicial falhou: " + ", ".join(failed))

                field_check_ok = True
                consume_or_mark_time_file_after_check(config, True, config_warnings)
                write_ready_status_file(
                    status_dir,
                    True,
                    PROJECT_VERSION,
                    checks,
                    "Sistema pronto. Pode fechar o case; a rotina normal sera iniciada.",
                    None,
                )
                append_log(log_file, "FIELD CHECK: ok; SET_TIME consumed; continuing normal recording")
                append_light_log(light_log_file, "FIELD_CHECK_OK", ready=True)
                led_solid_on()

            except Exception as check_exc:
                field_check_failure = True
                error_text = str(check_exc)
                consume_or_mark_time_file_after_check(config, False, config_warnings)
                write_ready_status_file(
                    status_dir,
                    False,
                    PROJECT_VERSION,
                    checks if 'checks' in locals() else [],
                    "Nao fechar o case. Corrigir o problema indicado e reiniciar o equipamento.",
                    error_text,
                )
                config["_poracam_stop_scheduling"] = True
                config["_poracam_stop_scheduling_reason"] = "field_check_failed"
                config["shutdown_after_recording"] = False
                raise FieldCheckError(error_text)

            append_log(log_file, "FIELD CHECK: normal recording will start now")

        for idx, seg_duration in enumerate(segment_plan, start=1):
            storage_now = check_storage_or_fail(media_dir, float(config["max_storage_percent"]))
            append_log(log_file, f"Storage before segment {idx}/{segment_count}: {storage_now['used_percent']}% used, {storage_now['free_mb']} MB free")

            segment = record_one_segment(
                config=config,
                segment_index=idx,
                segment_count=segment_count,
                segment_duration=seg_duration,
                stamp=stamp,
                video_dir=video_dir,
                audio_dir=audio_dir,
                temp_dir=temp_dir,
                log_file=log_file,
                raspivid=raspivid,
                arecord=arecord,
                ffmpeg=ffmpeg,
            )
            segments.append(segment)

            if segment["status"] != "ok":
                raise RuntimeError(f"Segmento {idx}/{segment_count} falhou: {segment.get('error')}")

        storage_after = get_storage_info(media_dir)
        append_log(log_file, f"Storage after recording: {storage_after['used_percent']}% used, {storage_after['free_mb']} MB free")

        status = "ok"

    except FieldCheckError as exc:
        status = "error"
        error_message = str(exc)
        field_check_failure = True
        stop_scheduling_reason = "field_check_failed"
        config["_poracam_stop_scheduling"] = True
        config["_poracam_stop_scheduling_reason"] = stop_scheduling_reason
        config["shutdown_after_recording"] = False
        consume_or_mark_time_file_after_check(config, False, config_warnings)
        try:
            storage_after = get_storage_info(media_dir)
        except Exception:
            storage_after = storage_before
        try:
            write_field_check_failure_status(
                status_dir=status_dir,
                config=config,
                config_source_type=config_source_type,
                external_storage_used=external_storage_used,
                storage_info=storage_after or storage_before,
                audio_resolution=audio_resolution,
                error_message=error_message,
            )
        except Exception as ready_exc:
            append_log(log_file, f"WARNING: failed to write ready failure status: {ready_exc}")
        append_log(log_file, f"FIELD CHECK ERROR: {error_message}")
        append_log(log_file, "POWER: field check failed; next startup will not be scheduled and shutdown will be skipped for LED indication.")

    except StorageFullError as exc:
        status = "error"
        error_message = str(exc)
        if STOP_SCHEDULING_WHEN_STORAGE_FULL:
            stop_scheduling_reason = "storage_full"
            config["_poracam_stop_scheduling"] = True
            config["_poracam_stop_scheduling_reason"] = stop_scheduling_reason
        append_log(log_file, f"ERROR: {error_message}")
        append_log(log_file, "POWER: storage full policy active; next startup will not be scheduled.")
        try:
            storage_after = get_storage_info(media_dir)
        except Exception:
            storage_after = None

    except ExternalStorageRequiredError as exc:
        status = "error"
        error_message = str(exc)
        recovery_pending = bool(config.get("_poracam_storage_recovery_pending", False))
        if recovery_pending:
            stop_scheduling_reason = None
            config["_poracam_stop_scheduling"] = False
            config["_poracam_stop_scheduling_reason"] = ""
        else:
            stop_scheduling_reason = str(
                config.get("_poracam_stop_scheduling_reason", "external_storage_missing")
                or "external_storage_missing"
            )
            config["_poracam_stop_scheduling"] = True
            config["_poracam_stop_scheduling_reason"] = stop_scheduling_reason

        if field_check_requested:
            field_check_failure = True
            config["shutdown_after_recording"] = False
            consume_or_mark_time_file_after_check(config, False, config_warnings)
            try:
                write_field_check_failure_status(
                    status_dir=status_dir,
                    config=config,
                    config_source_type=config_source_type,
                    external_storage_used=external_storage_used,
                    storage_info=storage_before,
                    audio_resolution=audio_resolution,
                    error_message=error_message,
                )
            except Exception as ready_exc:
                append_log(log_file, f"WARNING: failed to write ready failure status: {ready_exc}")
        append_log(log_file, f"ERROR: {error_message}")
        if recovery_pending:
            append_log(
                log_file,
                "POWER: external storage missing; automatic recovery power-cycle will be scheduled "
                f"(retry {config.get('_poracam_storage_recovery_retry_number')}/"
                f"{EXTERNAL_STORAGE_RECOVERY_MAX_RETRIES}).",
            )
        else:
            append_log(
                log_file,
                f"POWER: external storage missing; recovery stopped ({stop_scheduling_reason}); "
                "next startup will not be scheduled.",
            )

    except Exception as exc:
        status = "error"
        error_message = str(exc)
        if field_check_requested:
            field_check_failure = True
            stop_scheduling_reason = "field_check_failed"
            config["_poracam_stop_scheduling"] = True
            config["_poracam_stop_scheduling_reason"] = stop_scheduling_reason
            config["shutdown_after_recording"] = False
            consume_or_mark_time_file_after_check(config, False, config_warnings)
        append_log(log_file, f"ERROR: {error_message}")
        try:
            storage_after = get_storage_info(media_dir)
        except Exception:
            storage_after = storage_before
        if field_check_requested:
            try:
                write_field_check_failure_status(
                    status_dir=status_dir,
                    config=config,
                    config_source_type=config_source_type,
                    external_storage_used=external_storage_used,
                    storage_info=storage_after or storage_before,
                    audio_resolution=audio_resolution,
                    error_message=error_message,
                )
            except Exception as ready_exc:
                append_log(log_file, f"WARNING: failed to write ready failure status: {ready_exc}")

    finally:
        end_iso = iso_now()
        total_elapsed = time.monotonic() - total_t0
        timing["total_s"] = round(total_elapsed, 3)
        timing["segments_recording_s"] = round(sum((s.get("timing", {}).get("recording_s") or 0.0) for s in segments), 3)
        timing["segments_mp4_processing_s"] = round(sum((s.get("timing", {}).get("video_mp4_processing_s") or 0.0) for s in segments), 3)
        timing["segments_sync_s"] = round(sum((s.get("timing", {}).get("sync_s") or 0.0) for s in segments), 3)

        all_h264_deleted = all(bool(s.get("h264_deleted")) for s in segments) if segments else False
        any_audio_started = any(bool(s.get("audio_started")) for s in segments) if segments else False

        metadata["status"] = status
        metadata["end_time"] = end_iso
        metadata["duration_actual_s"] = round(total_elapsed, 3)
        metadata["timing"] = timing
        metadata["storage"] = {"before": storage_before, "after": storage_after}
        metadata["camera"] = {"conflicts": camera_conflicts, "audio_started": any_audio_started}
        metadata["segments"] = segments
        metadata["h264_deleted"] = all_h264_deleted
        metadata["files"] = {
            "log": file_info(log_file),
            "segments_ok": sum(1 for s in segments if s.get("status") == "ok"),
            "segments_total": segment_count,
        }
        metadata["field_check"] = {
            "requested": field_check_requested,
            "ok": field_check_ok,
            "failure": field_check_failure,
            "duration_s": FIELD_CHECK_DURATION_S if field_check_requested else None,
        }
        metadata["error"] = error_message
        metadata["stop_scheduling_reason"] = stop_scheduling_reason

        if EXTERNAL_STORAGE_VERIFY_BEFORE_METADATA and external_storage_used:
            ok_ext_meta, mount_root_meta = verify_external_storage_alive(
                external_storage_used=external_storage_used,
                config_source=config_source,
                warnings=config_warnings,
                context="pré-metadata",
            )
            metadata["external_mount_root"] = str(mount_root_meta) if mount_root_meta else metadata.get("external_mount_root")
            metadata["config_warnings"] = config_warnings
            if not ok_ext_meta:
                recovery_path = save_recovery_metadata(metadata, "external_storage_unavailable_before_metadata")
                if recovery_path:
                    append_log(log_file, f"RECOVERY metadata saved locally: {recovery_path}")
                raise RuntimeError(
                    "Armazenamento externo desapareceu antes de salvar metadata/status. "
                    f"Recovery local: {recovery_path or 'falhou'}"
                )

        write_metadata_file(metadata_file, metadata, log_file)
        try:
            write_status_files(status_dir, metadata, status, error_message)
        except Exception as status_exc:
            append_log(log_file, f"WARNING: failed to write status files: {status_exc}")

        # Power control phase 1: schedule startup only. GPIO-4 is deliberately
        # NOT touched here because Poracam still needs to persist final state.
        power_info: Dict[str, Any]
        try:
            power_info = prepare_power_control(config, status, start_epoch, log_file)
        except Exception as power_exc:
            append_log(log_file, f"POWER ERROR: unexpected scheduling failure: {power_exc}")
            power_info = {
                "enabled": bool(config.get("power_control_enabled", False)),
                "shutdown_pending": False,
                "shutdown_requested": False,
                "shutdown_skipped_reason": "power_prepare_exception",
                "error": str(power_exc),
            }

        metadata["power"] = power_info

        # Persist power schedule/result BEFORE any real shutdown request.
        write_metadata_file(metadata_file, metadata, log_file)
        try:
            write_status_files(status_dir, metadata, status, error_message)
        except Exception as status_exc:
            append_log(log_file, f"WARNING: failed to rewrite status files after power scheduling: {status_exc}")

        if status == "ok":
            led_solid_on()
        elif field_check_failure:
            led_error_blink()

        # These are the final Poracam disk writes for a real power-controlled cycle.
        append_log(log_file, f"Metadata saved: {metadata_file if METADATA_ENABLED else 'disabled'}")
        append_log(log_file, f"Final status: {status}")
        append_log(log_file, f"Total elapsed: {timing['total_s']} s")
        append_light_log(
            light_log_file,
            "END",
            status=status,
            error=error_message,
            video=metadata.get("paths", {}).get("video_mp4"),
            audio=metadata.get("paths", {}).get("audio_wav"),
            total_s=timing.get("total_s"),
            free_mb=(storage_after or storage_before or {}).get("free_mb") if (storage_after or storage_before) else None,
            next_startup=(metadata.get("power") or {}).get("next_startup_time"),
        )

        if bool(power_info.get("shutdown_pending", False)):
            # TERMINAL ACTION: on success this replaces the Python process and
            # never returns. Nothing in Poracam executes after the GPIO-4 trigger.
            execute_terminal_shutdown(config, power_info, log_file)
        else:
            # No shutdown will occur (disabled, field-check failure, schedule
            # failure, etc.), so a conventional logged sync is safe here.
            try:
                sync_filesystem(log_file)
            except Exception as sync_exc:
                append_log(log_file, f"WARNING: final sync failed: {sync_exc}")
    return 0 if status == "ok" else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poracam v0.8.3.4: USB boot robustness, direct ALSA hardware capture and terminal Witty Pi shutdown."
    )

    parser.add_argument(
        "--config",
        default=None,
        help="Caminho para o config.txt. Se omitido, procura armazenamento externo e fallback local.",
    )
    parser.add_argument(
        "--ignore-external-storage",
        action="store_true",
        help="Ignora busca por /media/*/*/PORACAM/config.txt e /mnt/*/PORACAM/config.txt.",
    )

    # User-facing developer test overrides.
    parser.add_argument("--session-name", default=None, help="Nome da campanha/sessão.")
    parser.add_argument("--record-duration-min", type=float, default=None, help="Tempo de gravação por ciclo, em minutos.")
    parser.add_argument("--cycle-period-min", type=float, default=None, help="Período entre inícios de gravação, em minutos.")
    parser.add_argument("--video-quality", default=None, help="Perfil de vídeo: low, balanced ou high.")

    # Developer/installation flags. Not intended for the end-user config.txt.
    parser.add_argument("--power-control", action="store_true", help="Ativa agendamento Witty Pi + shutdown ao final da gravação.")
    parser.add_argument("--no-power-control", action="store_true", help="Desativa controle de energia, mesmo na v0.8.3.4.")
    parser.add_argument("--power-dry-run", action="store_true", help="Simula agendamento/shutdown sem escrever no Witty Pi nem desligar.")
    parser.add_argument("--wittypi-dir", default=None, help="Diretório do Witty Pi contendo utilities.sh.")

    parser.add_argument("--version", action="version", version=f"Poracam {PROJECT_VERSION}")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        config, config_source, source_type, external_used, warnings = merge_config(args)
        warnings = handle_time_set_command(config, config_source, source_type, warnings)
        return record(config, config_source, source_type, external_used, warnings)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())