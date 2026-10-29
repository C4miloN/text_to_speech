import asyncio
import os
import re
import subprocess
import tempfile
import shutil
from pathlib import Path

import edge_tts

from podcast_tts import (
    assign_voices,
    clean_for_tts,
    concat_mp3,
    ffprobe_stream,
    long_path,
    make_silence,
    parse_document,
    shorten_output_path,
    synthesize_segment,
)


NOISE_DB = -50


def parse_silences(text):
    """Extrae intervalos (inicio, fin) de los silencios detectados por silencedetect."""
    intervals = []
    start = None
    for match in re.finditer(r"silence_(start|end):\s*([\d.]+)", text):
        kind, value = match.group(1), float(match.group(2))
        if kind == "start":
            start = value
        elif kind == "end" and start is not None:
            intervals.append((start, value))
            start = None
    return intervals, start


def truncate_silences(src, out, threshold, reduction, log=None):
    """Reduce los silencios mayores a `threshold` segundos a un porcentaje de su
    duración original. `reduction` es el % a quitar (0 = sin cambios, 90 = queda 10%)."""
    if threshold <= 0 or reduction <= 0:
        shutil.copy(src, out)
        return out

    keep = max(0.0, 1.0 - min(100.0, reduction) / 100.0)
    if log:
        log(f"  Truncando silencios > {threshold:.2f}s de {Path(src).name} "
            f"(se conserva el {keep * 100:.0f}%)...")

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(src),
         "-af", f"silencedetect=noise={NOISE_DB}dB:d={threshold:.3f}",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences, trailing = parse_silences(proc.stderr)
    total = probe_duration(src)
    if trailing is not None:
        silences.append((trailing, total))

    if not silences:
        if log:
            log(f"  Sin silencios mayores a {threshold:.2f}s en {Path(src).name}.")
        shutil.copy(src, out)
        return out

    segments = []
    pos = 0.0
    for s, e in silences:
        if e <= s:
            continue
        if s - pos > 0.01:
            segments.append(("audio", pos, s))
        shortened = (e - s) * keep
        if shortened > 0.005:
            segments.append(("silence", shortened))
        pos = e
    if total - pos > 0.01:
        segments.append(("audio", pos, total))
    if not segments:
        shutil.copy(src, out)
        return out

    sample_rate, channels = ffprobe_stream(src)
    layout = "stereo" if channels > 1 else "mono"
    n_audio = sum(1 for seg in segments if seg[0] == "audio")
    n_silence = len(segments) - n_audio

    filter_parts = [f"[0:a]asplit={n_audio}"
                    + "".join(f"[a{i}]" for i in range(n_audio))]
    if n_silence:
        filter_parts.append(f"[1:a]asplit={n_silence}"
                            + "".join(f"[s{i}]" for i in range(n_silence)))

    a_idx = s_idx = 0
    for i, seg in enumerate(segments):
        if seg[0] == "audio":
            _, s, e = seg
            filter_parts.append(
                f"[a{a_idx}]atrim=start={s:.3f}:end={e:.3f},"
                f"asetpts=PTS-STARTPTS[g{i}]")
            a_idx += 1
        else:
            _, duration = seg
            filter_parts.append(
                f"[s{s_idx}]aformat=sample_fmts=fltp:"
                f"sample_rates={sample_rate}:channel_layouts={layout},"
                f"atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[g{i}]")
            s_idx += 1

    filter_parts.append(
        "".join(f"[g{i}]" for i in range(len(segments)))
        + f"concat=n={len(segments)}:v=0:a=1[out]")
    filter_graph = ";".join(filter_parts)

    base = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    if n_silence:
        base += ["-f", "lavfi", "-i",
                 f"anullsrc=r={sample_rate}:cl={layout}"]
    tail = ["-map", "[out]", "-c:a", "libmp3lame", "-b:a", "192k", str(out)]

    # El grafo puede tener miles de tramos y superar el límite de la línea de
    # comandos de Windows (WinError 206). Se pasa mediante un archivo de filtro.
    script = Path(str(out) + ".filter.txt")
    script.write_text(filter_graph, encoding="utf-8")

    last = None
    for flag in ("-filter_complex_script", "-/filter_complex"):
        proc = subprocess.run(base + [flag, str(script)] + tail,
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return out
        last = proc
        if "Option" not in proc.stderr and "not found" not in proc.stderr:
            break
    raise RuntimeError(
        "Error de ffmpeg al truncar silencios: "
        + (last.stderr.strip()[:300] if last else "desconocido")
    )


def probe_duration(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    return float(proc.stdout.strip())


async def fetch_spanish_voices():
    data = await edge_tts.list_voices()
    voices = []
    for v in data:
        if v.get("ShortName", "").startswith("es-") and v.get("Status") == "GA":
            voices.append({
                "short": v["ShortName"],
                "gender": "female" if v.get("Gender") == "Female"
                else "male" if v.get("Gender") == "Male" else "neutral",
                "locale": v.get("LocaleName", v.get("Locale", "")),
                "label": v.get("FriendlyName", v["ShortName"]),
            })
    return voices


def build_voice_pools(voices, enabled):
    pools = {"female": [], "male": [], "neutral": []}
    for voice in voices:
        if voice["short"] in enabled:
            pools[voice["gender"]].append(voice["short"])
    return pools


def scale_audio(src, out, volume, log=None):
    if log:
        log(f"  Ajustando volumen de {Path(src).name} al {volume * 100:.0f}%...")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src),
         "-af", f"volume={volume:.3f}",
         "-c:a", "libmp3lame", "-b:a", "192k", str(out)],
        check=True,
    )


