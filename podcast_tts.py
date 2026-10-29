import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_SAFE_PATH = 248


def long_path(path):
    """Devuelve la ruta con prefijo \\\\?\\ si supera ~260 caracteres (límite de Windows).
    ffmpeg ya maneja rutas largas solo; Python necesita el prefijo."""
    p = os.path.abspath(str(path))
    if len(p) > MAX_SAFE_PATH:
        return "\\\\?\\" + p
    return p


def shorten_output_path(out_path, max_full=MAX_SAFE_PATH):
    """Acorta el NOMBRE del archivo de salida si la ruta completa excede el límite de
    Windows (~260 caracteres). Devuelve (Path, None) o (None, mensaje_error) si ni
    siquiera la carpeta deja espacio."""
    p = Path(out_path)
    suffix = p.suffix or ".mp3"
    parent = p.parent if str(p.parent) else Path.cwd()
    room = max_full - len(str(parent)) - 1
    if room < len(suffix) + 8:
        return None, (
            f"La carpeta de destino es demasiado larga ({len(str(parent))} caracteres). "
            "Elige una carpeta con una ruta más corta."
        )
    name = p.stem + suffix
    if len(name) > room:
        keep = max(8, room - len(suffix) - 7)
        digest = hashlib.md5(p.stem.encode("utf-8")).hexdigest()[:6]
        name = p.stem[:keep].rstrip(" .-_") + "_" + digest + suffix
    return parent / name, None

import edge_tts
from docx import Document

FEMALE_VOICES = [
    "es-ES-ElviraNeural",
    "es-MX-DaliaNeural",
    "es-AR-ElenaNeural",
    "es-CO-SalomeNeural",
    "es-US-PalomaNeural",
    "es-CL-CatalinaNeural",
]

MALE_VOICES = [
    "es-ES-AlvaroNeural",
    "es-MX-JorgeNeural",
    "es-CO-GonzaloNeural",
    "es-US-AlonsoNeural",
    "es-AR-TomasNeural",
]

NEUTRAL_VOICES = [
    "es-ES-LaiaNeural",
    "es-ES-ElviraNeural",
    "es-MX-JorgeNeural",
]

GENDER_MAP = {
    "mujer": "female",
    "femenino": "female",
    "female": "female",
    "ella": "female",
    "hombre": "male",
    "masculino": "male",
    "male": "male",
    "el": "male",
    "otro": "neutral",
    "neutral": "neutral",
    "n/a": "neutral",
    "na": "neutral",
    "no aplica": "neutral",
}

VOICE_POOLS = {
    "female": FEMALE_VOICES,
    "male": MALE_VOICES,
    "neutral": NEUTRAL_VOICES,
}

GENDER_HINT_RE = re.compile(r"\(\s*([^)]+?)\s*\)")


def to_ascii(text):
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text.rstrip(":.").strip()


def translate_gender(raw):
    key = to_ascii(raw.strip().lower())
    return GENDER_MAP.get(key, "neutral")


def parse_participants(header):
    if ":" in header:
        header = header.split(":", 1)[1]
    participants = []
    for part in header.split(","):
        m = re.search(r"([^(]+?)\s*\(([^)]+)\)", part)
        if not m:
            continue
        name = normalize(m.group(1)).strip(" -")
        gender = translate_gender(m.group(2))
        if name:
            participants.append((name, gender))
    return participants


def clean_for_tts(text):
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"@", " arroba ", text)
    text = re.sub(
        r"\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:\/\S*)?\b",
        lambda m: re.sub(r"\.", " punto ", m.group(0)),
        text,
    )
    return text.strip()


def parse_document(docx_path):
    doc = Document(long_path(docx_path))
    paragraphs = [p.text for p in doc.paragraphs]

    header_idx = None
    for i, text in enumerate(paragraphs):
        if ":" in text and GENDER_HINT_RE.search(text):
            header_idx = i
            break
    if header_idx is None:
        sys.exit("Error: no se encontro la primera linea 'Nombre: ... (genero)'.")

    participants = parse_participants(paragraphs[header_idx])
    if not participants:
        sys.exit("Error: no se pudieron identificar participantes en la primera linea.")

    name_lookup = {to_ascii(normalize(name)).lower(): name for name, _ in participants}

    blocks = []
    current = None
    for text in paragraphs[header_idx + 1:]:
        norm = normalize(text)
        ascii_norm = to_ascii(norm).lower()
        if ascii_norm in name_lookup:
            current = name_lookup[ascii_norm]
            blocks.append([current, []])
        else:
            if current is None:
                continue
            if not norm:
                continue
            blocks[-1][1].append(norm)

    merged = []
    for speaker, lines in blocks:
        text = " ".join(lines).strip()
        if text:
            merged.append((speaker, clean_for_tts(text)))

    return participants, merged


