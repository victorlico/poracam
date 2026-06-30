#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
poracam_record.py

Primeira versão de teste do projeto Poracam.

Objetivo:
- Gravar vídeo com raspivid.
- Gravar áudio com arecord.
- Salvar vídeo e áudio separados.
- Opcionalmente converter/remuxar o vídeo H.264 para MP4 sem recodificar.
- Registrar log simples da execução.

Pensado para Raspberry Pi Zero W com Raspberry Pi OS Buster.

Uso básico:
    python3 poracam_record.py --duration 60

Uso salvando em diretório específico:
    python3 poracam_record.py --duration 60 --media-dir /home/pi/poracam/media

Uso gerando também MP4:
    python3 poracam_record.py --duration 60 --make-mp4

Observação:
- Por padrão, o MP4 gerado contém apenas vídeo.
- O áudio continua salvo separado em WAV.
- A junção áudio+vídeo em um único MP4 fica para uma etapa posterior.
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


PROJECT_NAME = "poracam"


def now_stamp() -> str:
    """Retorna timestamp seguro para nome de arquivo."""
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def iso_now() -> str:
    """Retorna data/hora local em formato ISO."""
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def which_or_fail(command: str) -> str:
    """Localiza executável no PATH ou aborta com erro claro."""
    found = shutil.which(command)
    if not found:
        raise RuntimeError(f"Comando não encontrado no PATH: {command}")
    return found


def run_command(command, log_file: Path, check: bool = True) -> subprocess.CompletedProcess:
    """Executa comando simples, registrando stdout/stderr no log."""
    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"\n[{iso_now()}] RUN: {' '.join(map(str, command))}\n")
        log.flush()

        result = subprocess.run(
            command,
            stdout=log,
            stderr=log,
            check=False,
        )

        log.write(f"[{iso_now()}] RETURN CODE: {result.returncode}\n")
        log.flush()

    if check and result.returncode != 0:
        raise RuntimeError(f"Comando falhou com return code {result.returncode}: {command}")

    return result


def start_process(command, log_file: Path) -> subprocess.Popen:
    """Inicia processo e redireciona saída para o log."""
    log = log_file.open("ab", buffering=0)
    log.write(f"\n[{iso_now()}] START: {' '.join(map(str, command))}\n".encode("utf-8"))
    proc = subprocess.Popen(
        command,
        stdout=log,
        stderr=log,
        preexec_fn=os.setsid,
    )
    # Guardar referência ao arquivo de log para fechar depois.
    proc._poracam_log_handle = log  # type: ignore[attr-defined]
    return proc


def stop_process(proc: subprocess.Popen, name: str, log_file: Path, timeout: float = 5.0) -> None:
    """Tenta encerrar um processo de forma controlada."""
    if proc.poll() is not None:
        close_proc_log(proc)
        return

    with log_file.open("a", encoding="utf-8") as log:
        log.write(f"[{iso_now()}] STOP requested for {name}, pid={proc.pid}\n")

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"[{iso_now()}] {name} did not stop after SIGTERM; sending SIGKILL\n")
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=timeout)
    finally:
        close_proc_log(proc)


