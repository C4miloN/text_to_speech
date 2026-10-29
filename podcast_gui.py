import asyncio
import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

from podcast_audio import build_podcast, fetch_spanish_voices, build_voice_pools
from podcast_tts import VOICE_POOLS, shorten_output_path

AUDIO_FILTER = [
    ("Archivos de audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac *.wma"),
    ("Todos los archivos", "*.*"),
]

CONFIG_PATH = Path(__file__).resolve().with_name("config.json")


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


class PodcastGUI:
    def __init__(self, root):
        self.root = root
        root.title("Generador de Podcast TTS")
        root.configure(padx=12, pady=12)

        self.config = load_config()

        def cfg_get(*keys, default):
            value = self.config
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return value

        self.doc_var = tk.StringVar(value=cfg_get("paths", "docx", default=""))
        self.intro_var = tk.StringVar(value=cfg_get("paths", "intro", default=""))
        self.final_var = tk.StringVar(value=cfg_get("paths", "outro", default=""))
        self.bg_var = tk.StringVar(value=cfg_get("paths", "bg", default=""))
        self.out_var = tk.StringVar(value=cfg_get("paths", "out", default=""))
        self.loop_var = tk.BooleanVar(value=cfg_get("bg_loop", default=True))

        self.vol_podcast = tk.IntVar(value=cfg_get("volumes", "podcast", default=100))
        self.vol_intro = tk.IntVar(value=cfg_get("volumes", "intro", default=100))
        self.vol_final = tk.IntVar(value=cfg_get("volumes", "final", default=100))
        self.vol_bg = tk.IntVar(value=cfg_get("volumes", "bg", default=10))

        self.pause = tk.DoubleVar(value=cfg_get("pause", default=0.5))
        self.rate = tk.StringVar(value=cfg_get("rate", default="+0%"))
        self.fade = tk.DoubleVar(value=cfg_get("fade", default=2.0))

        self.sil_threshold = tk.DoubleVar(
            value=cfg_get("silence_threshold", default=0.5))
        self.sil_reduction = tk.IntVar(
            value=cfg_get("silence_reduction", default=50))

        self.spanish_voices = []
        self.enabled_voices = set(cfg_get("enabled_voices", default=[]))
        self.voices_loaded = False

        self.msg_queue = queue.Queue()
        self.busy = False

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self._build_ui()
        self._poll_queue()
        self._start_voice_load()

    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}

        title = ttk.Label(self.root, text="Generador de Podcast TTS",
                          font=("Segoe UI", 14, "bold"))
        title.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        self._add_file_row(1, "Guion DOCX:", self.doc_var,
                           [("Documentos Word", "*.docx"), ("Todos", "*.*")])
        self._add_file_row(2, "Audio inicial:", self.intro_var, AUDIO_FILTER)
        self._add_file_row(3, "Audio final:", self.final_var, AUDIO_FILTER)
        self._add_file_row(4, "Música de fondo:", self.bg_var, AUDIO_FILTER)

        loop_chk = ttk.Checkbutton(self.root, text="Reproducir fondo en bucle",
                                   variable=self.loop_var,
                                   command=self._save_config)
        loop_chk.grid(row=5, column=1, columnspan=3, sticky="w", **pad)

        self._add_file_row(6, "Salida MP3:", self.out_var,
                           [("MP3", "*.mp3")], save=True)

        ttk.Separator(self.root).grid(row=7, column=0, columnspan=4,
                                      sticky="ew", **pad)

        ttk.Label(self.root, text="Volúmenes (%)", font=("Segoe UI", 10, "bold")) \
            .grid(row=8, column=0, columnspan=4, sticky="w", **pad)

        self._add_volume_row(9, "Voz / Podcast", self.vol_podcast)
        self._add_volume_row(10, "Audio inicial", self.vol_intro)
        self._add_volume_row(11, "Audio final", self.vol_final)
        self._add_volume_row(12, "Fondo", self.vol_bg)

        ttk.Separator(self.root).grid(row=13, column=0, columnspan=4,
                                      sticky="ew", **pad)

        ttk.Label(self.root, text="Pausa entre intervenciones (s):") \
            .grid(row=14, column=0, sticky="e", **pad)
        ttk.Entry(self.root, textvariable=self.pause, width=8) \
            .grid(row=14, column=1, sticky="w", **pad)
        ttk.Label(self.root, text="Velocidad de voz:") \
            .grid(row=14, column=2, sticky="e", **pad)
        ttk.Entry(self.root, textvariable=self.rate, width=8) \
            .grid(row=14, column=3, sticky="w", **pad)

        ttk.Label(self.root, text="Fundido inicio/fin (s):") \
            .grid(row=15, column=0, sticky="e", **pad)
        ttk.Entry(self.root, textvariable=self.fade, width=8) \
            .grid(row=15, column=1, sticky="w", **pad)

        ttk.Label(self.root, text="Truncar silencios > (s):") \
            .grid(row=16, column=0, sticky="e", **pad)
        ttk.Entry(self.root, textvariable=self.sil_threshold, width=8) \
            .grid(row=16, column=1, sticky="w", **pad)
        ttk.Label(self.root, text="Reducción (%):") \
            .grid(row=16, column=2, sticky="e", **pad)
        ttk.Entry(self.root, textvariable=self.sil_reduction, width=8) \
            .grid(row=16, column=3, sticky="w", **pad)

        ttk.Separator(self.root).grid(row=17, column=0, columnspan=4,
                                      sticky="ew", **pad)

        ttk.Label(self.root, text="Voces disponibles:") \
            .grid(row=18, column=0, sticky="e", **pad)
        self.voices_btn = ttk.Button(self.root, text="Seleccionar voces...",
                                     command=self.open_voice_dialog)
        self.voices_btn.grid(row=18, column=1, columnspan=2, sticky="w", **pad)
        self.voices_info = ttk.Label(self.root, text="Cargando...", anchor="w")
        self.voices_info.grid(row=18, column=3, sticky="w", **pad)

        self.btn = ttk.Button(self.root, text="Generar Podcast",
                              command=self.on_generate)
        self.btn.grid(row=19, column=0, columnspan=4, sticky="ew",
                      pady=(12, 4))

        self.status = ttk.Label(self.root, text="Listo.", anchor="w")
        self.status.grid(row=20, column=0, columnspan=4, sticky="ew", **pad)

        log_frame = ttk.Frame(self.root)
        log_frame.grid(row=21, column=0, columnspan=4, sticky="nsew", **pad)
        self.log = tk.Text(log_frame, height=7, state="disabled")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.root.columnconfigure(0, weight=1)
        self.root.columnconfigure(1, weight=1)
        self.root.columnconfigure(2, weight=1)
        self.root.columnconfigure(3, weight=1)
        self.root.rowconfigure(21, weight=1)

    def _add_file_row(self, row, label, var, filetypes, save=False):
        pad = {"padx": 6, "pady": 3}
        ttk.Label(self.root, text=label).grid(row=row, column=0, sticky="w", **pad)
        ttk.Entry(self.root, textvariable=var).grid(
            row=row, column=1, columnspan=2, sticky="ew", **pad)

        def browse():
            if save:
                path = filedialog.asksaveasfilename(
                    title="Guardar MP3",
                    defaultextension=".mp3",
                    filetypes=filetypes,
                    initialfile="podcast.mp3",
                )
            else:
                path = filedialog.askopenfilename(
                    title="Seleccionar archivo", filetypes=filetypes)
            if path:
                var.set(path)
                if save:
                    return
                if path.lower().endswith(".docx") and not self.out_var.get():
                    suggested, _err = shorten_output_path(
                        Path(path).with_suffix(".mp3"))
                    if suggested:
                        self.out_var.set(str(suggested))

        ttk.Button(self.root, text="Examinar...", command=browse).grid(
            row=row, column=3, sticky="w", **pad)

    def _add_volume_row(self, row, label, var):
        pad = {"padx": 6, "pady": 2}
        ttk.Label(self.root, text=label).grid(row=row, column=0, sticky="w", **pad)

        svar = tk.StringVar(value=str(var.get()))
        scale = tk.Scale(
            self.root, from_=0, to=200, orient="horizontal",
            variable=var, showvalue=False, length=210,
            command=lambda _v: svar.set(str(int(float(_v)))),
        )
        scale.grid(row=row, column=1, columnspan=2, sticky="ew", **pad)
        entry = ttk.Entry(self.root, textvariable=svar, width=6, justify="right")
        entry.grid(row=row, column=3, sticky="w", **pad)

        def sync(*_):
            try:
                value = int(float(svar.get()))
            except ValueError:
                return
            value = max(0, min(200, value))
            if var.get() != value:
                var.set(value)

        svar.trace_add("write", sync)

    def _start_voice_load(self):
        def job():
            try:
                voices = asyncio.run(fetch_spanish_voices())
                self.msg_queue.put(("voices", voices))
            except Exception as exc:
                self.msg_queue.put(("voices_error", str(exc)))

        threading.Thread(target=job, daemon=True).start()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "voices":
                    self.spanish_voices = payload
                    self.voices_loaded = True
                    if not self.enabled_voices:
                        self.enabled_voices = {v["short"] for v in payload}
                    self._update_voices_info()
                    self._append_log(
                        f"{len(payload)} voces de español disponibles cargadas.")
                elif kind == "voices_error":
                    self.voices_loaded = True
                    self._append_log(f"Aviso: no se pudieron cargar las voces ({payload})")
                    self.voices_info.config(text="No disponibles")
                elif kind == "done":
                    ok, msg = payload
                    self.busy = False
                    self.btn.config(state="normal")
                    self.status.config(text=msg)
                    self._append_log("\n" + msg)
                    if ok:
                        messagebox.showinfo("Completado", msg)
                    else:
                        messagebox.showerror("Error", msg)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _update_voices_info(self):
        if self.spanish_voices:
            self.voices_info.config(
                text=f"{len(self.enabled_voices)}/{len(self.spanish_voices)} habilitadas")

    def _append_log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _collect_config(self):
        return {
            "paths": {
                "docx": self.doc_var.get().strip(),
                "intro": self.intro_var.get().strip(),
                "outro": self.final_var.get().strip(),
                "bg": self.bg_var.get().strip(),
                "out": self.out_var.get().strip(),
            },
            "volumes": {
                "podcast": self.vol_podcast.get(),
                "intro": self.vol_intro.get(),
                "final": self.vol_final.get(),
                "bg": self.vol_bg.get(),
            },
            "bg_loop": self.loop_var.get(),
            "pause": self.pause.get(),
            "rate": self.rate.get().strip(),
            "fade": self.fade.get(),
            "silence_threshold": self.sil_threshold.get(),
            "silence_reduction": self.sil_reduction.get(),
            "enabled_voices": sorted(self.enabled_voices),
        }

    def _save_config(self):
        save_config(self._collect_config())

    def on_close(self):
        self._save_config()
        self.root.destroy()

    def _build_voice_pools(self):
        if self.spanish_voices:
            pools = build_voice_pools(self.spanish_voices, self.enabled_voices)
            if any(pools.values()):
                return pools
        return {gender: list(pool) for gender, pool in VOICE_POOLS.items()}

    def open_voice_dialog(self):
        if not self.spanish_voices:
            messagebox.showinfo(
                "Cargando voces",
                "La lista de voces se está cargando. Inténtalo de nuevo en unos segundos.",
            )
            return

        win = tk.Toplevel(self.root)
        win.title("Voces disponibles (español)")
        win.geometry("560x500")
        win.transient(self.root)

        top = ttk.Frame(win)
        top.pack(fill="x", padx=10, pady=(10, 6))
        hint = ttk.Label(
            top, text="Marca las voces que quieras permitir en el podcast.",
            anchor="w")
        hint.pack(fill="x")
        buttons = ttk.Frame(top)
        buttons.pack(fill="x", pady=(6, 0))

        header = ttk.Frame(win)
        header.pack(fill="x", padx=10)
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True, padx=10, pady=6)
        footer = ttk.Frame(win)
        footer.pack(fill="x", padx=10, pady=(0, 10))

        self._dialog_count = tk.StringVar()
        ttk.Label(header, textvariable=self._dialog_count).pack(side="left")

        canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>",
            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_mousewheel(event):
            try:
                canvas.yview_scroll(int(-event.delta / 120), "units")
            except tk.TclError:
                pass

        canvas.bind("<MouseWheel>", on_mousewheel)
        inner.bind("<MouseWheel>", on_mousewheel)
        canvas.bind("<Button-4>", lambda _e: canvas.yview_scroll(-1, "units"))
        canvas.bind("<Button-5>", lambda _e: canvas.yview_scroll(1, "units"))

        ordered = sorted(
            self.spanish_voices,
            key=lambda v: (v["gender"] != "female", v["locale"], v["short"]),
        )
        boxes = []
        for voice in ordered:
            var = tk.BooleanVar(value=voice["short"] in self.enabled_voices)
            short, label = voice["short"], voice["label"]
            gender_text = "Mujer" if voice["gender"] == "female" else "Hombre"
            chk = ttk.Checkbutton(
                inner,
                text=f"{short}  —  {gender_text}",
                variable=var,
                command=self._update_dialog_count,
            )
            chk.pack(anchor="w")
            sub = ttk.Label(inner, text=f"    {label}", foreground="#555555")
            sub.pack(anchor="w")
            boxes.append((var, short))
        self._dialog_boxes = boxes

        def set_all(value):
            for var, _short in boxes:
                var.set(value)
            self._update_dialog_count()

        def set_habits():
            habitual = {
                s for pool in VOICE_POOLS.values() for s in pool
            }
            for var, short in boxes:
                var.set(short in habitual)
            self._update_dialog_count()

        ttk.Button(buttons, text="Todas", command=lambda: set_all(True)) \
            .pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="Ninguna", command=lambda: set_all(False)) \
            .pack(side="left", padx=(0, 4))
        ttk.Button(buttons, text="Habituales",
                   command=set_habits).pack(side="left")
        ttk.Button(buttons, text="Cancelar", command=win.destroy) \
            .pack(side="right")
        ttk.Button(buttons, text="Guardar",
                   command=lambda: self._apply_voices(boxes, win)) \
            .pack(side="right", padx=(0, 4))

        self._update_dialog_count()
        win.grab_set()

    def _update_dialog_count(self):
        count = sum(1 for var, _short in getattr(self, "_dialog_boxes", []))
        self._dialog_count.set(f"{count} voces habilitadas")

    def _apply_voices(self, boxes, win):
        self.enabled_voices = {short for var, short in boxes if var.get()}
        self._update_voices_info()
        self._save_config()
        self._append_log(
            f"Voces habilitadas: {len(self.enabled_voices)} de {len(self.spanish_voices)}")
        win.destroy()

    def on_generate(self):
        if self.busy:
            return
        if not self.doc_var.get():
            messagebox.showwarning("Falta el guion", "Selecciona un archivo .docx")
            return

        self._save_config()

        out_raw = self.out_var.get().strip() or str(
            Path(self.doc_var.get()).with_suffix(".mp3"))
        out_path, err = shorten_output_path(out_raw)
        if err:
            messagebox.showerror("Ruta de salida demasiado larga", err)
            return
        out = str(out_path)
        if out != out_raw:
            self.out_var.set(out)
            self._append_log(
                f"Nombre de salida acortado por el límite de Windows: {out_path.name}")

        cfg = {
            "docx": self.doc_var.get(),
            "intro": self.intro_var.get().strip(),
            "outro": self.final_var.get().strip(),
            "bg": self.bg_var.get().strip(),
            "bg_loop": self.loop_var.get(),
            "out": out,
            "pause": max(0.0, self.pause.get()),
            "rate": self.rate.get().strip() or "+0%",
            "fade": max(0.0, self.fade.get()),
            "silence_threshold": max(0.0, self.sil_threshold.get()),
            "silence_reduction": max(0, self.sil_reduction.get()),
            "vol_podcast": self.vol_podcast.get() / 100.0,
            "vol_intro": self.vol_intro.get() / 100.0,
            "vol_final": self.vol_final.get() / 100.0,
            "vol_bg": self.vol_bg.get() / 100.0,
            "voice_pools": self._build_voice_pools(),
        }

        self.busy = True
        self.btn.config(state="disabled")
        self.status.config(text="Generando...")
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

        def worker():
            result = build_podcast(cfg, lambda m: self.msg_queue.put(("log", m)))
            self.msg_queue.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()


def main():
    root = tk.Tk()
    root.minsize(600, 760)
    PodcastGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()