import json
import queue
import re
import shutil
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
CREATE_NO_WINDOW = 0x08000000

CONFIG_PATH = Path(__file__).resolve().with_name("config_spectro.json")

RESOLUTIONS = [
    ("640x360 (SD)", 640, 360),
    ("1280x720 (HD)", 1280, 720),
    ("1920x1080 (Full HD)", 1920, 1080),
    ("2560x1440 (2K)", 2560, 1440),
    ("3840x2160 (4K)", 3840, 2160),
    ("720x1280 (Vertical HD)", 720, 1280),
    ("1080x1920 (Vertical Full)", 1080, 1920),
    ("1080x1080 (Cuadrado)", 1080, 1080),
]
RESOLUTION_LABELS = [r[0] for r in RESOLUTIONS]
RESOLUTION_MAP = {r[0]: (r[1], r[2]) for r in RESOLUTIONS}

WAVE_COLORS = [
    "cyan", "lime", "yellow", "orange", "magenta", "red", "green", "white", "pink",
]
WAVE_MODES = [
    ("Rellenada (cline)", "cline"),
    ("Linea (p2p)", "p2p"),
    ("Punteada (point)", "point"),
    ("Linea simple (line)", "line"),
]
WAVE_POSITIONS = [
    ("Centro", "center"),
    ("Abajo", "bottom"),
    ("Arriba", "top"),
]
SPECTRUM_SIZES = [
    ("Compacto (65%)", 65),
    ("Normal (80%)", 80),
    ("Grande (95%)", 95),
]
PRESETS = ["veryslow", "slow", "medium", "fast", "veryfast", "ultrafast"]
CRF_LEVELS = [
    ("Alta (18)", "18"),
    ("Media (23)", "23"),
    ("Baja (28)", "28"),
]
FPS_LEVELS = ["24", "25", "30", "60"]

AUDIO_FILTER = [
    ("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma"),
    ("Todos los archivos", "*.*"),
]
IMAGE_FILTER = [
    ("Imagen", "*.png *.jpg *.jpeg *.webp *.bmp"),
    ("Todos los archivos", "*.*"),
]
VIDEO_FILTER = [("Video MP4", "*.mp4"), ("Todos los archivos", "*.*")]


def load_config():
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_config(data):
    try:
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def even(v):
    v = int(v)
    return v if v % 2 == 0 else v - 1


