"""Native desktop frontend. Tk stays on the main thread; the engine is isolated."""

from __future__ import annotations

import queue
from pathlib import Path

from .desktop import DesktopProcess
from .languages import LANGUAGES


class DesktopWindow:
    def __init__(self, root, campaign="."):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText

        self.root = root
        self.engine = DesktopProcess()
        self.ready = False
        self.microphone = False
        self.recording = False
        self.stopping = False
        self.closing = False
        self.preparing = False
        self.testing_voice = False
        root.title("NPC — Compagnon de jeu")
        root.geometry("1000x800")
        root.minsize(900, 650)
        root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background="#f1f4f8", foreground="#1d2b3a")
        style.configure("Title.TLabel", font=("Helvetica", 24, "bold"))
        style.configure("Subtitle.TLabel", foreground="#566575")
        style.configure("TButton", padding=(12, 7))
        style.configure("TLabelframe.Label", font=("Helvetica", 11, "bold"))

        body = ttk.Frame(root, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Vos personnages prennent la parole", style="Title.TLabel").pack(
            anchor="w")
        ttk.Label(body, text="Une campagne, un personnage, une conversation locale.",
                  style="Subtitle.TLabel").pack(anchor="w", pady=(4, 10))

        campaign_frame = ttk.LabelFrame(body, text="Campagne", padding=12)
        campaign_frame.pack(fill="x")
        self.path = tk.StringVar(value=str(Path(campaign).expanduser().resolve()))
        self.path_entry = ttk.Entry(campaign_frame, textvariable=self.path)
        self.path_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(campaign_frame, text="Parcourir…", command=self.browse)
        self.browse_button.pack(side="left", padx=(8, 4))
        self.new_button = ttk.Button(campaign_frame, text="Créer…", command=self.create)
        self.new_button.pack(side="left")

        languages = ttk.LabelFrame(body, text="Langue des personnages", padding=8)
        languages.pack(fill="x", pady=(12, 0))
        self.language_target = tk.StringVar(value="Campagne (par défaut)")
        self.language_target_picker = ttk.Combobox(
            languages, textvariable=self.language_target, state="readonly", width=28)
        self.language_target_picker.pack(side="left", padx=(0, 8))
        self.language_target_picker.bind("<<ComboboxSelected>>", self.refresh_language)
        self.language = tk.StringVar()
        self.language_picker = ttk.Combobox(
            languages, textvariable=self.language, state="readonly", width=29)
        self.language_picker.pack(side="left")
        self.language_picker.bind("<<ComboboxSelected>>", self.change_language)
        ttk.Label(languages, text="Vocabulaire régional ; accent selon la voix.",
                  style="Subtitle.TLabel", wraplength=170).pack(side="left", padx=(10, 0))
        self.path.trace_add("write", lambda *args: self.refresh_languages())
        self.path_entry.bind("<FocusOut>", lambda event: self.refresh_languages())

        voices = ttk.LabelFrame(body, text="Voix — même personnage sélectionné ci-dessus", padding=10)
        voices.pack(fill="x", pady=(10, 0))
        self.tts_backend = tk.StringVar()
        self.backend_picker = ttk.Combobox(voices, textvariable=self.tts_backend,
                                          state="readonly", width=17,
                                          values=("Piper", "Pocket TTS"))
        self.backend_picker.pack(side="left", padx=(0, 8))
        self.backend_picker.bind("<<ComboboxSelected>>", self.change_backend)
        self.voice = tk.StringVar()
        self.voice_picker = ttk.Combobox(voices, textvariable=self.voice, width=30)
        self.voice_picker.pack(side="left", fill="x", expand=True)
        self.voice_picker.bind("<<ComboboxSelected>>", self.change_voice)
        self.voice_picker.bind("<Return>", self.change_voice)
        self.voice_save_button = ttk.Button(voices, text="Enregistrer", command=self.change_voice)
        self.voice_save_button.pack(side="left", padx=8)
        self.voice_test_button = ttk.Button(voices, text="Écouter cette voix", command=self.test_voice)
        self.voice_test_button.pack(side="left")
        self.refresh_languages()

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=8)
        self.prepare_button = ttk.Button(actions, text="Préparer les modèles", command=self.prepare)
        self.prepare_button.pack(side="left")
        style.configure("Accent.TButton", background="#2458cc", foreground="white")
        style.map("Accent.TButton", background=[("disabled", "#dde3eb"),
                                                ("active", "#1c46a4")],
                  foreground=[("disabled", "#657385")])
        self.start_button = ttk.Button(actions, text="Démarrer", command=self.start,
                                       style="Accent.TButton")
        self.start_button.pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="Terminer et mémoriser", command=self.stop)
        self.stop_button.pack(side="left")
        self.force_button = ttk.Button(actions, text="Forcer l’arrêt…", command=self.force_stop)
        self.force_button.pack(side="left", padx=8)
        self.use_microphone = tk.BooleanVar(value=True)
        self.mic_option = ttk.Checkbutton(actions, text="Activer le micro",
                                         variable=self.use_microphone)
        self.mic_option.pack(side="right")

        self.status = tk.StringVar(value="Prêt à démarrer")
        ttk.Label(body, textvariable=self.status, style="Subtitle.TLabel").pack(anchor="w")
        self.log = ScrolledText(body, wrap="word", height=3, state="disabled",
                                font=("Helvetica", 12),
                                background="#f7f9fc", foreground="#1d2b3a",
                                relief="flat", padx=14, pady=12)
        self.log.pack(fill="both", expand=True, pady=(6, 8))

        composer = ttk.LabelFrame(body, text="Conversation", padding=12)
        composer.pack(side="bottom", fill="x", before=self.log)
        self.mode = tk.StringVar(value="Parole du joueur")
        self.mode_picker = ttk.Combobox(composer, textvariable=self.mode, state="readonly",
                                        values=("Parole du joueur", "Note du MJ", "Commande"),
                                        width=19)
        self.mode_picker.grid(row=0, column=0, padx=(0, 8))
        self.text = tk.StringVar()
        self.entry = ttk.Entry(composer, textvariable=self.text)
        self.entry.grid(row=0, column=1, sticky="ew")
        self.entry.bind("<Return>", lambda event: self.send_text())
        self.send_button = ttk.Button(composer, text="Envoyer", command=self.send_text)
        self.send_button.grid(row=0, column=2, padx=(8, 0))
        self.record_button = ttk.Button(composer, text="Enregistrer", command=self.toggle_recording)
        self.record_button.grid(row=0, column=3, padx=(8, 0))
        composer.columnconfigure(1, weight=1)
        shortcuts = ttk.Frame(composer)
        shortcuts.grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.shortcuts = []
        for label, command in (("Personnages", "/npc"), ("Secrets", "/secrets"),
                               ("Autoriser", "/yes"), ("Refuser", "/no"),
                               ("Plus tard", "/later"), ("État", "/status")):
            button = ttk.Button(shortcuts, text=label,
                                command=lambda line=command: self.send_line(line))
            button.pack(side="left", padx=(0, 4))
            self.shortcuts.append(button)
        ttk.Label(body, text="Ollama doit être installé et lancé. Choisissez la langue avant la session. "
                  "Micro : cliquez pour enregistrer, puis pour envoyer.",
                  style="Subtitle.TLabel", wraplength=820).pack(
                      side="bottom", anchor="w", pady=(10, 0), before=composer)
        self.append("Choisissez une campagne ou créez-en une, puis préparez les modèles "
                    "avant la première session. Les téléchargements peuvent prendre plusieurs minutes.")
        self.update_controls()
        root.after(80, self.poll)

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n\n")
        # Keep long sessions responsive; full transcripts are saved by NPCApp.
        if int(self.log.index("end-1c").split(".")[0]) > 4000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def update_controls(self):
        busy = self.engine.process is not None
        live = self.ready and not self.stopping
        for widget in (self.path_entry, self.browse_button, self.new_button,
                       self.prepare_button, self.start_button, self.mic_option):
            widget.configure(state="disabled" if busy else "normal")
        for picker in (self.language_target_picker, self.language_picker):
            picker.configure(state="disabled" if busy else "readonly")
        self.backend_picker.configure(state="disabled" if busy else "readonly")
        self.voice_picker.configure(state="disabled" if busy else
                                    "readonly" if self.tts_backend.get() == "Pocket TTS" else "normal")
        for widget in (self.voice_save_button, self.voice_test_button):
            widget.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if live else "disabled")
        self.force_button.configure(state="normal" if busy and
                                    (self.stopping or self.preparing or not self.ready)
                                    else "disabled")
        for widget in (self.entry, self.send_button, *self.shortcuts):
            widget.configure(state="normal" if live else "disabled")
        self.mode_picker.configure(state="readonly" if live else "disabled")
        self.record_button.configure(state="normal" if live and self.microphone else "disabled",
                                     text="Envoyer l’enregistrement" if self.recording
                                     else "Enregistrer")

    def refresh_languages(self):
        from .roster import discover_character_files, read_display_name

        self.language_targets = {"Campagne (par défaut)": None}
        path = Path(self.path.get()).expanduser()
        try:
            for ref in discover_character_files(path):
                name = read_display_name(ref.path.read_text(encoding="utf-8"), ref.stem)
                self.language_targets[f"{name} ({ref.stem})"] = ref.stem
        except OSError:
            pass  # path may still be being typed
        self.language_target_picker.configure(values=tuple(self.language_targets))
        self.language_target.set("Campagne (par défaut)")
        self.refresh_language()

    def refresh_language(self, event=None):
        from .config import ConfigError, load_config

        stem = self.language_targets.get(self.language_target.get())
        choices = [label for label, _, _ in LANGUAGES.values()]
        if stem is not None:
            choices.insert(0, "Suivre la campagne")
        self.language_picker.configure(values=choices)
        try:
            config = load_config(Path(self.path.get()))
            code = config.character_languages.get(stem) if stem else config.language
            self.language.set(LANGUAGES[code][0] if code else "Suivre la campagne")
        except (ConfigError, OSError):
            self.language.set("")
        if hasattr(self, "voice_picker"):
            self.refresh_voice()

    def refresh_voice(self):
        from .config import ConfigError, load_config
        from .pocket import POCKET_VOICES

        try:
            config = load_config(Path(self.path.get()))
            stem = self.language_targets.get(self.language_target.get())
            pocket = config.tts.backend == "pocket"
            self.tts_backend.set("Pocket TTS" if pocket else "Piper")
            values = list(POCKET_VOICES) if pocket else sorted(
                {path.stem for path in Path(config.tts.voices_dir).expanduser().glob("*.onnx")}
                | {config.tts.voice} | set(config.tts.voices.values()))
            if stem is not None:
                values.insert(0, "Suivre la campagne")
            self.voice_picker.configure(values=values, state="readonly" if pocket else "normal")
            self.voice.set(config.tts.voices.get(stem, "Suivre la campagne") if stem
                           else config.tts.voice)
        except (ConfigError, OSError):
            self.voice.set("")

    def change_backend(self, event=None):
        from tkinter import messagebox

        from .config import ConfigError, save_tts

        if self.engine.process is not None:
            return
        try:
            save_tts(Path(self.path.get()), backend="pocket" if self.tts_backend.get() == "Pocket TTS"
                     else "piper")
            self.refresh_voice()
            self.append(f"Moteur vocal de la campagne : {self.tts_backend.get()}. "
                        "Préparez les modèles au premier usage. Pocket TTS précharge "
                        "les voix sur CPU au démarrage.")
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showerror("Voix non enregistrée", str(exc), parent=self.root)
            self.refresh_voice()

    def change_voice(self, event=None):
        from tkinter import messagebox

        from .config import ConfigError, save_tts

        if self.engine.process is not None:
            return False
        try:
            value = "inherit" if self.voice.get() == "Suivre la campagne" else self.voice.get()
            save_tts(Path(self.path.get()), voice=value,
                     stem=self.language_targets.get(self.language_target.get()))
            self.append(f"Voix enregistrée — {self.language_target.get()} : {self.voice.get()}.")
            return True
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showerror("Voix non enregistrée", str(exc), parent=self.root)
            self.refresh_voice()
            return False

    def test_voice(self):
        from tkinter import messagebox

        from .config import load_config

        if not self.change_voice():
            return
        try:
            path = Path(self.path.get())
            config = load_config(path)
            stem = self.language_targets.get(self.language_target.get())
            voice = config.tts.voices.get(stem, config.tts.voice)
            self.engine.start(path, microphone=False, test_voice=voice)
            self.testing_voice = True
            self.status.set("Préparation et écoute de la voix…")
            self.append("Écoute sans LLM ni micro. Le premier chargement peut prendre du temps.")
            self.update_controls()
        except Exception as exc:
            messagebox.showerror("Écoute impossible", str(exc), parent=self.root)

    def change_language(self, event=None):
        from tkinter import messagebox

        from .config import ConfigError, save_language

        if self.engine.process is not None:
            return
        code = next((key for key, value in LANGUAGES.items()
                     if value[0] == self.language.get()), "inherit")
        stem = self.language_targets.get(self.language_target.get())
        path = Path(self.path.get()).expanduser()
        try:
            if not path.is_dir():
                raise ValueError("Choisissez d’abord un dossier de campagne existant.")
            save_language(path, code, stem)
        except (ConfigError, OSError, ValueError) as exc:
            messagebox.showerror("Langue non enregistrée", str(exc), parent=self.root)
            self.refresh_language()
            return
        self.append(f"Langue enregistrée — {self.language_target.get()} : "
                    f"{self.language.get()}. Préparez les modèles avant de démarrer "
                    "pour télécharger les voix manquantes.")
        self.refresh_voice()

    def browse(self):
        from tkinter import filedialog

        path = filedialog.askdirectory(parent=self.root, title="Choisir une campagne")
        if path:
            self.path.set(path)

    def create(self):
        from tkinter import filedialog, messagebox

        from .cli import init_campaign

        path = filedialog.askdirectory(parent=self.root, title="Créer un dossier de campagne",
                                       mustexist=False)
        if path:
            try:
                created = init_campaign(Path(path))
                self.path.set(path)
                self.append(f"Campagne : {path}\n{len(created)} fichier(s) créé(s). "
                            "Les fichiers existants sont conservés.")
            except OSError as exc:
                messagebox.showerror("Création impossible", str(exc), parent=self.root)

    def launch(self, *, prepare=False):
        from tkinter import messagebox

        from .config import load_config
        from .roster import discover_character_files

        path = Path(self.path.get()).expanduser()
        try:
            if not path.is_dir() or not discover_character_files(path):
                raise ValueError("Choisissez une campagne contenant character.md ou characters/*.md.")
            load_config(path)  # report invalid TOML before launching a process
            self.engine.start(path, prepare=prepare, microphone=self.use_microphone.get())
        except Exception as exc:
            messagebox.showerror("Démarrage impossible", str(exc), parent=self.root)
            return
        self.preparing = prepare
        self.status.set("Préparation et téléchargement…" if prepare else "Démarrage du moteur…")
        self.append(f"{'Préparation' if prepare else 'Démarrage'} : {path}")
        self.update_controls()

    def prepare(self):
        self.launch(prepare=True)

    def start(self):
        self.launch()

    def send(self, action, **values):
        try:
            self.engine.send(action, **values)
            return True
        except RuntimeError as exc:
            self.append(str(exc))
            return False

    def send_line(self, line):
        if self.ready and not self.stopping and self.send("line", text=line):
            self.append(f"Vous : {line}")
            if line.strip() in ("/end", "/quit"):
                self.mark_stopping()

    def send_text(self):
        text = self.text.get().strip()
        if not text or not self.ready or self.stopping:
            return
        if self.mode.get() == "Parole du joueur":
            self.send_line("/say " + text)
        elif self.mode.get() == "Note du MJ" and text.startswith("/"):
            self.append("Pour envoyer une commande /…, sélectionnez le mode Commande.")
            return
        else:
            self.send_line(text)
        self.text.set("")

    def toggle_recording(self):
        if not self.ready or self.stopping or not self.microphone:
            return
        if self.send("release" if self.recording else "record"):
            self.recording = not self.recording
            self.update_controls()

    def mark_stopping(self):
        self.stopping = True
        self.recording = False
        self.status.set("Fin de session et sauvegarde…")
        self.update_controls()

    def stop(self):
        if self.ready and not self.stopping and self.send("shutdown", summarize=True):
            self.mark_stopping()

    def force_stop(self):
        from tkinter import messagebox

        if messagebox.askyesno("Forcer l’arrêt", "Interrompre le moteur maintenant ? "
                               "Le résumé final ne sera pas garanti ; les modèles en cours "
                               "de téléchargement pourront rester incomplets.", parent=self.root):
            self.engine.terminate()
            return True
        return False

    def close(self):
        if self.engine.process is None:
            self.root.destroy()
        elif self.preparing or self.testing_voice or not self.ready:
            # Startup/model loading may not yet read input; allow cancellation.
            if self.force_stop():
                self.closing = True
        else:
            self.closing = True
            self.stop()

    def poll(self):
        for _ in range(150):
            try:
                message = self.engine.messages.get_nowait()
            except queue.Empty:
                break
            kind = message.get("kind")
            if kind in ("log", "error"):
                if message.get("text"):
                    self.append(message["text"])
            elif kind == "ready":
                self.ready = True
                self.microphone = message["microphone"]
                self.status.set(f"Session active — {message['npc']}")
                if not self.microphone:
                    self.append("Saisie disponible. Pour activer le micro : préparez les modèles, "
                                "vérifiez les autorisations système et redémarrez la session.")
                self.entry.focus_set()
            elif kind == "state" and not self.stopping:
                state = message["state"]
                self.recording = state == "recording"
                self.status.set({"idle": "À votre écoute", "recording": "Enregistrement…",
                                 "processing": "Le personnage réfléchit…",
                                 "speaking": "Le personnage parle…"}.get(state, state))
            elif kind == "stopping":
                self.mark_stopping()
            elif kind == "exit":
                self.engine.acknowledge_exit()
                self.ready = self.microphone = self.recording = self.stopping = False
                self.status.set("Écoute terminée" if self.testing_voice and message["code"] == 0
                                else "Préparation terminée" if self.preparing and message["code"] == 0
                                else "Session terminée" if message["code"] == 0
                                else "Arrêt du moteur — consultez les messages ci-dessus")
                self.preparing = False
                self.testing_voice = False
                if self.closing:
                    self.root.destroy()
                    return
            self.update_controls()
        self.root.after(80, self.poll)


def main(campaign=".") -> int:
    try:
        import tkinter as tk
    except ImportError:
        import sys

        print("Tkinter est requis. Utilisez Python depuis python.org avec Tcl/Tk, "
              "ou installez python3-tk sous Linux.", file=sys.stderr)
        return 1
    root = tk.Tk()
    DesktopWindow(root, campaign)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