def build_background(src, out, volume, duration, is_loop, log=None):
    if log:
        if is_loop:
            log(f"  Fondo en bucle hasta {duration:.1f}s...")
        else:
            log(f"  Fondo (sin bucle), volumen {volume * 100:.0f}%...")
    args = ["ffmpeg", "-y", "-v", "error"]
    if is_loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", str(src), "-t", f"{duration:.3f}",
             "-af", f"volume={volume:.3f}",
             "-c:a", "libmp3lame", "-b:a", "192k", str(out)]
    subprocess.run(args, check=True)


def mix_with_background(main, bg, out, main_volume, delay_ms, log=None):
    if log:
        if delay_ms > 0:
            log(f"  Mezclando voz ({main_volume * 100:.0f}%) con fondo "
                f"(empieza tras {delay_ms / 1000:.1f}s)...")
        else:
            log(f"  Mezclando voz ({main_volume * 100:.0f}%) con fondo...")
    filter_complex = (
        f"[0:a]volume={main_volume:.3f}[a0];"
        f"[1:a]adelay={delay_ms}:all=1[a1];"
        f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-i", str(main), "-i", str(bg),
         "-filter_complex", filter_complex, "-map", "[a]",
         "-c:a", "libmp3lame", "-b:a", "192k", str(out)],
        check=True,
    )


def synth_voice_track(docx_path, rate, pause, tmp_dir, log, voice_pools=None):
    participants, blocks = parse_document(docx_path)
    if not blocks:
        raise RuntimeError("No se encontraron intervenciones en el documento.")
    voices = assign_voices(participants, voice_pools)

    log("  Sintetizando voces con edge-tts...")
    parts = []

    async def run_all():
        sem = asyncio.Semaphore(4)

        async def worker(task):
            async with sem:
                await synthesize_segment(*task)

        tasks = []
        for i, (speaker, text) in enumerate(blocks):
            part = Path(tmp_dir) / f"part_{i:03d}.mp3"
            parts.append(part)
            tasks.append((text, voices[speaker], part, rate))
        await asyncio.gather(*(worker(t) for t in tasks))

    asyncio.run(run_all())

    silence = Path(tmp_dir) / "silence.mp3"
    sample_rate, channels = ffprobe_stream(parts[0])
    if pause > 0:
        make_silence(silence, pause, sample_rate, channels)

    voices_mp3 = Path(tmp_dir) / "voices.mp3"
    log("  Concatenando intervenciones...")
    concat_mp3(parts, voices_mp3, pause, silence)
    return voices_mp3, participants, voices