def build_filter_complex(W, H, fps, size_pct, color, wave_mode, blur,
                         position, brightness):
    pad = int(round(min(W, H) * 0.02)) + 12
    pad += pad % 2

    sw = int(round(W * size_pct / 100))
    sw = even(sw)
    sh = int(round(H * 0.36))
    sh = even(sh)

    sw = min(sw, even(max(2, W - 2 * pad)))
    sh = min(sh, even(max(2, H - 2 * pad)))

    bw = sw + 2 * pad
    bh = sh + 2 * pad
    bx = even((W - bw) // 2)
    margin = even(int(round(H * 0.03)))
    if position == "top":
        by = margin
    elif position == "bottom":
        by = max(0, H - bh - margin)
    else:
        by = even((H - bh) // 2)
    sx = bx + pad
    sy = by + pad

    bg = (
        f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
        f"crop={W}:{H},eq=brightness={brightness}"
    )
    if blur:
        bg += ",boxblur=3:1"
    bg += (
        f",format=rgba,drawbox=x={bx}:y={by}:w={bw}:h={bh}"
        f":color=black@0.5:thickness=fill[bg];"
    )
    spec = (
        f"[1:a]apad,aformat=channel_layouts=mono,dynaudnorm,"
        f"volume=12dB,alimiter=limit=0.98,showwaves=s={sw}x{sh}:"
        f"mode={wave_mode}:colors={color}:r={fps},format=yuv420p[spec];"
    )
    ovl = f"[bg][spec]overlay=x={sx}:y={sy},format=yuv420p[v]"
    return bg + spec + ovl


def audio_duration(path):
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    out = subprocess.run(
        cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW
    )
    return float(out.stdout.strip())


def build_command(bg, audio, out, W, H, fps, size_pct, color, wave_mode,
                  position, blur, preset, crf, duration):
    fc = build_filter_complex(W, H, fps, size_pct, color, wave_mode, blur,
                              position, -0.05)
    return [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-loop", "1", "-framerate", str(fps), "-i", str(bg),
        "-i", str(audio),
        "-filter_complex", fc,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-profile:v", "high", "-preset", preset,
        "-crf", crf, "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-r", str(fps),
        "-t", f"{duration:.3f}",
        "-progress", "pipe:1",
        str(out),
    ]


class SpectroGUI:
    def __init__(self, root):
        self.root = root
        root.title("Onda del audio -> video MP4")
        root.minsize(620, 640)

        self.queue = queue.Queue()
        self.proc = None
        self.running = False
        self.duration = 0.0
        self.config = load_config()

        def cfg_get(key, default):
            return self.config.get(key, default)

        self.audio_var = tk.StringVar(value=cfg_get("audio", ""))
        self.bg_var = tk.StringVar(value=cfg_get("bg", ""))
        self.out_var = tk.StringVar(value=cfg_get("out", ""))
        self.res_var = tk.StringVar(value=cfg_get("res", RESOLUTION_LABELS[1]))
        self.fps_var = tk.StringVar(value=cfg_get("fps", "30"))
        self.color_var = tk.StringVar(value=cfg_get("color", "cyan"))
        self.mode_var = tk.StringVar(value=cfg_get("wave_mode", "Rellenada (cline)"))
        self.position_var = tk.StringVar(value=cfg_get("position", "Centro"))
        self.size_var = tk.StringVar(value=cfg_get("spectrum_size", "Normal (80%)"))
        self.blur_var = tk.BooleanVar(value=cfg_get("blur", False))
        self.preset_var = tk.StringVar(value=cfg_get("preset", "fast"))
        self.crf_var = tk.StringVar(value=cfg_get("crf", "Media (23)"))

        self._build_ui()
        self.root.after(80, self._poll_queue)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        ttk.Label(self.root, text="Archivos",
                  font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w", padx=10)

        files = ttk.Frame(self.root)
        files.grid(row=1, column=0, sticky="ew", padx=10, pady=2)
        files.columnconfigure(1, weight=1)

        ttk.Label(files, text="Audio:").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(files, textvariable=self.audio_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(files, text="Examinar...", command=self._pick_audio).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(files, text="Imagen de fondo:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(files, textvariable=self.bg_var).grid(row=1, column=1, sticky="ew")
        ttk.Button(files, text="Examinar...", command=self._pick_bg).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(files, text="Video de salida:").grid(row=2, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(files, textvariable=self.out_var).grid(row=2, column=1, sticky="ew")
        ttk.Button(files, text="Guardar como...", command=self._pick_out).grid(row=2, column=2, padx=(6, 0))

        ttk.Separator(self.root).grid(row=2, column=0, sticky="ew", padx=10, pady=8)

        ttk.Label(self.root, text="Configuracion del video",
                  font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", padx=10)

        opts = ttk.Frame(self.root)
        opts.grid(row=4, column=0, sticky="ew", padx=10)
        opts.columnconfigure(1, weight=1)

        def add_row(frame, r, label, widget):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
            widget.grid(row=r, column=1, sticky="ew")

        add_row(opts, 0, "Resolucion:", ttk.Combobox(
            opts, textvariable=self.res_var, values=RESOLUTION_LABELS, state="readonly"))
        add_row(opts, 1, "FPS:", ttk.Combobox(
            opts, textvariable=self.fps_var, values=FPS_LEVELS, state="readonly"))
        add_row(opts, 2, "Color de la onda:", ttk.Combobox(
            opts, textvariable=self.color_var, values=WAVE_COLORS, state="readonly"))
        add_row(opts, 3, "Forma de la onda:", ttk.Combobox(
            opts, textvariable=self.mode_var,
            values=[m[0] for m in WAVE_MODES], state="readonly"))
        add_row(opts, 4, "Posicion de la grafica:", ttk.Combobox(
            opts, textvariable=self.position_var,
            values=[p[0] for p in WAVE_POSITIONS], state="readonly"))
        add_row(opts, 5, "Tamano de la grafica:", ttk.Combobox(
            opts, textvariable=self.size_var,
            values=[s[0] for s in SPECTRUM_SIZES], state="readonly"))
        add_row(opts, 6, "Calidad (CRF):", ttk.Combobox(
            opts, textvariable=self.crf_var,
            values=[c[0] for c in CRF_LEVELS], state="readonly"))
        add_row(opts, 7, "Velocidad de codificacion:", ttk.Combobox(
            opts, textvariable=self.preset_var, values=PRESETS, state="readonly"))

        ttk.Checkbutton(opts, text="Desenfocar el fondo (aspecto cinematografico)",
                        variable=self.blur_var).grid(row=8, column=1, sticky="w", pady=4)

        ttk.Separator(self.root).grid(row=5, column=0, sticky="ew", padx=10, pady=8)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.grid(row=6, column=0, sticky="ew", padx=10)
        self.status_var = tk.StringVar(value="Listo.")
        ttk.Label(self.root, textvariable=self.status_var).grid(
            row=7, column=0, sticky="w", padx=10)

        self.log = tk.Text(self.root, height=10, state="disabled",
                           font=("Consolas", 8))
        self.log.grid(row=8, column=0, sticky="nsew", padx=10, pady=4)
        self.root.rowconfigure(8, weight=1)
        self.root.columnconfigure(0, weight=1)

        buttons = ttk.Frame(self.root)
        buttons.grid(row=9, column=0, sticky="ew", padx=10, pady=6)
        self.cancel_btn = ttk.Button(buttons, text="Cancelar", command=self._cancel, state="disabled")
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.generate_btn = ttk.Button(buttons, text="Generar video", command=self._generate)
        self.generate_btn.pack(side="right")

    def _pick_audio(self):
        path = filedialog.askopenfilename(
            title="Seleccionar audio", filetypes=AUDIO_FILTER,
            initialdir=str(Path(__file__).resolve().parent))
        if not path:
            return
        path = Path(path)
        self.audio_var.set(str(path))
        if not self.out_var.get():
            self.out_var.set(str(path.with_name(path.stem + "_video.mp4")))

    def _pick_bg(self):
        path = filedialog.askopenfilename(
            title="Seleccionar imagen de fondo", filetypes=IMAGE_FILTER)
        if path:
            self.bg_var.set(path)

    def _pick_out(self):
        path = filedialog.asksaveasfilename(
            title="Guardar video como", defaultextension=".mp4",
            filetypes=VIDEO_FILTER, initialfile=Path(self.out_var.get()).name)
        if path:
            self.out_var.set(path)

    def _log_line(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        if int(float(self.log.index("end-1c"))) > 400:
            self.log.delete("1.0", "100.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _emit(self, kind, *payload):
        self.queue.put((kind, payload))

    def _fps(self):
        try:
            return int(self.fps_var.get())
        except ValueError:
            return 30

    def _generar_cmd(self):
        bg = Path(self.bg_var.get())
        audio = Path(self.audio_var.get())
        out = Path(self.out_var.get())
        if not (bg.exists() and bg.is_file()):
            raise ValueError("Selecciona una imagen de fondo valida.")
        if not (audio.exists() and audio.is_file()):
            raise ValueError("Selecciona un archivo de audio valido.")
        if not out.name:
            raise ValueError("Indica el nombre del video de salida.")
        W, H = RESOLUTION_MAP[self.res_var.get()]
        size_pct = dict(SPECTRUM_SIZES)[self.size_var.get()]
        wave_mode = dict(WAVE_MODES)[self.mode_var.get()]
        position = dict(WAVE_POSITIONS)[self.position_var.get()]
        crf = dict(CRF_LEVELS)[self.crf_var.get()]
        fps = self._fps()
        if not self.duration:
            try:
                self.duration = audio_duration(audio)
            except Exception:
                raise ValueError("No se pudo leer la duracion del audio.")
        return build_command(bg, audio, out, W, H, fps, size_pct,
                             self.color_var.get(), wave_mode, position,
                             self.blur_var.get(),
                             self.preset_var.get(), crf, self.duration)

    def _generate(self):
        if self.running:
            return
        try:
            cmd = self._generar_cmd()
        except ValueError as exc:
            messagebox.showerror("Falta un archivo", str(exc))
            return
        duration = self.duration
        out = Path(self.out_var.get())
        if out.exists():
            if not messagebox.askyesno(
                    "Sobrescribir", f"El archivo ya existe:\n{out}\n\nDeseas sobrescribirlo?"):
                return

        self.config.update({
            "audio": self.audio_var.get(),
            "bg": self.bg_var.get(),
            "out": self.out_var.get(),
            "res": self.res_var.get(),
            "fps": self.fps_var.get(),
            "color": self.color_var.get(),
            "wave_mode": self.mode_var.get(),
            "position": self.position_var.get(),
            "spectrum_size": self.size_var.get(),
            "blur": self.blur_var.get(),
            "preset": self.preset_var.get(),
            "crf": self.crf_var.get(),
        })
        save_config(self.config)

        self.running = True
        self.generate_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.progress.configure(value=0)
        self.status_var.set("Generando video...")
        self.log_text = ""
        self._log_line(" ".join(cmd))
        threading.Thread(target=self._worker, args=(cmd, duration),
                         daemon=True).start()
        self._poll_queue()

    def _worker(self, cmd, duration):
        self.duration = duration
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            self._emit("done", 1, str(exc))
            return
        self.proc = proc
        threading.Thread(target=self._sink_stderr, args=(proc,),
                         daemon=True).start()
        try:
            last_pct = -1
            for raw in iter(proc.stdout.readline, b""):
                if not raw:
                    break
                m = re.search(rb"out_time_ms=(\d+)", raw)
                if m:
                    secs = int(m.group(1)) / 1_000_000
                    pct = (secs / duration) * 100 if duration else 0
                    if pct > last_pct:
                        last_pct = pct
                        self._emit("progress", min(pct, 100))
                if b"progress=end" in raw:
                    break
        except Exception:
            pass
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
        code = proc.wait()
        self.proc = None
        self._emit("done", code, "")

    def _sink_stderr(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._emit("log", line)
        except Exception:
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:
                pass

    def _cancel(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass

    def _poll_queue(self):
        try:
            while True:
                try:
                    kind, payload = self.queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    self.progress.configure(value=payload[0])
                    self.status_var.set(
                        f"Generando... {payload[0]:.0f}%")
                elif kind == "log":
                    self._log_line(payload[0])
                elif kind == "done":
                    self._finish(payload[0], payload[1])
        except Exception:
            pass
        self.root.after(80, self._poll_queue)

    def _finish(self, code, error):
        self.running = False
        self.generate_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if code == 0:
            self.progress.configure(value=100)
            self.status_var.set("Terminado.")
            out = Path(self.out_var.get())
            if messagebox.askyesno(
                    "Video generado",
                    f"Video creado correctamente:\n{out}\n\nAbrir la carpeta?"):
                subprocess.Popen(["explorer", "/select,", str(out)])
        else:
            self.status_var.set("Error.")
            messagebox.showerror(
                "Error de ffmpeg",
                f"El proceso termino con el codigo {code}." +
                (f"\n{error}" if error else "\nRevisa el registro de la parte inferior."))


def main():
    if not (FFMPEG and FFPROBE):
        messagebox.showerror(
            "Falta ffmpeg",
            "No se encontraron ffmpeg/ffprobe.\n"
            "Instalalo y agregalo al PATH, por ejemplo desde:\n"
            "https://www.gyan.dev/ffmpeg/builds/")
        return
    root = tk.Tk()
    SpectroGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()