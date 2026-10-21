"""Pocket TTS French demo: native streaming on CPU, local WAV and timing reports."""

from __future__ import annotations

import argparse
import json
import os
import queue
import threading
import traceback

from engine import EXAMPLE, ROOT, VOICES, Engine, Settings


def window():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    root.title("Pocket TTS — Français en streaming local")
    root.geometry("920x720")
    root.minsize(880, 680)
    body = ttk.Frame(root, padding=20)
    body.pack(fill="both", expand=True)
    ttk.Label(body, text="Pocket TTS : parler sans attendre la fin", font=("Segoe UI", 19, "bold")).pack(anchor="w")
    ttk.Label(body, text="Français · génération sur CPU · lecture des blocs audio dès leur disponibilité").pack(anchor="w", pady=(5, 12))
    profile, voice, seed = tk.StringVar(value="Rapide — 6 couches"), tk.StringVar(value="estelle"), tk.StringVar(value="42")
    reference, play = tk.StringVar(), tk.BooleanVar(value=True)
    row = ttk.Frame(body)
    row.pack(fill="x")
    ttk.Label(row, text="Profil").pack(side="left")
    ttk.Combobox(row, textvariable=profile, state="readonly", width=26,
                 values=("Rapide — 6 couches", "Qualité — 24 couches")).pack(side="left", padx=(8, 18))
    ttk.Label(row, text="Voix").pack(side="left")
    ttk.Combobox(row, textvariable=voice, values=VOICES, state="readonly", width=15).pack(side="left", padx=8)
    ttk.Label(row, text="Graine").pack(side="left", padx=(12, 0))
    ttk.Entry(row, textvariable=seed, width=9).pack(side="left", padx=8)
    ttk.Label(body, text="Estelle utilise une référence française. Les autres voix peuvent avoir un accent différent.",
              wraplength=860).pack(anchor="w", pady=8)
    refs = ttk.LabelFrame(body, text="Facultatif : votre extrait vocal, prioritaire sur la voix sélectionnée", padding=10)
    refs.pack(fill="x", pady=(0, 12))
    ttk.Entry(refs, textvariable=reference).pack(side="left", fill="x", expand=True)

    def browse():
        path = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.flac *.mp3"), ("Tous", "*.*")])
        if path:
            reference.set(path)

    ttk.Button(refs, text="Parcourir…", command=browse).pack(side="left", padx=6)
    ttk.Button(refs, text="Effacer", command=lambda: reference.set("")).pack(side="left")
    text = tk.Text(body, height=5, wrap="word", font=("Segoe UI", 12))
    text.insert("1.0", EXAMPLE)
    text.pack(fill="x")
    ttk.Checkbutton(body, text="Écouter pendant la génération", variable=play).pack(anchor="w", pady=8)
    actions = ttk.Frame(body)
    actions.pack(fill="x", pady=(0, 8))
    status = tk.StringVar(value="Commencez avec Estelle et le profil rapide, puis comparez les essais à chaud.")
    ttk.Label(body, textvariable=status, wraplength=860).pack(anchor="w", pady=8)
    table = ttk.Treeview(body, columns=("ready", "sent", "total", "audio", "rtf", "gaps"), show="headings", height=5)
    for key, label in (("ready", "Premier bloc"), ("sent", "Envoi audio"), ("total", "Génération"),
                       ("audio", "Durée audio"), ("rtf", "Temps / audio"), ("gaps", "Sous-alimentations")):
        table.heading(key, text=label)
        table.column(key, width=135, anchor="center")
    table.pack(fill="both", expand=True)
    ttk.Label(body, text="Délais hors chargement et préparation de voix. L'envoi à PortAudio ne mesure pas le son "
              "physiquement entendu. Temps / audio < 1 : génération plus rapide que la lecture.",
              wraplength=860).pack(anchor="w", pady=8)
    log = ScrolledText(body, height=5, state="disabled", wrap="word")
    log.pack(fill="x")
    engine, mailbox, stop, busy, last = Engine(), queue.Queue(), threading.Event(), [False], [None]
    buttons = []

    def launch(load_only=False, repeats=1):
        if busy[0]:
            return
        try:
            settings = Settings(language="french" if profile.get().startswith("Rapide") else "french_24l",
                                voice=voice.get(), reference=reference.get().strip(), seed=int(seed.get()))
            phrase = text.get("1.0", "end").strip()
            settings.validate(phrase)
        except ValueError as exc:
            messagebox.showerror("Paramètres", str(exc))
            return
        should_play = play.get()
        stop.clear()
        busy[0] = True
        for button in buttons:
            button.configure(state="disabled")
        stop_button.configure(state="normal")
        status.set("Démarrage…")

        def worker():
            def report(message):
                mailbox.put(("status", message))
            try:
                if load_only:
                    load_s, prep_s, _ = engine.prepare(settings, report)
                    report(f"Prêt : modèle {load_s:.2f} s ; préparation voix {prep_s:.2f} s.")
                else:
                    for index in range(repeats):
                        if stop.is_set():
                            break
                        report(f"Essai {index + 1}/{repeats}")
                        mailbox.put(("result", engine.run(phrase, settings, playback=should_play,
                                                          stop=stop, report=report)))
            except Exception as exc:
                traceback.print_exc()
                mailbox.put(("error", str(exc)))
            finally:
                mailbox.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def replay():
        if last[0] and os.name == "nt":
            import winsound
            winsound.PlaySound(last[0], winsound.SND_FILENAME | winsound.SND_ASYNC)

    def outputs():
        folder = ROOT / "outputs"
        folder.mkdir(exist_ok=True)
        if os.name == "nt":
            os.startfile(str(folder))

    for label, command in (("Préparer", lambda: launch(True)), ("Tester et écouter", lambda: launch()),
                           ("Mesurer 3 essais", lambda: launch(repeats=3)), ("Réécouter", replay),
                           ("Résultats", outputs)):
        button = ttk.Button(actions, text=label, command=command)
        button.pack(side="left", padx=(0, 8))
        buttons.append(button)
    stop_button = ttk.Button(actions, text="Arrêter", command=stop.set, state="disabled")
    stop_button.pack(side="left")

    def append(message):
        log.configure(state="normal")
        log.insert("end", message + "\n")
        log.see("end")
        log.configure(state="disabled")

    def poll():
        try:
            while True:
                kind, data = mailbox.get_nowait()
                if kind == "status":
                    status.set(data)
                    append(data)
                elif kind == "result":
                    submitted = data["first_audio_submitted_s"]
                    table.insert("", "end", values=(f"{data['first_audio_ready_s']:.3f} s",
                                 f"{submitted:.3f} s" if submitted is not None else "—",
                                 f"{data['generation_s']:.2f} s", f"{data['audio_s']:.2f} s",
                                 f"{data['real_time_factor']:.2f}", data["playback_underflows"]))
                    status.set(f"Terminé : premier bloc en {data['first_audio_ready_s']:.3f} s.")
                    last[0] = data["wav"]
                    append("Fichier : " + data["wav"])
                    if data["playback_error"]:
                        append("Erreur de lecture : " + data["playback_error"])
                elif kind == "error":
                    status.set("Échec : " + data)
                    messagebox.showerror("Pocket TTS", data)
                elif kind == "done":
                    busy[0] = False
                    for button in buttons:
                        button.configure(state="normal")
                    stop_button.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, poll)

    root.protocol("WM_DELETE_WINDOW", lambda: (stop.set(), root.destroy()))
    root.after(100, poll)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text")
    parser.add_argument("--language", choices=("french", "french_24l"), default="french")
    parser.add_argument("--voice", choices=VOICES, default="estelle")
    parser.add_argument("--reference", default="")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--play", action="store_true")
    args = parser.parse_args(argv)
    if not args.text:
        window()
        return 0
    if not 1 <= args.repeat <= 10:
        parser.error("--repeat doit être compris entre 1 et 10")
    engine = Engine()
    settings = Settings(args.language, args.voice, args.reference, args.seed)
    try:
        for _ in range(args.repeat):
            result = engine.run(args.text, settings, playback=args.play)
            print("RESULT " + json.dumps(result, ensure_ascii=False), flush=True)
    except Exception:
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