def close_proc_log(proc: subprocess.Popen) -> None:
    """Fecha handle de log associado ao processo, se existir."""
    handle = getattr(proc, "_poracam_log_handle", None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def file_info(path: Path) -> dict:
    if not path.exists():
        return {
            "exists": False,
            "size_bytes": 0,
        }
    return {
        "exists": True,
        "size_bytes": path.stat().st_size,
    }


def sync_filesystem(log_file: Path) -> None:
    """Força sincronização do filesystem."""
    try:
        run_command(["sync"], log_file, check=False)
    except Exception:
        # sync dificilmente falha; não deve derrubar o teste.
        pass


def record(args) -> int:
    start_time = time.monotonic()
    start_iso = iso_now()
    stamp = now_stamp()

    media_dir = Path(args.media_dir).expanduser().resolve()
    raw_dir = media_dir / "raw"
    mp4_dir = media_dir / "mp4"
    log_dir = media_dir / "logs"
    meta_dir = media_dir / "metadata"

    for directory in [raw_dir, mp4_dir, log_dir, meta_dir]:
        ensure_dir(directory)

    log_file = log_dir / f"{stamp}_poracam.log"
    metadata_file = meta_dir / f"{stamp}_metadata.json"

    video_h264 = raw_dir / f"{stamp}_video.h264"
    audio_wav = raw_dir / f"{stamp}_audio.wav"
    video_mp4 = mp4_dir / f"{stamp}_video.mp4"

    status = "unknown"
    error_message = None
    video_proc = None
    audio_proc = None

    metadata = {
        "project": PROJECT_NAME,
        "status": status,
        "start_time": start_iso,
        "end_time": None,
        "duration_requested_s": args.duration,
        "duration_actual_s": None,
        "video_h264": str(video_h264),
        "audio_wav": str(audio_wav),
        "video_mp4": str(video_mp4) if args.make_mp4 else None,
        "make_mp4": bool(args.make_mp4),
        "commands": {},
        "files": {},
        "error": None,
    }

    try:
        raspivid = which_or_fail("raspivid")
        arecord = which_or_fail("arecord")

        if args.make_mp4:
            ffmpeg = which_or_fail("ffmpeg")
        else:
            ffmpeg = None

        # raspivid grava por tempo próprio em milissegundos.
        # -n: sem preview.
        # -t: duração em ms.
        # -o: saída h264.
        # -w/-h/-fps/-b: parâmetros configuráveis.
        video_cmd = [
            raspivid,
            "-n",
            "-t", str(int(args.duration * 1000)),
            "-w", str(args.width),
            "-h", str(args.height),
            "-fps", str(args.fps),
            "-b", str(args.bitrate),
            "-o", str(video_h264),
        ]

        # arecord grava áudio WAV por duração própria.
        # -D: dispositivo ALSA.
        # -f cd: 16 bit, 44100 Hz, stereo.
        # -d: duração em segundos.
        audio_cmd = [
            arecord,
            "-D", args.audio_device,
            "-f", args.audio_format,
            "-d", str(int(args.duration)),
            str(audio_wav),
        ]

        metadata["commands"]["video"] = video_cmd
        metadata["commands"]["audio"] = audio_cmd

        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"[{iso_now()}] Poracam recording started\n")
            log.write(f"[{iso_now()}] Media dir: {media_dir}\n")
            log.write(f"[{iso_now()}] Duration: {args.duration} s\n")
            log.write(f"[{iso_now()}] Video output: {video_h264}\n")
            log.write(f"[{iso_now()}] Audio output: {audio_wav}\n")
            log.flush()

        # Inicia áudio e vídeo quase juntos.
        # A sincronização fina não é objetivo desta primeira versão.
        audio_proc = start_process(audio_cmd, log_file)
        time.sleep(args.start_delay)
        video_proc = start_process(video_cmd, log_file)

        # Espera ambos terminarem. Cada processo tem duração própria.
        video_rc = video_proc.wait(timeout=args.duration + args.extra_timeout)
        audio_rc = audio_proc.wait(timeout=args.duration + args.extra_timeout)

        close_proc_log(video_proc)
        close_proc_log(audio_proc)

        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"[{iso_now()}] Video return code: {video_rc}\n")
            log.write(f"[{iso_now()}] Audio return code: {audio_rc}\n")

        if video_rc != 0:
            raise RuntimeError(f"raspivid terminou com erro: return code {video_rc}")
        if audio_rc != 0:
            raise RuntimeError(f"arecord terminou com erro: return code {audio_rc}")

        # Opcional: remux do H264 para MP4 sem recodificar.
        # Este MP4 contém apenas vídeo.
        if args.make_mp4:
            ffmpeg_cmd = [
                ffmpeg,
                "-y",
                "-framerate", str(args.fps),
                "-i", str(video_h264),
                "-c:v", "copy",
                str(video_mp4),
            ]
            metadata["commands"]["ffmpeg_video_mp4"] = ffmpeg_cmd
            run_command(ffmpeg_cmd, log_file, check=True)

        sync_filesystem(log_file)
        status = "ok"

    except Exception as exc:
        status = "error"
        error_message = str(exc)

        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"[{iso_now()}] ERROR: {error_message}\n")

        # Em caso de erro/interrupção, tenta finalizar processos vivos.
        if video_proc is not None and video_proc.poll() is None:
            stop_process(video_proc, "raspivid", log_file)
        if audio_proc is not None and audio_proc.poll() is None:
            stop_process(audio_proc, "arecord", log_file)

        sync_filesystem(log_file)

    finally:
        end_iso = iso_now()
        duration_actual = time.monotonic() - start_time

        metadata["status"] = status
        metadata["end_time"] = end_iso
        metadata["duration_actual_s"] = round(duration_actual, 3)
        metadata["files"] = {
            "video_h264": file_info(video_h264),
            "audio_wav": file_info(audio_wav),
            "video_mp4": file_info(video_mp4) if args.make_mp4 else None,
            "log": file_info(log_file),
        }
        metadata["error"] = error_message

        with metadata_file.open("w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)

        with log_file.open("a", encoding="utf-8") as log:
            log.write(f"[{iso_now()}] Metadata saved: {metadata_file}\n")
            log.write(f"[{iso_now()}] Final status: {status}\n")

    return 0 if status == "ok" else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poracam: gravação simples de vídeo e áudio na Raspberry Pi."
    )

    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duração da gravação em segundos. Padrão: 60.",
    )

    parser.add_argument(
        "--media-dir",
        default="/home/pi/poracam/media",
        help="Diretório base para salvar mídia, logs e metadados.",
    )

    parser.add_argument(
        "--width",
        type=int,
        default=1280,
        help="Largura do vídeo. Padrão: 1280.",
    )

    parser.add_argument(
        "--height",
        type=int,
        default=720,
        help="Altura do vídeo. Padrão: 720.",
    )

    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Frames por segundo. Padrão: 30.",
    )

    parser.add_argument(
        "--bitrate",
        type=int,
        default=2500000,
        help="Bitrate do vídeo em bits/s. Padrão: 2500000.",
    )

    parser.add_argument(
        "--audio-device",
        default="default",
        help=(
            "Dispositivo ALSA para áudio. "
            "Exemplos: default, plughw:1,0, hw:1,0. Padrão: default."
        ),
    )

    parser.add_argument(
        "--audio-format",
        default="cd",
        help=(
            "Formato do arecord. Padrão: cd. "
            "Exemplos: cd, S16_LE."
        ),
    )

    parser.add_argument(
        "--make-mp4",
        action="store_true",
        help="Também gera um MP4 apenas com vídeo, remuxando o H264 via ffmpeg.",
    )

    parser.add_argument(
        "--start-delay",
        type=float,
        default=0.2,
        help="Atraso entre início do áudio e início do vídeo, em segundos. Padrão: 0.2.",
    )

    parser.add_argument(
        "--extra-timeout",
        type=float,
        default=15.0,
        help="Tempo extra para aguardar processos finalizarem. Padrão: 15 s.",
    )

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.duration <= 0:
        print("Erro: --duration precisa ser maior que zero.", file=sys.stderr)
        return 2

    return record(args)


if __name__ == "__main__":
    sys.exit(main())