def assemble_tracks(voices_mp3, cfg, tmp_dir, log):
    paths = []
    intro = cfg.get("intro")
    outro = cfg.get("outro")
    gap = cfg["pause"]

    threshold = cfg.get("silence_threshold", 0.0)
    reduction = cfg.get("silence_reduction", 0.0)
    trunc = threshold > 0 and reduction > 0

    def maybe_truncate(src, tag, out_name):
        if not trunc:
            return src
        out = Path(tmp_dir) / out_name
        truncate_silences(src, out, threshold, reduction, log)
        return out

    voices_proc = maybe_truncate(voices_mp3, "voz", "voices_trunc.mp3")

    intro_scaled = None
    outro_scaled = None
    if intro:
        intro_src = maybe_truncate(Path(intro), "intro", "intro_trunc.mp3")
        intro_scaled = Path(tmp_dir) / "intro_scaled.mp3"
        scale_audio(intro_src, intro_scaled, cfg["vol_intro"], log)
        paths.append(intro_scaled)
    paths.append(voices_proc)
    if outro:
        outro_src = maybe_truncate(Path(outro), "cierre", "outro_trunc.mp3")
        outro_scaled = Path(tmp_dir) / "outro_scaled.mp3"
        scale_audio(outro_src, outro_scaled, cfg["vol_final"], log)
        paths.append(outro_scaled)

    multi = len(paths) > 1
    if multi:
        main_combined = Path(tmp_dir) / "main_combined.mp3"
        silence = Path(tmp_dir) / "silence.mp3"
        log("  Uniendo intro, voces y cierre...")
        concat_mp3(paths, main_combined, gap, silence)
    else:
        main_combined = voices_proc

    if cfg.get("bg"):
        bg_scaled = Path(tmp_dir) / "bg_scaled.mp3"
        bg_src = maybe_truncate(Path(cfg["bg"]), "fondo", "bg_trunc.mp3")
        voices_dur = probe_duration(voices_proc)

        delay_s = 0.0
        if intro_scaled:
            delay_s = probe_duration(intro_scaled) + (gap if multi else 0.0)
        if outro_scaled:
            bg_len_s = voices_dur + (gap if multi else 0.0)
        else:
            bg_len_s = probe_duration(main_combined) - delay_s
        if bg_len_s <= 0:
            bg_len_s = voices_dur

        log(f"  Fondo en el tramo de voces "
            f"(empieza a {delay_s:.1f}s y dura {bg_len_s:.1f}s)...")
        build_background(bg_src, bg_scaled, cfg["vol_bg"],
                         bg_len_s, cfg["bg_loop"], log)
        final_mix = Path(tmp_dir) / "final_mix.mp3"
        mix_with_background(main_combined, bg_scaled, final_mix,
                            cfg["vol_podcast"], int(delay_s * 1000), log)
    else:
        final_mix = Path(tmp_dir) / "final_mix.mp3"
        log(f"  Ajustando volumen de la voz al {cfg['vol_podcast'] * 100:.0f}%...")
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(main_combined),
             "-af", f"volume={cfg['vol_podcast']:.3f}",
             "-c:a", "libmp3lame", "-b:a", "192k", str(final_mix)],
            check=True,
        )

    final = Path(tmp_dir) / "final.mp3"
    fade = max(0.0, cfg.get("fade", 0.0))
    dur = probe_duration(final_mix)
    if fade > 0:
        fade = min(fade, dur / 2 - 0.05)
        if fade <= 0:
            fade = 0.0
    if fade > 0:
        log(f"  Fundido de {fade:.1f}s al inicio y al final...")
        afade = (
            f"afade=t=in:st=0:d={fade:.3f},"
            f"afade=t=out:st={dur - fade:.3f}:d={fade:.3f}"
        )
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(final_mix),
             "-af", afade, "-c:a", "libmp3lame", "-b:a", "192k", str(final)],
            check=True,
        )
    else:
        shutil.copy(final_mix, final)

    return final


def build_podcast(cfg, log):
    try:
        with tempfile.TemporaryDirectory(prefix="podcast_gui_") as td:
            tmp = Path(td)
            log("Generando pista de voces...")
            voices_mp3, _participants, _voices = synth_voice_track(
                cfg["docx"], cfg["rate"], cfg["pause"], td, log,
                voice_pools=cfg.get("voice_pools"))
            log("Ensamblando pista final...")
            final = assemble_tracks(voices_mp3, cfg, tmp, log)

            out, err = shorten_output_path(cfg["out"])
            if err:
                return False, f"Error: {err}"
            if str(Path(cfg["out"])) != str(out):
                log(f"  Nombre de salida acortado por límite de Windows: {out.name}")

            parent = str(out.parent)
            try:
                os.makedirs(long_path(parent), exist_ok=True)
            except OSError as exc:
                return False, (
                    f"Error: no se pudo crear/acceder a la carpeta de salida:\n"
                    f"{parent}\n{exc}"
                )

            try:
                shutil.copy(long_path(final), long_path(out))
            except OSError as exc:
                return False, (
                    f"Error al guardar el MP3:\n{out}\n{exc}\n\n"
                    "La ruta es demasiado larga para Windows (máx. ~260 caracteres). "
                    "Acorta el nombre del archivo o guárdalo en una carpeta con una "
                    "ruta más corta."
                )

            duration = probe_duration(final)
            return True, f"Pista generada: {out} ({duration / 60:.1f} min)"
    except Exception as exc:
        msg = f"Error: {exc}"
        text = str(exc)
        if "403" in text or "WSServerHandshakeError" in text:
            msg += (
                "\n\nEl servidor de Microsoft rechazó la conexión (suele pasar cuando "
                "actualizan los encabezados de autenticación). Actualiza edge-tts con:\n"
                "  python -m pip install -U edge-tts"
            )
        return False, msg