def assign_voices(participants, pools=None, overrides=None):
    overrides = overrides or {}
    pools = pools or VOICE_POOLS
    counts = {"female": 0, "male": 0, "neutral": 0}
    result = {}
    for name, gender in participants:
        if name in overrides:
            result[name] = overrides[name]
            continue
        pool = pools.get(gender) or pools.get("neutral") or []
        if not pool:
            pool = pools.get("female") or pools.get("male") or FEMALE_VOICES
        result[name] = pool[counts[gender] % len(pool)]
        counts[gender] += 1
    return result


def ffprobe_stream(path):
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(proc.stdout)
        stream = data["streams"][0]
        return int(stream.get("sample_rate", 24000)), int(stream.get("channels", 1))
    except Exception:
        return 24000, 1


def make_silence(path, seconds, sample_rate, channels):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={'stereo' if channels > 1 else 'mono'}",
            "-t", str(seconds), "-q:a", "9", str(path),
        ],
        check=True,
    )


def concat_mp3(parts, out_path, pause, silence_path, bitrate="192k"):
    with tempfile.TemporaryDirectory() as tmp:
        payload = []
        for part in parts:
            if pause > 0:
                payload.append(part)
                payload.append(silence_path)
            else:
                payload.append(part)
        if payload and payload[-1] == silence_path:
            payload.pop()

        list_file = Path(tmp) / "concat.txt"
        with open(list_file, "w", encoding="utf-8") as fh:
            for p in payload:
                fh.write(f"file '{p.as_posix()}'\n")

        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c:a", "libmp3lame", "-b:a", bitrate,
                str(out_path),
            ],
            check=True,
        )


async def synthesize_segment(text, voice, out_path, rate):
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(out_path))
    return out_path


async def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convierte un guion DOCX de podcast a MP3 con voces por participante (edge-tts)."
    )
    parser.add_argument("docx", help="Ruta al archivo .docx con el guion")
    parser.add_argument("--out", help="Ruta del MP3 de salida (por defecto: mismo nombre que el DOCX)")
    parser.add_argument(
        "--voice", action="append", default=[],
        help="Voz para un participante: 'Nombre=es-MX-DaliaNeural'. Se puede repetir.",
    )
    parser.add_argument("--rate", default="+0%", help="Velocidad de la voz (ej: -5%%, +10%%)")
    parser.add_argument("--pause", type=float, default=0.5, help="Silencio entre intervenciones (segundos)")
    args = parser.parse_args(argv)

    docx_path = Path(args.docx)
    if not docx_path.exists():
        sys.exit(f"Error: no existe el archivo {docx_path}")

    if args.out:
        out_path, err = shorten_output_path(args.out)
    else:
        out_path, err = shorten_output_path(docx_path.with_suffix(".mp3"))
    if err:
        sys.exit(f"Error: {err}")

    overrides = {}
    for spec in args.voice:
        if "=" in spec:
            name, voice = spec.split("=", 1)
            overrides[name.strip()] = voice.strip()
        else:
            parser.error(f"--voice debe tener el formato 'Nombre=voz': {spec}")

    participants, blocks = parse_document(docx_path)
    if not blocks:
        sys.exit("Error: no se encontraron intervenciones en el documento.")

    voices = assign_voices(participants, overrides=overrides)

    print("Participantes y voces asignadas:")
    for name, gender in participants:
        print(f"  - {name}: {voices[name]} ({'mujer' if gender == 'female' else 'hombre' if gender == 'male' else 'otro'})")
    print(f"Intervenciones detectadas: {len(blocks)}")
    print("Generando audio con edge-tts...")

    tmp_dir = Path(tempfile.mkdtemp(prefix="podcast_tts_"))
    try:
        parts = []
        sem = asyncio.Semaphore(4)

        async def worker(work):
            async with sem:
                await synthesize_segment(*work)

        tasks = []
        for i, (speaker, text) in enumerate(blocks):
            voice = voices[speaker]
            part = tmp_dir / f"part_{i:03d}.mp3"
            parts.append(part)
            tasks.append((text, voice, part, args.rate))

        await asyncio.gather(*(worker(t) for t in tasks))
        print("Concatenando bloques...")

        silence_path = tmp_dir / "silence.mp3"
        sample_rate, channels = ffprobe_stream(parts[0])
        if args.pause > 0:
            make_silence(silence_path, args.pause, sample_rate, channels)
        concat_mp3(parts, out_path, args.pause, silence_path)

        duration_cmd = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(out_path)],
            capture_output=True, text=True,
        )
        duration = float(duration_cmd.stdout.strip()) if duration_cmd.stdout.strip() else 0
        print(f"Listo: {out_path} ({duration / 60:.1f} min)")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())