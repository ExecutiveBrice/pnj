"""Desktop process bridge. No GUI imports: usable and testable without a display."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path


class DesktopProcess:
    """One isolated Python engine; only the reader thread touches stdout.

    The GUI drains messages on its own thread. No shell, terminal emulation,
    or global stdout redirection in the GUI process.
    """

    def __init__(self):
        self.process = None
        self.messages: queue.Queue = queue.Queue()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, campaign: Path, *, prepare: bool = False, microphone: bool = True,
              test_voice: str | None = None, test_text: str | None = None):
        if self.process is not None:
            raise RuntimeError("La session précédente doit être terminée avant de redémarrer.")
        executable = Path(sys.executable)
        # GUI entry points on Windows use pythonw, whose stdio is disabled.
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        command = [str(executable), "-X", "utf8", "-u", "-m", "npc.desktop_worker", str(campaign.resolve())]
        if prepare:
            command.append("--prepare")
        if not microphone:
            command.append("--no-microphone")
        if test_voice is not None:
            command.extend(("--test-voice", test_voice))
        if test_text is not None:
            command.extend(("--test-text", test_text))
        options = {}
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                 "PYTHONUTF8": "1"},
            **options,
        )
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict) or "kind" not in message:
                        raise ValueError("not a desktop message")
                except (ValueError, TypeError):
                    message = {"kind": "log", "text": line.rstrip()}
                self.messages.put(message)
        finally:
            process.stdout.close()
            code = process.wait()
            if process.stdin is not None:
                process.stdin.close()
            self.messages.put({"kind": "exit", "code": code})

    def send(self, action: str, **values):
        if not self.running:
            raise RuntimeError("Aucune session active.")
        try:
            self.process.stdin.write(json.dumps({"action": action, **values}, ensure_ascii=False)
                                     + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RuntimeError("La session ne répond plus.") from exc

    def terminate(self):
        if self.running:
            self.process.kill()

    def acknowledge_exit(self):
        """Called only after draining the exit message, before a new launch."""
        self.process = None
