"""
Genera un archivo MP3 continuo a partir de un archivo SRT usando edge-tts.
Extrae todo el texto del SRT, lo une sin saltos y genera un solo audio fluido.
Sin aceleracion, sin depender de los tiempos del subtitulo original.
"""

import edge_tts
import subprocess
import re
import asyncio
from pathlib import Path


def parse_srt(filepath):
    with open(filepath, "r", encoding="utf-8-sig") as f:
        content = f.read()

    blocks = re.split(r"\n\s*\n", content.strip())
    texts = []

    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        text = " ".join(lines[2:]).strip()
        if text:
            texts.append(text)

    return texts


async def select_voice():
    print("\nBuscando voces disponibles...")
    voices = await edge_tts.list_voices()
    spanish = [v for v in voices if v["Locale"].startswith("es-")]

    print("\n--- Voces en espanol disponibles ---")
    for i, v in enumerate(spanish):
        gender = "M" if v["Gender"] == "Male" else "F"
        print(f"  {i + 1:2d}. [{gender}] {v['ShortName']}")

    while True:
        try:
            choice = int(input(f"\nSelecciona una voz (1-{len(spanish)}): ")) - 1
            if 0 <= choice < len(spanish):
                return spanish[choice]["ShortName"]
        except (ValueError, EOFError):
            pass
        print("Seleccion invalida, intenta de nuevo.")





async def main():
    voice = await select_voice()
    print(f"\nVoz seleccionada: {voice}")

    script_dir = Path(__file__).parent
    srt_files = list(script_dir.glob("*.srt"))

    if not srt_files:
        print("\nNo se encontro archivo .srt en la carpeta del script")
        return

    srt_file = srt_files[0]
    texts = parse_srt(srt_file)
    print(f"Subtitulos: {len(texts)}")
    print(f"Archivo SRT: {srt_file.name}")

    if not texts:
        print("No se encontro texto en el SRT")
        return

    full_text = " ".join(texts)
    print(f"Caracteres totales: {len(full_text)}")

    output = script_dir / (srt_file.stem + ".mp3")
    print(f"\nGenerando audio...")

    comm = edge_tts.Communicate(full_text, voice)
    await comm.save(str(output))

    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(output)]
    import json
    result = subprocess.run(cmd, capture_output=True, text=True)
    dur = float(json.loads(result.stdout)["format"]["duration"])
    size_mb = output.stat().st_size / (1024 * 1024)

    print(f"\nArchivo generado: {output}")
    print(f"Duracion: {dur:.1f}s ({dur / 60:.1f} min)")
    print(f"Tamano: {size_mb:.1f} MB")


if __name__ == "__main__":
    asyncio.run(main())
