#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poracam_record.py — Poracam v0.6.1

Novidades da v0.6:
- Procura armazenamento externo com PORACAM/config.txt em /media/*/* e /mnt/*.
- Se encontrar config externo, usa esse config e salva em PORACAM/media quando media_dir não estiver definido.
- Mantém fallback local.
- Cria arquivos de status em PORACAM/status ou no diretório pai de media/.
- Mantém lógica validada: vídeo MP4 final, áudio WAV separado, H264 temporário apagado.
"""

import argparse
import datetime as dt
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_NAME = "poracam"
PROJECT_VERSION = "0.6.1"

DEFAULT_CONFIG: Dict[str, Any] = {
    "duration": 60,
    "segment_duration_s": 600,
    "enable_segment_split": True,
    "cycle_period_s": 300,
    "run_mode": "single",
    "media_dir": "/home/fishcam/poracam/media",
    "width": 1280,
    "height": 720,
    "fps": 30,
    "bitrate": 2500000,
    "audio_device": "default",
    "audio_format": "cd",
    "keep_h264": False,
    "start_delay": 0.2,
    "extra_timeout": 15.0,
    "max_storage_percent": 95.0,
    "prefer_external_storage": True,
    "allow_local_fallback": True,
    "camera_conflict_policy": "stop_known_processes",
    "camera_startup_check_s": 1.0,
    "camera_stop_timeout_s": 5.0,
}

CONFIG_KEY_ALIASES = {
    "record_duration_s": "duration", "duration_s": "duration", "duration": "duration",
    "segment_duration_s": "segment_duration_s", "max_segment_duration_s": "segment_duration_s", "split_duration_s": "segment_duration_s",
    "enable_segment_split": "enable_segment_split", "segment_split_enabled": "enable_segment_split",
    "cycle_period_s": "cycle_period_s", "cycle_s": "cycle_period_s", "period_s": "cycle_period_s",
    "run_mode": "run_mode", "mode": "run_mode",
    "media_dir": "media_dir", "output_dir": "media_dir",
    "width": "width", "height": "height", "fps": "fps", "bitrate": "bitrate",
    "audio_device": "audio_device", "audio_format": "audio_format",
    "keep_h264": "keep_h264", "delete_h264_after_mp4": "delete_h264_after_mp4",
    "start_delay": "start_delay", "start_delay_s": "start_delay",
    "extra_timeout": "extra_timeout", "extra_timeout_s": "extra_timeout",
    "max_storage_percent": "max_storage_percent", "storage_max_percent": "max_storage_percent",
    "prefer_external_storage": "prefer_external_storage",
    "allow_local_fallback": "allow_local_fallback",
    "camera_conflict_policy": "camera_conflict_policy",
    "camera_startup_check_s": "camera_startup_check_s",
    "camera_stop_timeout_s": "camera_stop_timeout_s",
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
    if key in ("duration", "segment_duration_s", "cycle_period_s", "width", "height", "fps", "bitrate"):
        return int(value)
    if key in ("start_delay", "extra_timeout", "max_storage_percent", "camera_startup_check_s", "camera_stop_timeout_s"):
        return float(value)
    if key in ("keep_h264", "delete_h264_after_mp4", "prefer_external_storage", "allow_local_fallback"):
        return parse_bool(value)
    if key in ("run_mode", "camera_conflict_policy"):
        return value.strip().lower()
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
        external = find_external_config_path()
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


def merge_config(args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[str], str, bool, List[str]]:
    final_config = dict(DEFAULT_CONFIG)
    config_path, source_type, external_used, warnings = determine_config_path(args)
    file_config, config_source, file_warnings, media_dir_was_defined = load_config_file(config_path)
    warnings.extend(file_warnings)
    final_config.update(file_config)
    if external_used and config_path is not None and not media_dir_was_defined:
        poracam_root = config_path.parent
        final_config["media_dir"] = str(poracam_root / "media")
        warnings.append(f"media_dir não definido no config externo; usando automaticamente {final_config['media_dir']}")
    cli_overrides = {
        "duration": args.duration,
        "segment_duration_s": args.segment_duration_s,
        "enable_segment_split": args.enable_segment_split,
        "cycle_period_s": args.cycle_period_s,
        "run_mode": args.run_mode,
        "media_dir": args.media_dir,
        "width": args.width,
        "height": args.height,
        "fps": args.fps,
        "bitrate": args.bitrate,
        "audio_device": args.audio_device,
        "audio_format": args.audio_format,
        "keep_h264": args.keep_h264,
        "start_delay": args.start_delay,
        "extra_timeout": args.extra_timeout,
        "max_storage_percent": args.max_storage_percent,
        "prefer_external_storage": False if args.ignore_external_storage else None,
        "allow_local_fallback": args.allow_local_fallback,
        "camera_conflict_policy": args.camera_conflict_policy,
        "camera_startup_check_s": args.camera_startup_check_s,
        "camera_stop_timeout_s": args.camera_stop_timeout_s,
    }
    for key, value in cli_overrides.items():
        if value is not None:
            final_config[key] = str(value).lower() if key == "run_mode" else value
    return final_config, config_source, source_type, external_used, warnings


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


def check_storage_or_fail(path: Path, max_storage_percent: float) -> Dict[str, Any]:
    info = get_storage_info(path)
    if info["used_percent"] >= max_storage_percent:
        raise RuntimeError(f"Uso do armazenamento acima do limite: {info['used_percent']}% usado, limite={max_storage_percent}%")
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
        raise ValueError(f"Na v0.6.1, apenas run_mode=single é suportado. Recebido: run_mode={config['run_mode']}")
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
    summary = {
        "project": metadata.get("project"),
        "version": metadata.get("version"),
        "last_status": status,
        "last_start_time": metadata.get("start_time"),
        "last_end_time": metadata.get("end_time"),
        "video_mp4": metadata.get("paths", {}).get("video_mp4"),
        "audio_wav": metadata.get("paths", {}).get("audio_wav"),
        "metadata": metadata.get("paths", {}).get("metadata"),
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
        f"Metadata: {metadata.get('paths', {}).get('metadata')}",
    ]
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


def record(config: Dict[str, Any], config_source: Optional[str], config_source_type: str, external_storage_used: bool, config_warnings: List[str]) -> int:
    validate_config(config)
    total_t0 = time.monotonic()
    start_iso = iso_now()
    stamp = now_stamp()
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
    for directory in [video_dir, audio_dir, temp_dir, log_dir, metadata_dir, status_dir]:
        ensure_dir(directory)

    log_file = log_dir / f"{stamp}_poracam.log"
    metadata_file = metadata_dir / f"{stamp}_metadata.json"

    status = "unknown"
    error_message: Optional[str] = None
    storage_before: Optional[Dict[str, Any]] = None
    storage_after: Optional[Dict[str, Any]] = None
    camera_conflicts: Optional[Dict[str, Any]] = None
    segments: List[Dict[str, Any]] = []

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
        "config_warnings": config_warnings,
        "media_dir": str(media_dir),
        "status_dir": str(status_dir),
        "paths": {
            "video_mp4": str(first_video_mp4),
            "audio_wav": str(first_audio_wav),
            "temp_h264": str(first_temp_h264),
            "log": str(log_file),
            "metadata": str(metadata_file),
        },
        "settings": {
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
            "keep_h264": bool(config["keep_h264"]),
            "delete_h264_after_mp4": not bool(config["keep_h264"]),
            "start_delay_s": float(config["start_delay"]),
            "extra_timeout_s": float(config["extra_timeout"]),
            "max_storage_percent": float(config["max_storage_percent"]),
            "prefer_external_storage": bool(config["prefer_external_storage"]),
            "allow_local_fallback": bool(config["allow_local_fallback"]),
            "camera_conflict_policy": str(config["camera_conflict_policy"]),
            "camera_startup_check_s": float(config["camera_startup_check_s"]),
            "camera_stop_timeout_s": float(config["camera_stop_timeout_s"]),
        },
        "camera": {"conflicts": None, "audio_started": False},
        "storage": {"before": None, "after": None},
        "segments": [],
        "timing": timing,
        "files": {},
        "h264_deleted": None,
        "error": None,
    }

    try:
        append_log(log_file, f"Poracam v{PROJECT_VERSION} recording started")
        append_log(log_file, f"Config source: {config_source or 'internal defaults / CLI only'}")
        append_log(log_file, f"Config source type: {config_source_type}")
        append_log(log_file, f"External storage used: {external_storage_used}")
        for warning in config_warnings:
            append_log(log_file, f"CONFIG WARNING: {warning}")

        storage_t0 = time.monotonic()
        storage_before = check_storage_or_fail(media_dir, float(config["max_storage_percent"]))
        timing["storage_check_s"] = round(time.monotonic() - storage_t0, 3)
        append_log(log_file, f"Storage before recording: {storage_before['used_percent']}% used, {storage_before['free_mb']} MB free")

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

    except Exception as exc:
        status = "error"
        error_message = str(exc)
        append_log(log_file, f"ERROR: {error_message}")
        try:
            storage_after = get_storage_info(media_dir)
        except Exception:
            storage_after = None

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
        metadata["error"] = error_message

        with metadata_file.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        try:
            write_status_files(status_dir, metadata, status, error_message)
        except Exception as status_exc:
            append_log(log_file, f"WARNING: failed to write status files: {status_exc}")
        append_log(log_file, f"Metadata saved: {metadata_file}")
        append_log(log_file, f"Final status: {status}")
        append_log(log_file, f"Total elapsed: {timing['total_s']} s")
    return 0 if status == "ok" else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poracam v0.6.1: gravação MP4/WAV com proteção contra câmera ocupada.")
    parser.add_argument("--config", default=None, help="Caminho para o config.txt. Se omitido, procura armazenamento externo e fallback local.")
    parser.add_argument("--ignore-external-storage", action="store_true", help="Ignora busca por /media/*/*/PORACAM/config.txt e /mnt/*/PORACAM/config.txt.")
    parser.add_argument("--allow-local-fallback", action="store_true", default=None, help="Permite fallback local. Campo reservado para uso futuro.")
    parser.add_argument("--duration", type=int, default=None, help="Duração da gravação em segundos.")
    parser.add_argument("--cycle-period-s", type=int, default=None, help="Período total do ciclo em segundos.")
    parser.add_argument("--segment-duration-s", type=int, default=None, help="Duração máxima de cada segmento, em segundos.")
    parser.add_argument("--enable-segment-split", action="store_true", default=None, help="Habilita divisão da gravação em segmentos.")
    parser.add_argument("--run-mode", default=None, help="Modo de execução. Na v0.5, apenas 'single' é suportado.")
    parser.add_argument("--media-dir", default=None, help="Diretório base para salvar mídia, logs e metadados.")
    parser.add_argument("--width", type=int, default=None, help="Largura do vídeo.")
    parser.add_argument("--height", type=int, default=None, help="Altura do vídeo.")
    parser.add_argument("--fps", type=int, default=None, help="Frames por segundo.")
    parser.add_argument("--bitrate", type=int, default=None, help="Bitrate do vídeo em bits/s.")
    parser.add_argument("--audio-device", default=None, help="Dispositivo ALSA para áudio. Exemplo: default, plughw:1,0.")
    parser.add_argument("--audio-format", default=None, help="Formato do arecord. Exemplo: cd, S16_LE.")
    parser.add_argument("--start-delay", type=float, default=None, help="Atraso entre início do áudio e início do vídeo.")
    parser.add_argument("--extra-timeout", type=float, default=None, help="Tempo extra para aguardar processos finalizarem.")
    parser.add_argument("--max-storage-percent", type=float, default=None, help="Uso máximo permitido do armazenamento, em porcentagem.")
    parser.add_argument("--camera-conflict-policy", default=None, help="error_only, stop_known_processes ou ignore.")
    parser.add_argument("--camera-startup-check-s", type=float, default=None, help="Tempo para verificar se raspivid falha logo no início.")
    parser.add_argument("--camera-stop-timeout-s", type=float, default=None, help="Tempo para tentar parar processos conhecidos da câmera.")
    parser.add_argument("--keep-h264", action="store_true", default=None, help="Mantém o arquivo .h264 temporário após gerar o MP4.")
    parser.add_argument("--version", action="version", version=f"Poracam {PROJECT_VERSION}")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        config, config_source, source_type, external_used, warnings = merge_config(args)
        return record(config, config_source, source_type, external_used, warnings)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())