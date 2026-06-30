#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poracam_record.py — Poracam v0.2

Gravador simples para Raspberry Pi Zero W com Raspberry Pi OS Buster.

Características da v0.2:
- Grava vídeo com raspivid em .h264 temporário.
- Grava áudio com arecord em .wav.
- Gera vídeo final .mp4 por padrão usando ffmpeg sem recodificar o vídeo.
- Remove o .h264 automaticamente após o .mp4 ser criado com sucesso.
- Salva áudio e vídeo separados.
- Organiza saída em pastas: video/, audio/, temp/, logs/, metadata/.
- Mede tempos de gravação, remux, sync e execução total.
- Gera metadata JSON e log da execução.

Uso básico:
    python3 poracam_record.py --duration 60

Uso com diretório específico:
    python3 poracam_record.py --duration 60 --media-dir /home/fishcam/poracam/media

Uso preservando o .h264 temporário:
    python3 poracam_record.py --duration 60 --keep-h264

Uso com dispositivo de áudio específico:
    python3 poracam_record.py --duration 60 --audio-device plughw:1,0
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
from typing import Any, Dict, List, Optional


PROJECT_NAME = "poracam"
PROJECT_VERSION = "0.2"


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


def sync_filesystem(log_file: Path) -> float:
    t0 = time.monotonic()
    try:
        run_command(["sync"], log_file, check=False)
    except Exception as exc:
        append_log(log_file, f"WARNING: sync failed: {exc}")
    return time.monotonic() - t0


def validate_args(args: argparse.Namespace) -> None:
    if args.duration <= 0:
        raise ValueError("--duration precisa ser maior que zero.")
    if args.width <= 0:
        raise ValueError("--width precisa ser maior que zero.")
    if args.height <= 0:
        raise ValueError("--height precisa ser maior que zero.")
    if args.fps <= 0:
        raise ValueError("--fps precisa ser maior que zero.")
    if args.bitrate <= 0:
        raise ValueError("--bitrate precisa ser maior que zero.")
    if args.start_delay < 0:
        raise ValueError("--start-delay não pode ser negativo.")
    if args.extra_timeout < 0:
        raise ValueError("--extra-timeout não pode ser negativo.")


