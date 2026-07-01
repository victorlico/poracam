#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poracam_record.py — Poracam v0.4

Gravador simples para Raspberry Pi Zero W com Raspberry Pi OS Buster.

Características da v0.4:
- Lê configuração de um arquivo chamado config.txt.
- Mantém argumentos de linha de comando como override.
- Adiciona run_mode=single.
- Adiciona cycle_period_s para validação futura de ciclos.
- Adiciona max_storage_percent.
- Verifica uso do armazenamento antes de gravar.
- Registra informações de armazenamento no metadata.
- Grava vídeo com raspivid em .h264 temporário.
- Grava áudio com arecord em .wav.
- Gera vídeo final .mp4 por padrão usando ffmpeg sem recodificar o vídeo.
- Remove o .h264 automaticamente após o .mp4 ser criado com sucesso.
- Salva áudio e vídeo separados.
- Organiza saída em pastas: video/, audio/, temp/, logs/, metadata/.
- Mede tempos de gravação, remux, sync e execução total.
- Gera metadata JSON e log da execução.
"""

import argparse
import datetime as dt
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
PROJECT_VERSION = "0.4"

DEFAULT_CONFIG: Dict[str, Any] = {
    "duration": 60,
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
}

CONFIG_KEY_ALIASES = {
    "record_duration_s": "duration", "duration_s": "duration", "duration": "duration",
    "cycle_period_s": "cycle_period_s", "cycle_s": "cycle_period_s", "period_s": "cycle_period_s",
    "run_mode": "run_mode", "mode": "run_mode",
    "media_dir": "media_dir", "output_dir": "media_dir",
    "width": "width", "height": "height", "fps": "fps", "bitrate": "bitrate",
    "audio_device": "audio_device", "audio_format": "audio_format",
    "keep_h264": "keep_h264", "delete_h264_after_mp4": "delete_h264_after_mp4",
    "start_delay": "start_delay", "start_delay_s": "start_delay",
    "extra_timeout": "extra_timeout", "extra_timeout_s": "extra_timeout",
    "max_storage_percent": "max_storage_percent", "storage_max_percent": "max_storage_percent",
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
    if key in ("duration", "cycle_period_s", "width", "height", "fps", "bitrate"):
        return int(value)
    if key in ("start_delay", "extra_timeout", "max_storage_percent"):
        return float(value)
    if key in ("keep_h264", "delete_h264_after_mp4"):
        return parse_bool(value)
    if key == "run_mode":
        return value.strip().lower()
    return value


def find_default_config_path() -> Optional[Path]:
    candidates = [
        Path.cwd() / "config.txt",
        Path(__file__).resolve().parent / "config.txt",
        Path("/home/fishcam/poracam/config.txt"),
        Path("/home/pi/poracam/config.txt"),
    ]
    seen = set()
    for candidate in candidates:
        resolved = str(candidate)
        if resolved in seen:
            continue
        seen.add(resolved)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def load_config_file(path: Optional[Path]) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
    warnings: List[str] = []
    if path is None:
        return {}, None, warnings
    if not path.exists():
        warnings.append(f"Arquivo de configuração não encontrado: {path}")
        return {}, None, warnings

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
    return config, str(path), warnings


def merge_config(args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[str], List[str]]:
    final_config = dict(DEFAULT_CONFIG)
    warnings: List[str] = []
    config_path = Path(args.config).expanduser().resolve() if args.config is not None else find_default_config_path()
    file_config, config_source, file_warnings = load_config_file(config_path)
    warnings.extend(file_warnings)
    final_config.update(file_config)

    cli_overrides = {
        "duration": args.duration,
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
    }
    for key, value in cli_overrides.items():
        if value is not None:
            final_config[key] = str(value).lower() if key == "run_mode" else value
    return final_config, config_source, warnings


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
        raise RuntimeError(
            "Uso do armazenamento acima do limite: "
            f"{info['used_percent']}% usado, limite={max_storage_percent}%"
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
    if int(config["cycle_period_s"]) <= 0:
        raise ValueError("cycle_period_s precisa ser maior que zero.")
    if int(config["cycle_period_s"]) < int(config["duration"]):
        raise ValueError(
            "cycle_period_s precisa ser maior ou igual a record_duration_s/duration. "
            f"Recebido: cycle_period_s={config['cycle_period_s']}, duration={config['duration']}"
        )
    run_mode = str(config["run_mode"]).lower()
    if run_mode != "single":
        raise ValueError(
            "Na v0.4, apenas run_mode=single é suportado. "
            f"Recebido: run_mode={config['run_mode']}"
        )
    if int(config["width"]) <= 0:
        raise ValueError("width precisa ser maior que zero.")
    if int(config["height"]) <= 0:
        raise ValueError("height precisa ser maior que zero.")
    if int(config["fps"]) <= 0:
        raise ValueError("fps precisa ser maior que zero.")
    if int(config["bitrate"]) <= 0:
        raise ValueError("bitrate precisa ser maior que zero.")
    if float(config["start_delay"]) < 0:
        raise ValueError("start_delay não pode ser negativo.")
    if float(config["extra_timeout"]) < 0:
        raise ValueError("extra_timeout não pode ser negativo.")
    if not str(config["media_dir"]).strip():
        raise ValueError("media_dir não pode ser vazio.")
    if not str(config["audio_device"]).strip():
        raise ValueError("audio_device não pode ser vazio.")
    if not str(config["audio_format"]).strip():
        raise ValueError("audio_format não pode ser vazio.")
    max_storage_percent = float(config["max_storage_percent"])
    if max_storage_percent <= 0 or max_storage_percent > 100:
        raise ValueError("max_storage_percent precisa estar entre 0 e 100.")


def record(config: Dict[str, Any], config_source: Optional[str], config_warnings: List[str]) -> int:
    validate_config(config)
    total_t0 = time.monotonic()
    start_iso = iso_now()
    stamp = now_stamp()
    duration = int(config["duration"])
    media_dir = Path(str(config["media_dir"])).expanduser().resolve()

    video_dir = media_dir / "video"
    audio_dir = media_dir / "audio"
    temp_dir = media_dir / "temp"
    log_dir = media_dir / "logs"
    metadata_dir = media_dir / "metadata"
    for directory in [video_dir, audio_dir, temp_dir, log_dir, metadata_dir]:
        ensure_dir(directory)

    log_file = log_dir / f"{stamp}_poracam.log"
    metadata_file = metadata_dir / f"{stamp}_metadata.json"
    temp_h264 = temp_dir / f"{stamp}_video.h264"
    video_mp4 = video_dir / f"{stamp}_video.mp4"
    audio_wav = audio_dir / f"{stamp}_audio.wav"

    status = "unknown"
    error_message: Optional[str] = None
    video_proc: Optional[subprocess.Popen] = None
    audio_proc: Optional[subprocess.Popen] = None
    h264_deleted = False
    storage_before: Optional[Dict[str, Any]] = None
    storage_after: Optional[Dict[str, Any]] = None

    timing: Dict[str, Optional[float]] = {
        "storage_check_s": None,
        "recording_s": None,
        "video_mp4_processing_s": None,
        "delete_h264_s": None,
        "sync_s": None,
        "total_s": None,
    }

    metadata: Dict[str, Any] = {
        "project": PROJECT_NAME,
        "version": PROJECT_VERSION,
        "status": status,
        "start_time": start_iso,
        "end_time": None,
        "duration_requested_s": duration,
        "duration_actual_s": None,
        "config_source": config_source,
        "config_warnings": config_warnings,
        "media_dir": str(media_dir),
        "paths": {
            "video_mp4": str(video_mp4),
            "audio_wav": str(audio_wav),
            "temp_h264": str(temp_h264),
            "log": str(log_file),
            "metadata": str(metadata_file),
        },
        "settings": {
            "run_mode": str(config["run_mode"]),
            "duration": duration,
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
        },
        "storage": {"before": None, "after": None},
        "commands": {},
        "timing": timing,
        "files": {},
        "h264_deleted": h264_deleted,
        "error": None,
    }

    try:
        append_log(log_file, f"Poracam v{PROJECT_VERSION} recording started")
        append_log(log_file, f"Config source: {config_source or 'internal defaults / CLI only'}")
        for warning in config_warnings:
            append_log(log_file, f"CONFIG WARNING: {warning}")

        storage_t0 = time.monotonic()
        storage_before = check_storage_or_fail(media_dir, float(config["max_storage_percent"]))
        timing["storage_check_s"] = round(time.monotonic() - storage_t0, 3)
        append_log(log_file, f"Storage before recording: {storage_before['used_percent']}% used, {storage_before['free_mb']} MB free")

        raspivid = which_or_fail("raspivid")
        arecord = which_or_fail("arecord")
        ffmpeg = which_or_fail("ffmpeg")

        video_cmd = [
            raspivid, "-n", "-t", str(int(duration * 1000)),
            "-w", str(int(config["width"])), "-h", str(int(config["height"])),
            "-fps", str(int(config["fps"])), "-b", str(int(config["bitrate"])),
            "-o", str(temp_h264),
        ]
        audio_cmd = [
            arecord, "-D", str(config["audio_device"]), "-f", str(config["audio_format"]),
            "-d", str(int(duration)), str(audio_wav),
        ]
        ffmpeg_cmd = [
            ffmpeg, "-y", "-framerate", str(int(config["fps"])),
            "-i", str(temp_h264), "-c:v", "copy", str(video_mp4),
        ]
        metadata["commands"]["video"] = video_cmd
        metadata["commands"]["audio"] = audio_cmd
        metadata["commands"]["ffmpeg_video_mp4"] = ffmpeg_cmd

        append_log(log_file, f"Media dir: {media_dir}")
        append_log(log_file, f"Run mode: {config['run_mode']}")
        append_log(log_file, f"Duration requested: {duration} s")
        append_log(log_file, f"Cycle period configured: {config['cycle_period_s']} s")
        append_log(log_file, f"Temporary H264: {temp_h264}")
        append_log(log_file, f"Final MP4 video: {video_mp4}")
        append_log(log_file, f"Audio WAV: {audio_wav}")

        recording_t0 = time.monotonic()
        audio_proc = start_process(audio_cmd, log_file)
        time.sleep(float(config["start_delay"]))
        video_proc = start_process(video_cmd, log_file)
        video_rc = video_proc.wait(timeout=duration + float(config["extra_timeout"]))
        audio_rc = audio_proc.wait(timeout=duration + float(config["extra_timeout"]))
        close_proc_log(video_proc)
        close_proc_log(audio_proc)
        timing["recording_s"] = round(time.monotonic() - recording_t0, 3)

        append_log(log_file, f"Video return code: {video_rc}")
        append_log(log_file, f"Audio return code: {audio_rc}")
        append_log(log_file, f"Recording phase elapsed: {timing['recording_s']} s")

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
        append_log(log_file, f"MP4 generation elapsed: {timing['video_mp4_processing_s']} s")

        if not bool(config["keep_h264"]):
            delete_t0 = time.monotonic()
            h264_deleted = remove_file(temp_h264, log_file)
            timing["delete_h264_s"] = round(time.monotonic() - delete_t0, 3)
        else:
            append_log(log_file, "Keeping H264 temporary file because keep_h264=true")
            h264_deleted = False
            timing["delete_h264_s"] = 0.0

        timing["sync_s"] = round(sync_filesystem(log_file), 3)
        storage_after = get_storage_info(media_dir)
        append_log(log_file, f"Storage after recording: {storage_after['used_percent']}% used, {storage_after['free_mb']} MB free")
        status = "ok"

    except Exception as exc:
        status = "error"
        error_message = str(exc)
        append_log(log_file, f"ERROR: {error_message}")
        if video_proc is not None and video_proc.poll() is None:
            stop_process(video_proc, "raspivid", log_file)
        if audio_proc is not None and audio_proc.poll() is None:
            stop_process(audio_proc, "arecord", log_file)
        timing["sync_s"] = round(sync_filesystem(log_file), 3)
        try:
            storage_after = get_storage_info(media_dir)
        except Exception:
            storage_after = None

    finally:
        end_iso = iso_now()
        total_elapsed = time.monotonic() - total_t0
        timing["total_s"] = round(total_elapsed, 3)
        metadata["status"] = status
        metadata["end_time"] = end_iso
        metadata["duration_actual_s"] = round(total_elapsed, 3)
        metadata["timing"] = timing
        metadata["storage"] = {"before": storage_before, "after": storage_after}
        metadata["h264_deleted"] = h264_deleted
        metadata["files"] = {
            "video_mp4": file_info(video_mp4),
            "audio_wav": file_info(audio_wav),
            "temp_h264": file_info(temp_h264),
            "log": file_info(log_file),
        }
        metadata["error"] = error_message
        with metadata_file.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        append_log(log_file, f"Metadata saved: {metadata_file}")
        append_log(log_file, f"Final status: {status}")
        append_log(log_file, f"Total elapsed: {timing['total_s']} s")

    return 0 if status == "ok" else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poracam v0.4: gravação MP4/WAV com config.txt, validação e checagem de armazenamento.")
    parser.add_argument("--config", default=None, help="Caminho para o config.txt. Se omitido, o script procura automaticamente.")
    parser.add_argument("--duration", type=int, default=None, help="Duração da gravação em segundos.")
    parser.add_argument("--cycle-period-s", type=int, default=None, help="Período total do ciclo em segundos.")
    parser.add_argument("--run-mode", default=None, help="Modo de execução. Na v0.4, apenas 'single' é suportado.")
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
    parser.add_argument("--keep-h264", action="store_true", default=None, help="Mantém o arquivo .h264 temporário após gerar o MP4.")
    parser.add_argument("--version", action="version", version=f"Poracam {PROJECT_VERSION}")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        config, config_source, config_warnings = merge_config(args)
        return record(config, config_source, config_warnings)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())