def record(args: argparse.Namespace) -> int:
    validate_args(args)

    total_t0 = time.monotonic()
    start_iso = iso_now()
    stamp = now_stamp()

    media_dir = Path(args.media_dir).expanduser().resolve()
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

    timing: Dict[str, Optional[float]] = {
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
        "duration_requested_s": args.duration,
        "duration_actual_s": None,
        "media_dir": str(media_dir),
        "paths": {
            "video_mp4": str(video_mp4),
            "audio_wav": str(audio_wav),
            "temp_h264": str(temp_h264),
            "log": str(log_file),
            "metadata": str(metadata_file),
        },
        "settings": {
            "width": args.width,
            "height": args.height,
            "fps": args.fps,
            "bitrate": args.bitrate,
            "audio_device": args.audio_device,
            "audio_format": args.audio_format,
            "keep_h264": bool(args.keep_h264),
            "delete_h264_after_mp4": not bool(args.keep_h264),
            "start_delay_s": args.start_delay,
            "extra_timeout_s": args.extra_timeout,
        },
        "commands": {},
        "timing": timing,
        "files": {},
        "h264_deleted": h264_deleted,
        "error": None,
    }

    try:
        raspivid = which_or_fail("raspivid")
        arecord = which_or_fail("arecord")
        ffmpeg = which_or_fail("ffmpeg")

        video_cmd = [
            raspivid, "-n", "-t", str(int(args.duration * 1000)),
            "-w", str(args.width), "-h", str(args.height),
            "-fps", str(args.fps), "-b", str(args.bitrate),
            "-o", str(temp_h264),
        ]

        audio_cmd = [
            arecord, "-D", args.audio_device,
            "-f", args.audio_format,
            "-d", str(int(args.duration)),
            str(audio_wav),
        ]

        ffmpeg_cmd = [
            ffmpeg, "-y", "-framerate", str(args.fps),
            "-i", str(temp_h264), "-c:v", "copy", str(video_mp4),
        ]

        metadata["commands"]["video"] = video_cmd
        metadata["commands"]["audio"] = audio_cmd
        metadata["commands"]["ffmpeg_video_mp4"] = ffmpeg_cmd

        append_log(log_file, f"Poracam v{PROJECT_VERSION} recording started")
        append_log(log_file, f"Media dir: {media_dir}")
        append_log(log_file, f"Duration requested: {args.duration} s")
        append_log(log_file, f"Temporary H264: {temp_h264}")
        append_log(log_file, f"Final MP4 video: {video_mp4}")
        append_log(log_file, f"Audio WAV: {audio_wav}")

        recording_t0 = time.monotonic()
        audio_proc = start_process(audio_cmd, log_file)
        time.sleep(args.start_delay)
        video_proc = start_process(video_cmd, log_file)

        video_rc = video_proc.wait(timeout=args.duration + args.extra_timeout)
        audio_rc = audio_proc.wait(timeout=args.duration + args.extra_timeout)

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

        if not args.keep_h264:
            delete_t0 = time.monotonic()
            h264_deleted = remove_file(temp_h264, log_file)
            timing["delete_h264_s"] = round(time.monotonic() - delete_t0, 3)
        else:
            append_log(log_file, "Keeping H264 temporary file because --keep-h264 was used")
            h264_deleted = False
            timing["delete_h264_s"] = 0.0

        timing["sync_s"] = round(sync_filesystem(log_file), 3)
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

    finally:
        end_iso = iso_now()
        total_elapsed = time.monotonic() - total_t0
        timing["total_s"] = round(total_elapsed, 3)

        metadata["status"] = status
        metadata["end_time"] = end_iso
        metadata["duration_actual_s"] = round(total_elapsed, 3)
        metadata["timing"] = timing
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
    parser = argparse.ArgumentParser(description="Poracam v0.2: gravação simples de vídeo MP4 e áudio WAV separado.")

    parser.add_argument("--duration", type=int, default=60, help="Duração da gravação em segundos. Padrão: 60.")
    parser.add_argument("--media-dir", default="/home/fishcam/poracam/media", help="Diretório base para salvar mídia, logs e metadados. Padrão: /home/fishcam/poracam/media.")
    parser.add_argument("--width", type=int, default=1280, help="Largura do vídeo. Padrão: 1280.")
    parser.add_argument("--height", type=int, default=720, help="Altura do vídeo. Padrão: 720.")
    parser.add_argument("--fps", type=int, default=30, help="Frames por segundo. Padrão: 30.")
    parser.add_argument("--bitrate", type=int, default=2500000, help="Bitrate do vídeo em bits/s. Padrão: 2500000.")
    parser.add_argument("--audio-device", default="default", help="Dispositivo ALSA para áudio. Exemplos: default, plughw:1,0, hw:1,0. Padrão: default.")
    parser.add_argument("--audio-format", default="cd", help="Formato do arecord. Padrão: cd. Exemplos: cd, S16_LE.")
    parser.add_argument("--keep-h264", action="store_true", help="Mantém o arquivo .h264 temporário após gerar o MP4. Por padrão, ele é apagado.")
    parser.add_argument("--start-delay", type=float, default=0.2, help="Atraso entre início do áudio e início do vídeo, em segundos. Padrão: 0.2.")
    parser.add_argument("--extra-timeout", type=float, default=15.0, help="Tempo extra para aguardar processos finalizarem. Padrão: 15 s.")
    parser.add_argument("--version", action="version", version=f"Poracam {PROJECT_VERSION}")

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    try:
        return record(args)
    except Exception as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())