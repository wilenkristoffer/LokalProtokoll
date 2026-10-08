"""Small desktop window: record, follow processing, read past meetings.

Start with:  python lp.py app   (or double-click LokalProtokoll.pyw)

- The window is hidden from screen sharing by default (Windows 10 2004 and
  newer): you see it, but Teams/Zoom/screenshots do not.
- Minutes, transcripts and speaker naming open in a panel on the right side of
  the same window (viewer.py).
- Closing the window tucks it away in the system tray; quit from the tray menu.
- Heavy work runs "lp.py ..." in a subprocess, so the command line and the app
  always behave the same.
"""

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import font as tkfont

import customtkinter as ctk

from . import output, recorder, speakers
from .config import PROJECT_DIR, resolve
from .theme import (BG, CARD, CHECK, DOT, INK, INK_HOVER, LINE, MIDDOT, MUTED, OTHERS, RED, RED_HOVER,
                    SQUARE, YOU, fmt_duration, pick, restyle_segments, speaker_color)
from .viewer import Viewer

LEFT_WIDTH, VIEWER_WIDTH = 380, 640
SPEAKER_CHOICES = ["Auto", "1", "2", "3", "4", "5", "6"]
MORE_LABEL = "More"

# Glyphs from the "Segoe Fluent Icons" font that ships with Windows 11
# ("Segoe MDL2 Assets" on Windows 10 has the same codes).
ICONS = {"minutes": "\ue8a5", "transcript": "\ue8fd", "speakers": "\ue716", "rewrite": "\ue72c",
         "folder": "\ue838", "delete": "\ue74d", "play": "\ue768", "pin": "\ue718", "copy": "\ue8c8",
         "open": "\ue8a7", "more": "\ue712", "import": "\ue8b5", "hidden": "\ued1a", "visible": "\ue890",
         "tray": "\ue921", "log": "\ue9d9", "add": "\ue710", "edit": "\ue70f", "find": "\ue721",
         "rename": "\ue8ac", "redo": "\ue895", "close": "\ue711", "profile": "\ue77b"}

STEPS = ["convert", "detect", "transcribe", "diarize", "summarize"]
STEP_LABELS = {"convert": "Converting audio", "detect": "Detecting language",
               "transcribe": "Transcribing", "diarize": "Finding speakers",
               "summarize": "Writing minutes"}

WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11


def set_hidden_from_capture(window, hidden):
    """Hide a window from screen sharing and screenshots (it stays visible to you)."""
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(
            hwnd, WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE))
    except (AttributeError, OSError):
        return False


def window_handle(window):
    return ctypes.windll.user32.GetParent(window.winfo_id())


def mouse_button_down():
    """True if the left or right mouse button is pressed, or was pressed since the
    last check (so short clicks between two checks are not missed)."""
    user32 = ctypes.windll.user32
    return any(user32.GetAsyncKeyState(button) & 0x8001 for button in (0x01, 0x02))


def open_path(path):
    if Path(path).exists():
        os.startfile(path)


def app_icon_image(size=64):
    """The app/tray icon: a plain "notes" tile. Deliberately neutral, and the same
    while recording, so the taskbar or tray does not show that a meeting is recorded."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((3 * s, 3 * s, 61 * s, 61 * s), radius=13 * s, fill="#2E3A40")
    # Three thick lines stay readable at 16 px (tray, title bar).
    for i, length in enumerate((36, 28, 20)):
        y = (18 + 12 * i) * s
        d.rounded_rectangle((14 * s, y, (14 + length) * s, y + 6 * s), radius=3 * s, fill="#E9E4DA")
    return img


def write_icon(path):
    """Save the app icon as a .ico (window, taskbar and desktop shortcut)."""
    app_icon_image(256).save(path, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    return path


# ---------------------------------------------------------------- one window only

SHOW_EVENT = r"Local\LokalProtokoll.Show"


def claim_single_instance():
    """Returns a handle to wait on when this is the first LokalProtokoll, or None
    when one is already running (it has then been asked to show its window)."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateEventW.restype = ctypes.c_void_p
        handle = kernel32.CreateEventW(None, False, False, SHOW_EVENT)
        if handle and ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.SetEvent(ctypes.c_void_p(handle))
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return None
        return handle or -1
    except (AttributeError, OSError):
        return -1  # not Windows: no check


def watch_for_second_start(handle, show_requested):
    """Set show_requested each time another start of the app signals the event."""
    if handle == -1:
        return
    kernel32 = ctypes.WinDLL("kernel32")

    def wait():
        while kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0xFFFFFFFF) == 0:
            show_requested.set()
    threading.Thread(target=wait, daemon=True).start()


# ---------------------------------------------------------------- background work

class RecordingWorker:
    """Records one meeting at a time in a background thread. The window reads
    self.status regularly to show the timer and the levels. A finished recording
    is handed to on_recorded(args, name, folder) to be processed, so the next
    meeting can be recorded right away."""

    def __init__(self, cfg, on_recorded, on_failed):
        self.cfg = cfg
        self.on_recorded, self.on_failed = on_recorded, on_failed
        self.status = {"state": "idle"}
        self.stop_event = threading.Event()
        self.thread = None
        self.quitting = False

    def recording(self):
        return self.status["state"] == "recording"

    def start(self, name, num_speakers, mic_only):
        """name "" means: use a placeholder now and a title made from the minutes later."""
        auto_name = not name
        name = name or output.DEFAULT_NAME
        out_dir = output.new_meeting_dir(self.cfg, name, datetime.now())
        self.stop_event.clear()
        self.status = {"state": "recording", "name": name, "folder": str(out_dir),
                       "elapsed": 0.0, "levels": {}, "mic_only": mic_only}
        self.thread = threading.Thread(target=self._record, daemon=True,
                                       args=(out_dir, name, num_speakers, mic_only, auto_name))
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _record(self, out_dir, name, num_speakers, mic_only, auto_name):
        rec = self.cfg["record"]
        try:
            info = recorder.record(out_dir, name, rec.get("mic_device", ""), rec.get("speaker_device", ""),
                                   mic_only=mic_only, stop_event=self.stop_event, status=self.status,
                                   auto_name=auto_name)
        except (Exception, SystemExit) as e:
            shutil.rmtree(out_dir, ignore_errors=True)
            self.status = {"state": "idle"}
            self.on_failed(name, str(e))
            return
        self.status = {"state": "idle"}
        if info["duration_s"] < 2:
            shutil.rmtree(out_dir, ignore_errors=True)
            return
        if self.quitting:
            return  # saved: it shows as "not processed" next time
        args = ["process", str(out_dir)]
        if num_speakers:
            args += ["--speakers", str(num_speakers)]
        self.on_recorded(args, name, out_dir)


class JobQueue:
    """Runs lp.py commands one at a time in a background thread, in the order they
    were added (one at a time, so two jobs never share the GPU). Each job is a
    dict the window reads to show progress: {"id", "args", "name", "folder",
    "state": waiting/processing/done/error, "stage", "step", "log", "message"}.
    self.version changes when a job is added, starts, finishes or is removed, so
    the window knows when to redraw."""

    MAX_FINISHED = 5

    def __init__(self, config_path):
        self.config_path = config_path
        self.lock = threading.Condition()
        self.waiting, self.finished = [], []
        self.current = None
        self.proc = None
        self.version = 0
        self.next_id = 0
        self.quitting = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _new_job(self, args, name, folder, state):
        self.next_id += 1
        return {"id": self.next_id, "args": args, "name": name, "folder": str(folder or ""), "state": state,
                "stage": "Waiting", "step": 0, "log": [], "message": ""}

    def add(self, args, name, folder=None):
        with self.lock:
            self.waiting.append(self._new_job(args, name, folder, "waiting"))
            self.version += 1
            self.lock.notify()

    def add_failed(self, name, message):
        """Show an error that happened outside a job (e.g. the recording failed)."""
        with self.lock:
            job = self._new_job([], name, "", "error")
            job.update(message=message, log=[message])
            self._finish(job)

    def has(self, folder):
        """True if a job for this meeting folder is waiting or running."""
        folder = str(folder)
        with self.lock:
            return any(job and job["folder"] == folder for job in self.waiting + [self.current])

    def busy(self):
        return self.current is not None or bool(self.waiting)

    def cancel(self, job_id):
        """Remove a waiting job (the running one is not stopped)."""
        with self.lock:
            self.waiting = [job for job in self.waiting if job["id"] != job_id]
            self.version += 1

    def dismiss(self, job_id):
        with self.lock:
            self.finished = [job for job in self.finished if job["id"] != job_id]
            self.version += 1

    def stop(self):
        """Quitting: forget the waiting jobs and stop the running one."""
        with self.lock:
            self.quitting = True
            self.waiting = []
            if self.proc:
                self.proc.terminate()

    def _finish(self, job):
        self.finished = (self.finished + [job])[-self.MAX_FINISHED:]
        self.version += 1

    def _loop(self):
        while True:
            with self.lock:
                while not self.waiting:
                    self.lock.wait()
                job = self.waiting.pop(0)
                job.update(state="processing", stage="Starting")
                self.current = job
                self.version += 1
            self._run_cli(job)
            with self.lock:
                self.current = None
                self._finish(job)

    def _run_cli(self, job):
        python = sys.executable.replace("pythonw.exe", "python.exe")
        cmd = [python, str(PROJECT_DIR / "lp.py")]
        if self.config_path:
            cmd += ["--config", str(self.config_path)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        # Below normal priority: processing may run during the next meeting and
        # should not take the CPU from the recording or the call itself.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        with self.lock:
            if self.quitting:
                job.update(state="error", message="Stopped")
                return
            self.proc = subprocess.Popen(cmd + job["args"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         cwd=PROJECT_DIR, env=env, creationflags=flags)
        for raw in self.proc.stdout:
            for line in raw.decode("utf-8", errors="replace").replace("\r", "\n").splitlines():
                if not line.strip():
                    continue
                job["log"] = (job["log"] + [line.rstrip()])[-400:]
                m = re.fullmatch(r"\[([a-z ]+)\]", line.strip())
                if m:
                    step, _, detail = m.group(1).partition(" ")
                    if step in STEPS:
                        job["step"] = STEPS.index(step)
                        job["stage"] = f"{STEP_LABELS[step]} ({detail})" if detail else STEP_LABELS[step]
                if line.startswith("Output: "):
                    job["folder"] = line[len("Output: "):].strip()
        self.proc.wait()
        if job["folder"] and Path(job["folder"]).is_dir():
            Path(job["folder"], "process.log").write_text("\n".join(job["log"]), encoding="utf-8")
        if self.proc.returncode == 0:
            job["state"] = "done"
        else:
            tail = [line for line in job["log"] if "progress =" not in line][-6:]
            job.update(state="error", message="\n".join(tail) or "Failed")
        self.proc = None


def list_meetings(cfg):
    """Newest first: [{"folder", "name", "date", "duration_s", "speakers", "processed"}]."""
    base = Path(resolve(cfg["paths"]["meetings_dir"]))
    items = []
    if not base.exists():
        return items
    for folder in sorted(base.iterdir(), reverse=True):
        if not folder.is_dir() or folder.name.startswith("_"):
            continue
        meeting_json, rec_json = folder / "meeting.json", folder / "recording.json"
        try:
            if meeting_json.exists():
                m = json.loads(meeting_json.read_text(encoding="utf-8"))
                count = len({s.get("speaker") for s in m["segments"] if s.get("speaker") is not None})
                items.append({"folder": folder, "name": m["name"], "date": m["date"],
                              "duration_s": m.get("duration_s", 0), "speakers": count, "processed": True})
            elif rec_json.exists() and (folder / "mic.wav").exists():
                r = json.loads(rec_json.read_text(encoding="utf-8"))
                items.append({"folder": folder, "name": r["name"], "date": r["started"].replace("T", " ")[:16],
                              "duration_s": r.get("duration_s", 0), "speakers": 0, "processed": False})
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return items


# ---------------------------------------------------------------- small widgets

class IconButton(ctk.CTkFrame):
    """A flat button with an icon glyph and optional text; highlights on hover."""

    def __init__(self, parent, app, glyph, text, command, text_color=INK, hover=LINE, height=30,
                 pad=10, font=None, fg_color="transparent", icon_font=None, icon_color=None):
        super().__init__(parent, fg_color=fg_color, corner_radius=8, height=height)
        self.command, self.hover, self.normal = command, hover, fg_color
        self.icon = ctk.CTkLabel(self, text=glyph, font=icon_font or app.f_icon, text_color=icon_color or text_color,
                                 width=16)
        self.icon.pack(side="left", padx=(pad, 6 if text else pad), pady=4)
        self.label = None
        if text:
            self.label = ctk.CTkLabel(self, text=text, font=font or app.f_small, text_color=text_color)
            self.label.pack(side="left", padx=(0, pad + 2), pady=4)
        for widget in (self, self.icon, self.label):
            if widget is not None:
                widget.bind("<Enter>", self._enter)
                widget.bind("<Leave>", self._leave)
                widget.bind("<ButtonRelease-1>", self._click)
                widget.configure(cursor="hand2")

    def _enter(self, event=None):
        self.configure(fg_color=self.hover)

    def _leave(self, event=None):
        # Moving between the icon and the text also fires Leave; ignore that.
        inside = self.winfo_containing(*self.winfo_pointerxy())
        if not (inside and str(inside).startswith(str(self))):
            self.configure(fg_color=self.normal)

    def _click(self, event=None):
        if self.command:
            self.command()

    def set_style(self, glyph=None, text=None, color=None, fg_color=None):
        if glyph:
            self.icon.configure(text=glyph)
        if text and self.label is not None:
            self.label.configure(text=text)
        if color:
            self.icon.configure(text_color=color)
            if self.label is not None:
                self.label.configure(text_color=color)
        if fg_color is not None:
            self.normal = fg_color
            self.configure(fg_color=fg_color)


class PopupMenu(ctk.CTkToplevel):
    """A rounded dropdown menu with icons. items: (glyph, text, command, danger) or None
    for a separator. Closes after choosing, on Escape, on a click anywhere outside
    it, or when another program becomes the active window."""

    def __init__(self, app, items, x, y, align_right=False):
        """x, y: where the top-left corner goes (or the top-right corner with align_right)."""
        super().__init__(app)
        self.app = app
        app.popup = self
        self.overrideredirect(True)
        # Owned by the app window: stays above the app, but not above other programs.
        self.transient(app)
        # Pixels in this color become see-through, which gives the menu round corners.
        key = "#FBFAF7" if ctk.get_appearance_mode() == "Light" else "#211F1D"
        self.configure(fg_color=key)
        self.attributes("-transparentcolor", key)
        frame = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12, border_width=1, border_color=LINE,
                             bg_color=key)
        frame.pack(fill="both", expand=True)
        self._fill(frame, items)

        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        if align_right:
            x -= w
        x = max(8, min(x, self.winfo_screenwidth() - w - 8))  # keep it on the screen
        if y + h > self.winfo_screenheight() - 48:
            y = y - h - 36
        self.geometry(f"+{x}+{y}")
        set_hidden_from_capture(self, app.hidden.get())
        self.bind("<Escape>", lambda e: self.destroy())
        self.after(20, self.focus_force)
        mouse_button_down()  # clear "pressed since last check" from the click that opened the menu
        self.after(80, self._watch)

    def _fill(self, frame, items):
        for item in items:
            if item is None:
                ctk.CTkFrame(frame, height=1, corner_radius=0, fg_color=LINE).pack(fill="x", padx=14, pady=5)
                continue
            glyph, text, command, danger = item
            IconButton(frame, self.app, glyph, text, lambda c=command: self._choose(c), font=self.app.f_body,
                       text_color=RED if danger else INK, height=34, pad=12).pack(fill="x", padx=6, pady=1)
        ctk.CTkFrame(frame, height=4, fg_color="transparent").pack()
        frame.pack_configure(ipady=4)

    def _watch(self):
        """Clicks inside the app are handled by App.close_popup_on_click. This
        catches the rest: clicks on the title bar or other programs, and another
        program becoming the active window."""
        if not self.winfo_exists():
            return
        px, py = self.winfo_pointerxy()
        inside = (self.winfo_rootx() <= px < self.winfo_rootx() + self.winfo_width()
                  and self.winfo_rooty() <= py < self.winfo_rooty() + self.winfo_height())
        active = ctypes.windll.user32.GetForegroundWindow()
        if (mouse_button_down() and not inside) or active not in (window_handle(self), window_handle(self.app)):
            self.destroy()
            return
        self.after(80, self._watch)

    def _choose(self, command):
        self.destroy()
        command()


class NumberPicker(PopupMenu):
    """A small grid of numbers (e.g. 7-20 speakers) in the same style as the menu.
    items: (numbers, current or None, function called with the choice[, title[, note]]).
    numbers may contain "Auto"."""

    COLUMNS = 5

    def _fill(self, frame, items):
        numbers, current, on_pick = items[:3]
        title = items[3] if len(items) > 3 else "How many others?"
        note = items[4] if len(items) > 4 else None
        ctk.CTkLabel(frame, text=title, font=self.app.f_small,
                     text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 4))
        grid = ctk.CTkFrame(frame, fg_color="transparent")
        grid.pack(padx=10, pady=(0, 6 if note else 10))
        first_row = 0
        if "Auto" in numbers:  # "Auto" gets its own full-width row above the numbers
            ctk.CTkButton(grid, text="Auto", height=32, corner_radius=8, font=self.app.f_body,
                          fg_color=INK if current == "Auto" else BG, hover_color=LINE,
                          text_color=BG if current == "Auto" else INK,
                          command=lambda: self._choose(lambda: on_pick("Auto"))
                          ).grid(row=0, column=0, columnspan=self.COLUMNS, sticky="ew", padx=2, pady=2)
            numbers = [n for n in numbers if n != "Auto"]
            first_row = 1
        for i, n in enumerate(numbers):
            chosen = n == current
            ctk.CTkButton(grid, text=str(n), width=40, height=32, corner_radius=8,
                          font=self.app.f_body, fg_color=INK if chosen else BG,
                          hover_color=INK_HOVER if chosen else LINE, text_color=BG if chosen else INK,
                          command=lambda n=n: self._choose(lambda: on_pick(n))
                          ).grid(row=first_row + i // self.COLUMNS, column=i % self.COLUMNS, padx=2, pady=2)
        if note:
            ctk.CTkLabel(frame, text=note, font=self.app.f_small, text_color=MUTED, justify="left",
                         wraplength=250).pack(anchor="w", padx=14, pady=(0, 10))


class SpeakerPicker(PopupMenu):
    """"Who said this?" for a sentence in the transcript: choose an existing speaker
    or a new one, for this sentence or the whole paragraph, or fix the text.
    items: (meeting, segment index, on_speaker(indexes, speaker), on_edit_text())."""

    def _fill(self, frame, items):
        from .edit import next_speaker_number, paragraph_of
        meeting, index, on_speaker, on_edit_text = items
        current = meeting["segments"][index].get("speaker")
        paragraph = paragraph_of(meeting, index)
        ctk.CTkLabel(frame, text="Who said this?", font=self.app.f_small,
                     text_color=MUTED).pack(anchor="w", padx=14, pady=(10, 4))
        scope = ctk.CTkSegmentedButton(frame, values=["This sentence", "Whole paragraph"], font=self.app.f_small,
                                       height=26, selected_color=INK, selected_hover_color=INK_HOVER,
                                       unselected_color=BG, unselected_hover_color=LINE, fg_color=BG,
                                       command=lambda v: restyle_segments(scope))
        scope.set("This sentence")
        restyle_segments(scope)
        if len(paragraph) > 1:
            scope.pack(fill="x", padx=12, pady=(0, 6))

        def choose(speaker):
            indexes = paragraph if scope.get() == "Whole paragraph" else [index]
            self._choose(lambda: on_speaker(indexes, speaker))

        numbers = sorted({s["speaker"] for s in meeting["segments"] if isinstance(s.get("speaker"), int)})
        for number in numbers:
            name = output.speaker_name(meeting, number)
            row = IconButton(frame, self.app, DOT, name + ("   (now)" if number == current else ""),
                             lambda n=number: choose(n), font=self.app.f_body, height=32, pad=12,
                             icon_font=self.app.f_body, icon_color=speaker_color(number))
            row.pack(fill="x", padx=6, pady=1)
        new = next_speaker_number(meeting)
        IconButton(frame, self.app, ICONS["add"], f"New speaker ({output.speaker_name(meeting, new)})",
                   lambda: choose(new), font=self.app.f_body, height=32, pad=12).pack(fill="x", padx=6, pady=1)
        ctk.CTkFrame(frame, height=1, corner_radius=0, fg_color=LINE).pack(fill="x", padx=14, pady=5)
        IconButton(frame, self.app, ICONS["edit"], "Fix the text...", lambda: self._choose(on_edit_text),
                   font=self.app.f_body, height=32, pad=12).pack(fill="x", padx=6, pady=(1, 8))


class PromptPopup(PopupMenu):
    """A small form: a title, one or more text fields and a Save button.
    items: (title, [(label, initial text), ...], on_save(values), save label)."""

    def _fill(self, frame, items):
        title, fields, on_save, save_label = items
        ctk.CTkLabel(frame, text=title, font=self.app.f_bold, text_color=INK).pack(anchor="w", padx=14, pady=(12, 6))
        self.entries = []
        for label, initial in fields:
            if label:
                ctk.CTkLabel(frame, text=label, font=self.app.f_small, text_color=MUTED).pack(anchor="w", padx=14)
            entry = ctk.CTkEntry(frame, width=380, height=34, font=self.app.f_body, border_width=1,
                                 border_color=LINE, fg_color=BG, text_color=INK)
            entry.insert(0, initial)
            entry.pack(fill="x", padx=12, pady=(0, 8))
            entry.bind("<Return>", lambda e: self._save(on_save))
            entry.bind("<Escape>", lambda e: self.destroy())
            self.entries.append(entry)
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(buttons, text=save_label, width=100, height=32, corner_radius=8, font=self.app.f_small,
                      fg_color=INK, hover_color=INK_HOVER, text_color=BG,
                      command=lambda: self._save(on_save)).pack(side="right")
        ctk.CTkButton(buttons, text="Cancel", width=80, height=32, corner_radius=8, font=self.app.f_small,
                      fg_color=BG, hover_color=LINE, text_color=INK, border_width=1, border_color=LINE,
                      command=self.destroy).pack(side="right", padx=(0, 6))
        if self.entries:
            self.after(60, lambda: (self.entries[0].focus_set(), self.entries[0].select_range(0, "end")))

    def _save(self, on_save):
        values = [e.get() for e in self.entries]
        self._choose(lambda: on_save(values))


MINUTES_LANGUAGES = {"auto": "Same as the meeting", "sv": "Svenska", "en": "English"}


DEVICE_CHOICES = {"auto": "Auto", "desktop": "Desktop GPU", "laptop": "Laptop GPU", "small": "Small GPU",
                  "cpu": "No GPU"}


class ProfilePopup(PromptPopup):
    """Your name, the language of the minutes and the device profile.
    items: (name, language "auto"/"sv"/"en", device "auto"/"desktop"/"laptop"/"cpu"/"custom",
    hardware.detect() result, on_save(name, language, device))."""

    def _segments(self, frame, values, current):
        choice = ctk.CTkSegmentedButton(frame, values=values, font=self.app.f_small, height=28, selected_color=INK,
                                        selected_hover_color=INK_HOVER, unselected_color=BG,
                                        unselected_hover_color=LINE, fg_color=BG,
                                        command=lambda v: restyle_segments(choice))
        choice.set(current)
        restyle_segments(choice)
        return choice

    def _fill(self, frame, items):
        from .hardware import PROFILES, describe
        name, language, device, info, on_save = items
        ctk.CTkLabel(frame, text="Profile", font=self.app.f_bold, text_color=INK).pack(anchor="w", padx=14, pady=(12, 6))
        ctk.CTkLabel(frame, text="Your name (shown as \"Name (Me)\" in recordings)", font=self.app.f_small,
                     text_color=MUTED).pack(anchor="w", padx=14)
        entry = ctk.CTkEntry(frame, width=380, height=34, font=self.app.f_body, border_width=1,
                             border_color=LINE, fg_color=BG, text_color=INK)
        entry.insert(0, name)
        entry.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(frame, text="Write the minutes in", font=self.app.f_small,
                     text_color=MUTED).pack(anchor="w", padx=14)
        choice = self._segments(frame, list(MINUTES_LANGUAGES.values()),
                                MINUTES_LANGUAGES.get(language, MINUTES_LANGUAGES["auto"]))
        choice.pack(fill="x", padx=12, pady=(0, 10))

        # Which models fit this computer. "custom" (set by hand in config.toml) is kept
        # unless another choice is clicked.
        ctk.CTkLabel(frame, text="This computer", font=self.app.f_small, text_color=MUTED).pack(anchor="w", padx=14)
        devices = self._segments(frame, list(DEVICE_CHOICES.values()), DEVICE_CHOICES.get(device, ""))
        devices.pack(fill="x", padx=12, pady=(0, 2))
        suggested = PROFILES[info["profile"]]["label"]
        note = f"{describe(info)}.\nAuto uses: {suggested}."
        if device == "custom":
            note += "\nNow: custom models from config.toml."
        ctk.CTkLabel(frame, text=note, font=self.app.f_small, text_color=MUTED, justify="left",
                     wraplength=370).pack(anchor="w", padx=14, pady=(0, 12))

        def save():
            picked = next(k for k, v in MINUTES_LANGUAGES.items() if v == choice.get())
            device_picked = next((k for k, v in DEVICE_CHOICES.items() if v == devices.get()), device)
            # Read the name now: _choose destroys the popup (and the entry) first.
            name_picked = entry.get().strip()
            self._choose(lambda: on_save(name_picked, picked, device_picked))
        entry.bind("<Return>", lambda e: save())
        entry.bind("<Escape>", lambda e: self.destroy())
        buttons = ctk.CTkFrame(frame, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=(0, 12))
        ctk.CTkButton(buttons, text="Save", width=100, height=32, corner_radius=8, font=self.app.f_small,
                      fg_color=INK, hover_color=INK_HOVER, text_color=BG, command=save).pack(side="right")
        ctk.CTkButton(buttons, text="Cancel", width=80, height=32, corner_radius=8, font=self.app.f_small,
                      fg_color=BG, hover_color=LINE, text_color=INK, border_width=1, border_color=LINE,
                      command=self.destroy).pack(side="right", padx=(0, 6))
        self.after(60, lambda: (entry.focus_set(), entry.select_range(0, "end")))


class Tooltip:
    """A small label shown under a widget while the mouse is over it."""

    def __init__(self, app, widget, text):
        self.app, self.widget, self.text, self.win = app, widget, text, None
        for w in [widget] + list(widget.winfo_children()):
            w.bind("<Enter>", self.show, add="+")
            w.bind("<Leave>", self.hide, add="+")

    def show(self, event=None):
        if self.win is not None:
            return
        # A plain Toplevel: in dark mode CTkToplevel redraws its title bar with
        # update() inside its constructor. That handled the next Enter event before
        # self.win was set, so a second tooltip opened and the first was never closed.
        self.win = tk.Toplevel(self.app, bg=pick(INK))
        self.win.overrideredirect(True)
        # Topmost like system tooltips, so it is above the app also when that is
        # "Keep on top". No transient(): on a window without a title bar that puts
        # it behind the app window.
        self.win.attributes("-topmost", True)
        ctk.CTkLabel(self.win, text=self.text, font=self.app.f_small, text_color=BG, fg_color=INK,
                     corner_radius=0, padx=8, pady=2).pack()
        self.win.geometry(f"+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 4}")
        set_hidden_from_capture(self.win, self.app.hidden.get())
        self.win.after(150, self._watch)

    def _pointer_over_widget(self):
        inside = self.widget.winfo_containing(*self.widget.winfo_pointerxy())
        return bool(inside and str(inside).startswith(str(self.widget)))

    def _watch(self):
        # Leave events can be missed (e.g. the window is tucked away under the
        # mouse), so also check regularly while the tooltip is shown.
        if self.win is None:
            return
        if not self.app.winfo_viewable() or not self._pointer_over_widget() or mouse_button_down():
            self.hide(force=True)
            return
        self.win.after(150, self._watch)

    def hide(self, event=None, force=False):
        if self.win is not None and (force or not self._pointer_over_widget()):
            self.win.destroy()
            self.win = None


class FittedName(ctk.CTkFrame):
    """One line of text cut to the width it gets, with "..." at the end. While the
    mouse is over a cut text, the text slides to the left to show the rest, and
    slides back when it reaches the end."""

    ELLIPSIS = chr(0x2026)
    DELAY, STEP, FRAME, PAUSE = 500, 1, 16, 1200  # ms before start, px per frame, ms per frame, ms at the ends

    def __init__(self, parent, text, font, text_color):
        super().__init__(parent, fg_color="transparent", corner_radius=0, width=1)
        self.text = text
        self.label = ctk.CTkLabel(self, text=text, font=font, text_color=text_color, anchor="w", padx=0)
        self.scale = self.label._get_widget_scaling()
        # Measure with the font the label really draws with. Measuring with the
        # unscaled font and scaling the result is a few percent off, because the
        # scaled size is rounded to whole pixels (13 * 1.5 = 19.5 -> 20).
        self.font = tkfont.Font(self, font=font.create_scaled_tuple(self.scale))
        # The label is placed, not packed, so it can be wider than this frame and
        # move inside it; the frame clips it. The frame height is set by hand.
        self.configure(height=round(self.label.winfo_reqheight() / self.scale))
        self.label.place(x=0, y=0)
        self.offset, self.job = 0, None
        for widget in (self, self.label):
            widget.bind("<Enter>", self._enter, add="+")
        self.bind("<Configure>", lambda e: self._reset())

    def bind_all_parts(self, sequence, func):
        for widget in (self, self.label):
            widget.bind(sequence, func, add="+")
            widget.configure(cursor="hand2")

    def _overflow(self):
        """How many screen pixels of the full text do not fit."""
        return self.font.measure(self.text) - self.winfo_width()

    def _fitted(self):
        if self._overflow() <= 0:
            return self.text
        room = self.winfo_width() - self.font.measure(self.ELLIPSIS) - 2
        low, high = 0, len(self.text)
        while low < high:  # the longest start of the text that fits
            mid = (low + high + 1) // 2
            if self.font.measure(self.text[:mid].rstrip()) <= room:
                low = mid
            else:
                high = mid - 1
        return self.text[:low].rstrip() + self.ELLIPSIS

    def _reset(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None
        self.offset = 0
        self.label.configure(text=self._fitted())
        self.label.place_configure(x=0)

    def _pointer_inside(self):
        inside = self.winfo_containing(*self.winfo_pointerxy())
        return bool(inside and str(inside).startswith(str(self)))

    def _enter(self, event=None):
        if self.job is None and self._overflow() > 0:
            self.job = self.after(self.DELAY, self._start)

    def _start(self):
        if not self._pointer_inside():
            self._reset()
            return
        self.label.configure(text=self.text)
        self._slide(-1)

    def _slide(self, direction):
        # Checked every frame instead of using <Leave>: the moving label fires
        # Leave and Enter by itself while the mouse stands still.
        if not self._pointer_inside():
            self._reset()
            return
        # Screen pixels: place_configure() is plain tkinter and is not scaled like
        # CTk's place().
        end = -(self._overflow() + 4)
        self.offset = max(end, min(0, self.offset + direction * self.STEP * self.scale))
        self.label.place_configure(x=round(self.offset))
        if (direction < 0 and self.offset <= end) or (direction > 0 and self.offset >= 0):
            self.job = self.after(self.PAUSE, lambda: self._slide(-direction))
        else:
            self.job = self.after(self.FRAME, lambda: self._slide(direction))


class Tray:
    """System tray icon. Menu clicks arrive on the tray's own thread, so they are
    put in a queue that the window handles in tick()."""

    def __init__(self):
        import pystray
        self.queue = queue.Queue()
        self.recording = False
        menu = pystray.Menu(
            pystray.MenuItem("Show LokalProtokoll", lambda: self.queue.put("show"), default=True),
            pystray.MenuItem(lambda item: "Stop recording" if self.recording else "Start recording",
                             lambda: self.queue.put("record")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.queue.put("quit")))
        self.icon = pystray.Icon("LokalProtokoll", app_icon_image(), "LokalProtokoll", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def set_recording(self, recording):
        # Only the right-click menu changes; the icon and its hover text stay the
        # same so nothing on screen reveals that a meeting is being recorded.
        if recording != self.recording:
            self.recording = recording
            self.icon.update_menu()

    def notify(self, message):
        try:
            self.icon.notify(message, "LokalProtokoll")
        except Exception:
            pass

    def stop(self):
        self.icon.stop()


# ---------------------------------------------------------------- main window

class App(ctk.CTk):
    def __init__(self, cfg, config_path=None, tray=True):
        super().__init__()
        self.cfg = cfg
        self.config_path = config_path
        self.jobs = JobQueue(config_path)
        self.rec = RecordingWorker(cfg, self.jobs.add, self.jobs.add_failed)
        self.shown_state = None
        self.shown_jobs = None  # JobQueue.version the processing card was last drawn for
        self.notified = set()  # finished jobs the tray has told about
        self.viewer_open = False
        self.closed_width = LEFT_WIDTH
        self.rows = {}
        self.told_about_tray = False
        self.show_requested = threading.Event()  # set when the app is started a second time

        self.title("LokalProtokoll")
        self.geometry(f"{LEFT_WIDTH}x660")
        self.minsize(340, 500)
        self.configure(fg_color=BG)
        self._set_window_icon()

        self.f_brand = ctk.CTkFont("Bahnschrift", 17, "bold")
        self.f_title = ctk.CTkFont("Bahnschrift", 15, "bold")
        self.f_view_title = ctk.CTkFont("Bahnschrift", 19, "bold")
        self.f_timer = ctk.CTkFont("Bahnschrift", 46)
        self.f_body = ctk.CTkFont("Segoe UI Variable Text", 13)
        self.f_bold = ctk.CTkFont("Segoe UI Variable Text", 13, "bold")
        self.f_small = ctk.CTkFont("Segoe UI Variable Text", 11)
        self.f_button = ctk.CTkFont("Bahnschrift", 14, "bold")
        self.f_mono = ctk.CTkFont("Cascadia Mono", 10)
        self.f_icon = ctk.CTkFont("Segoe Fluent Icons", 13)

        self.hidden = ctk.BooleanVar(value=True)
        self.on_top = ctk.BooleanVar(value=False)

        # Two columns: the recorder on the left, the viewer (when open) on the right.
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.left = ctk.CTkFrame(self, fg_color="transparent", width=LEFT_WIDTH)
        self.left.grid(row=0, column=0, sticky="nsew")
        self.left.pack_propagate(False)
        self.viewer = Viewer(self)

        self._build_header()
        self.card = ctk.CTkFrame(self.left, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        self.card.pack(fill="x", padx=14, pady=(4, 10))
        self._build_idle()
        self._build_recording()
        self._build_jobs()
        self._build_list()

        self.tray = None
        if tray:
            try:
                self.tray = Tray()
            except Exception:
                self.tray = None

        self.popup = None
        # Clicks on labels and frames do not move keyboard focus in tkinter, so a
        # popup menu cannot rely on losing focus; every click in the app checks it.
        self.bind_all("<ButtonPress>", self.close_popup_on_click, add="+")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind("<Control-r>", lambda e: self.toggle_record())
        self.bind("<Control-f>", lambda e: self.viewer.open_find() if self.viewer_open else None)
        self.bind("<F3>", lambda e: self.viewer.find_step(1) if self.viewer.find_open else None)
        self.bind("<Shift-F3>", lambda e: self.viewer.find_step(-1) if self.viewer.find_open else None)
        self.bind("<Escape>", lambda e: self.close_viewer() if self.viewer_open else None)
        self.after(200, self.apply_window_flags)
        # First start, or a computer the models were not downloaded for.
        self.after(1500, self._offer_model_download)
        self.refresh_list()
        self.tick()

    def _set_window_icon(self):
        try:
            path = write_icon(Path(tempfile.gettempdir()) / "lokalprotokoll.ico")
            # CustomTkinter sets its own icon after ~200 ms; set ours after that.
            self.after(300, lambda: self.iconbitmap(str(path)))
        except Exception:
            pass

    # ----- layout -----
    def _build_header(self):
        bar = ctk.CTkFrame(self.left, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(bar, text="Lokal", font=self.f_brand, text_color=INK).pack(side="left")
        ctk.CTkLabel(bar, text="Protokoll", font=self.f_brand, text_color=RED).pack(side="left")
        self.tray_btn = IconButton(bar, self, ICONS["tray"], "", self.tuck_away, text_color=MUTED, pad=7)
        self.tray_btn.pack(side="right")
        Tooltip(self, self.tray_btn, "Tuck away in the tray")
        self.pin_btn = IconButton(bar, self, ICONS["pin"], "", self.toggle_on_top, text_color=MUTED, pad=7)
        self.pin_btn.pack(side="right", padx=2)
        Tooltip(self, self.pin_btn, "Keep on top")
        self.hide_btn = IconButton(bar, self, ICONS["hidden"], "Hidden", self.toggle_hidden, text_color=MUTED, pad=8)
        self.hide_btn.pack(side="right", padx=2)
        Tooltip(self, self.hide_btn, "Hidden from screen sharing and screenshots")
        self.profile_btn = IconButton(bar, self, ICONS["profile"], "", None, text_color=MUTED, pad=7)
        self.profile_btn.command = lambda: self.edit_profile(self.profile_btn)
        self.profile_btn.pack(side="right", padx=2)
        Tooltip(self, self.profile_btn, "Profile: your name and the language of the minutes")

    def _build_idle(self):
        f = self.idle = ctk.CTkFrame(self.card, fg_color="transparent")
        self.name_entry = ctk.CTkEntry(f, placeholder_text="Meeting name", font=self.f_body, height=36,
                                       border_width=1, border_color=LINE, fg_color=BG, text_color=INK)
        self.name_entry.pack(fill="x", padx=16, pady=(16, 10))

        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=16)
        ctk.CTkLabel(row, text="Others", font=self.f_small, text_color=MUTED).pack(side="left", padx=(0, 8))
        # "Auto", 1-6, and a last segment that opens a grid with 7-20. After choosing,
        # the last segment shows that number instead of "More".
        self.speakers_choice = "Auto"
        self.speakers_seg = ctk.CTkSegmentedButton(
            row, values=SPEAKER_CHOICES + [MORE_LABEL], font=self.f_small, height=26,
            selected_color=INK, selected_hover_color=INK_HOVER, unselected_color=BG,
            unselected_hover_color=LINE, fg_color=BG, command=self.on_speakers)
        self.speakers_seg.set("Auto")
        self.speakers_seg.pack(side="left", fill="x", expand=True)
        restyle_segments(self.speakers_seg)

        self.mic_only = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(f, text="In the room (microphone only)", variable=self.mic_only, font=self.f_small,
                      text_color=MUTED, progress_color=INK, button_color=("#FFFFFF", "#ECE7DD"),
                      button_hover_color=("#FFFFFF", "#FFFFFF"), fg_color=("#CFC8BA", "#4A4640"),
                      switch_width=36, switch_height=18).pack(anchor="w", padx=16, pady=(10, 4))

        ctk.CTkButton(f, text=f"{DOT}  Record", height=52, corner_radius=26, font=self.f_button,
                      fg_color=RED, hover_color=RED_HOVER, text_color="#FFFFFF",
                      command=self.toggle_record).pack(fill="x", padx=16, pady=(10, 16))

    def on_speakers(self, value):
        if value in SPEAKER_CHOICES:
            self._set_speakers(value)
            return
        # The last segment ("More" or a number above 6): keep the old choice until a
        # number is picked, and open the grid under the segment.
        self.speakers_seg.set(self.speakers_choice)
        restyle_segments(self.speakers_seg)
        button = getattr(self.speakers_seg, "_buttons_dict", {}).get(value, self.speakers_seg)
        current = int(self.speakers_choice) if self.speakers_choice.isdigit() else None
        NumberPicker(self, (list(range(len(SPEAKER_CHOICES), 21)), current, lambda n: self._set_speakers(str(n))),
                     button.winfo_rootx() + button.winfo_width(), button.winfo_rooty() + button.winfo_height() + 4,
                     align_right=True)

    def _set_speakers(self, value):
        """Select "Auto" or a number. Numbers above 6 are shown on the last segment."""
        self.speakers_choice = value
        last = value if value.isdigit() and int(value) >= len(SPEAKER_CHOICES) else MORE_LABEL
        if self.speakers_seg.cget("values")[-1] != last:
            self.speakers_seg.configure(values=SPEAKER_CHOICES + [last])
        self.speakers_seg.set(value)
        restyle_segments(self.speakers_seg)

    def _build_recording(self):
        f = self.recording = ctk.CTkFrame(self.card, fg_color="transparent")
        top = ctk.CTkFrame(f, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(14, 0))
        self.rec_dot = ctk.CTkLabel(top, text=DOT, font=self.f_title, text_color=RED)
        self.rec_dot.pack(side="left")
        self.rec_name = ctk.CTkLabel(top, text="", font=self.f_small, text_color=MUTED)
        self.rec_name.pack(side="left", padx=6)
        self.timer = ctk.CTkLabel(f, text="0:00", font=self.f_timer, text_color=INK)
        self.timer.pack(pady=(0, 6))
        self.meters = {}
        for key, label, color in (("mic", "You", YOU), ("system", "Others", OTHERS)):
            row = ctk.CTkFrame(f, fg_color="transparent")
            row.pack(fill="x", padx=18, pady=3)
            ctk.CTkLabel(row, text=label, width=52, anchor="w", font=self.f_small, text_color=MUTED).pack(side="left")
            bar = ctk.CTkProgressBar(row, height=8, corner_radius=4, progress_color=color, fg_color=LINE)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True)
            self.meters[key] = (row, bar)
        ctk.CTkButton(f, text=f"{SQUARE}  Stop", height=48, corner_radius=24, font=self.f_button,
                      fg_color=INK, hover_color=INK_HOVER, text_color=BG,
                      command=self.toggle_record).pack(fill="x", padx=16, pady=(14, 16))

    def _build_jobs(self):
        """A card of its own under the recorder: the job being processed, the jobs
        waiting for it, and the finished ones until they are dismissed. Hidden
        when there is nothing to show."""
        self.jobs_card = ctk.CTkFrame(self.left, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        f = self.job_now = ctk.CTkFrame(self.jobs_card, fg_color="transparent")
        self.proc_name = ctk.CTkLabel(f, text="", font=self.f_title, text_color=INK, anchor="w")
        self.proc_name.pack(fill="x", padx=16, pady=(12, 0))
        self.proc_stage = ctk.CTkLabel(f, text="", font=self.f_body, text_color=MUTED, anchor="w", justify="left")
        self.proc_stage.pack(fill="x", padx=16)
        self.proc_bar = ctk.CTkProgressBar(f, height=6, corner_radius=3, progress_color=INK, fg_color=LINE)
        self.proc_bar.pack(fill="x", padx=16, pady=(8, 4))
        self.proc_line = ctk.CTkLabel(f, text="", font=self.f_mono, text_color=MUTED, anchor="w")
        self.proc_line.pack(fill="x", padx=16, pady=(0, 8))
        # Waiting and finished jobs, redrawn when the queue changes.
        self.job_rows = ctk.CTkFrame(self.jobs_card, fg_color="transparent")

    def _render_jobs(self):
        jobs = self.jobs
        with jobs.lock:
            current, waiting, finished = jobs.current, list(jobs.waiting), list(jobs.finished)
        self.job_now.pack_forget()
        self.job_rows.pack_forget()
        for child in self.job_rows.winfo_children():
            child.destroy()
        if not (current or waiting or finished):
            self.jobs_card.pack_forget()
            return
        self.jobs_card.pack(fill="x", padx=14, pady=(0, 10), after=self.card)
        if current:
            self.proc_name.configure(text=current["name"])
            self.job_now.pack(fill="x")
        for i, job in enumerate(waiting):
            self._job_row(job, f"{'Next' if i == 0 else 'Then'}: {job['name']}", MUTED,
                          [(ICONS["close"], "", lambda j=job: jobs.cancel(j["id"]), "Remove from the queue")])
        for job in reversed(finished):
            folder = Path(job["folder"]) if job["folder"] else None
            if job["state"] == "done":
                name = job["name"]
                if folder and (folder / "meeting.json").exists():
                    try:  # the meeting may have got a title from its minutes
                        name = output.load_meeting(folder)["name"]
                    except (SystemExit, ValueError, KeyError):
                        pass
                view = "minutes" if folder and (folder / "summary.md").exists() else "speakers"
                buttons = [(ICONS["minutes"], "Open", lambda f=folder, v=view, j=job: self._open_job(j, f, v), None)]
                text, color = f"{CHECK}  {name}", YOU
            else:
                log = job["message"] + "\n\n" + "\n".join(job["log"])
                buttons = [(ICONS["log"], "Log", lambda n=job["name"], t=log: self.open_viewer(log=(f"Log - {n}", t)),
                            None)]
                text, color = f"!  Failed: {job['name']}", RED
            buttons.append((ICONS["close"], "", lambda j=job: jobs.dismiss(j["id"]), "Dismiss"))
            self._job_row(job, text, color, buttons)
        self.job_rows.pack(fill="x", pady=(0 if current else 6, 6))

    def _job_row(self, job, text, color, buttons):
        row = ctk.CTkFrame(self.job_rows, fg_color="transparent")
        row.pack(fill="x", padx=8, pady=1)
        for glyph, label, command, tip in reversed(buttons):  # packed first, so a long name cannot push them out
            button = IconButton(row, self, glyph, label, command, text_color=MUTED, pad=6, height=26)
            button.pack(side="right", padx=1)
            if tip:
                Tooltip(self, button, tip)
        name = FittedName(row, text, self.f_small, color)
        name.pack(side="left", fill="x", expand=True, padx=(8, 4))

    def _open_job(self, job, folder, view):
        self.jobs.dismiss(job["id"])
        if folder and folder.is_dir():
            self.open_viewer(folder, view)

    def _update_progress(self, job):
        self.proc_stage.configure(text=job.get("stage", "") + "...")
        # whisper and diarization print "progress = 52%"; use it inside the current step.
        last = next((line for line in reversed(job.get("log", [])) if line.strip()), "")
        m = re.search(r"progress =\s*(\d+)%", last)
        within = int(m.group(1)) / 100 if m else 0.1
        self.proc_bar.set((job.get("step", 0) + within) / len(STEPS))
        self.proc_line.configure(text=f"{m.group(1)}%" if m else last.strip()[:52])

    def _build_list(self):
        # Search all meetings: Enter shows the results in the panel on the right.
        box = ctk.CTkFrame(self.left, fg_color=CARD, corner_radius=10, border_width=1, border_color=LINE)
        box.pack(fill="x", padx=14, pady=(0, 8))
        ctk.CTkLabel(box, text=ICONS["find"], font=self.f_icon, text_color=MUTED, width=16).pack(side="left", padx=(10, 4))
        self.search_entry = ctk.CTkEntry(box, placeholder_text="Search all meetings", font=self.f_body, height=32,
                                         border_width=0, fg_color=CARD, text_color=INK)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 6), pady=2)
        self.search_entry.bind("<Return>", lambda e: self.run_search())
        self.search_entry.bind("<Escape>", lambda e: self.search_entry.delete(0, "end"))

        head = ctk.CTkFrame(self.left, fg_color="transparent")
        head.pack(fill="x", padx=18, pady=(4, 2))
        ctk.CTkLabel(head, text="MEETINGS", font=self.f_small, text_color=MUTED).pack(side="left")
        IconButton(head, self, ICONS["import"], "Import file", self.import_file, text_color=MUTED,
                   pad=6).pack(side="right")
        self.list_frame = ctk.CTkScrollableFrame(self.left, fg_color="transparent", scrollbar_button_color=LINE,
                                                 scrollbar_button_hover_color=MUTED)
        self.list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def close_popup_on_click(self, event):
        popup = self.popup
        if popup is not None and popup.winfo_exists() and not str(event.widget).startswith(str(popup)):
            popup.destroy()

    def icon_button(self, parent, icon, text, command):
        """Used by the viewer for its Copy / Open file buttons."""
        return IconButton(parent, self, ICONS[icon], text, command, text_color=MUTED, pad=8)

    def flash(self, button, text):
        """Briefly show a confirmation text on an IconButton (e.g. "Copied")."""
        old = button.label.cget("text")
        button.set_style(glyph="\ue73e", text=text)
        self.after(1200, lambda: button.set_style(glyph=ICONS["copy"], text=old))

    # ----- window flags -----
    def apply_window_flags(self):
        ok = set_hidden_from_capture(self, self.hidden.get())
        if self.hidden.get() and not ok:
            self.hidden.set(False)
        self.attributes("-topmost", self.on_top.get())
        hidden, top = self.hidden.get(), self.on_top.get()
        self.hide_btn.set_style(glyph=ICONS["hidden" if hidden else "visible"],
                                text="Hidden" if hidden else "Visible",
                                color=BG if hidden else MUTED, fg_color=INK if hidden else "transparent")
        self.pin_btn.set_style(color=BG if top else MUTED, fg_color=INK if top else "transparent")

    def toggle_hidden(self):
        self.hidden.set(not self.hidden.get())
        self.apply_window_flags()

    def toggle_on_top(self):
        self.on_top.set(not self.on_top.get())
        self.apply_window_flags()

    # ----- viewer panel -----
    def _geometry(self):
        w, h, x, y = map(int, re.match(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", self.geometry()).groups())
        return w, h, x, y

    def run_search(self):
        from . import search
        query = self.search_entry.get().strip()
        if not query:
            return
        self.open_viewer(search=(query, search.search(self.cfg, query)))

    def open_viewer(self, folder=None, view="minutes", log=None, search=None, focus=None):
        """Open the panel on the right: a meeting (view = minutes/transcript/speakers,
        focus = a sentence to scroll to), a log, or search results."""
        if not self.viewer_open:
            w, h, x, y = self._geometry()
            self.closed_width = w
            self.grid_columnconfigure(0, weight=0)
            self.grid_columnconfigure(1, weight=1)
            self.viewer.grid(row=0, column=1, sticky="nsew", padx=(0, 14), pady=(12, 14))
            self.minsize(LEFT_WIDTH + 420, 500)
            self.geometry(f"{LEFT_WIDTH + VIEWER_WIDTH}x{h}+{x}+{y}")
            self.viewer_open = True
            # Keep the wider window on screen (positions are in real pixels).
            self.update_idletasks()
            overflow = self.winfo_rootx() + self.winfo_width() - self.winfo_screenwidth()
            if overflow > 0:
                self.geometry(f"+{max(0, self.winfo_x() - overflow - 8)}+{self.winfo_y()}")
        if log is not None:
            self.viewer.show_log(*log)
            self.highlight_row(None)
        elif search is not None:
            self.viewer.show_search(*search)
            self.highlight_row(None)
        else:
            self.viewer.open(folder, view)
            if focus is not None:
                self.viewer.focus_sentence(focus)
            self.highlight_row(folder)

    def close_viewer(self):
        if not self.viewer_open:
            return
        self.viewer.stop_audio()
        _, h, x, y = self._geometry()
        self.viewer.grid_forget()
        self.viewer.folder = None
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.minsize(340, 500)
        self.geometry(f"{LEFT_WIDTH}x{h}+{x}+{y}")
        self.viewer_open = False
        self.highlight_row(None)

    def highlight_row(self, folder):
        for key, row in self.rows.items():
            active = folder is not None and key == str(folder)
            row.configure(border_color=INK if active else LINE, border_width=2 if active else 1)

    # ----- actions -----
    def toggle_record(self):
        # Processing runs in its own queue, so a new recording can start while the
        # last meeting is still being processed.
        if self.rec.recording():
            self.rec.stop()
        else:
            name = self.name_entry.get().strip()
            value = self.speakers_choice
            self.rec.start(name, 0 if value == "Auto" else int(value), self.mic_only.get())
            if name:  # the next meeting gets its own name
                self.name_entry.delete(0, "end")
                self.name_entry._activate_placeholder()

    def import_file(self):
        path = filedialog.askopenfilename(parent=self, title="Choose a recording",
                                          filetypes=[("Audio/video", "*.wav *.mp3 *.m4a *.mp4 *.mkv *.ogg *.flac *.webm"),
                                                     ("All files", "*.*")])
        if not path:
            return
        args = ["process", path]
        if self.name_entry.get().strip():
            args += ["--name", self.name_entry.get().strip()]
        if self.speakers_choice != "Auto":
            args += ["--speakers", self.speakers_choice]
        self.jobs.add(args, self.name_entry.get().strip() or Path(path).stem)

    def meeting_menu(self, item, widget):
        folder = item["folder"]
        if item["processed"]:
            items = [(ICONS["minutes"], "View minutes", lambda: self.open_viewer(folder, "minutes"), False),
                     (ICONS["transcript"], "View transcript", lambda: self.open_viewer(folder, "transcript"), False),
                     (ICONS["speakers"], "Name speakers", lambda: self.open_viewer(folder, "speakers"), False),
                     (ICONS["rewrite"], "Rewrite minutes", lambda: self.run_job(["summarize", str(folder)], item["name"], folder), False),
                     None,
                     (ICONS["rename"], "Rename...", lambda: self.rename_meeting(item, widget), False),
                     (ICONS["redo"], "Redo speakers...", lambda: self.redo_speakers(item, widget), False),
                     (ICONS["find"], "Find and replace...", lambda: self.find_replace(item, widget), False)]
        else:
            items = [(ICONS["play"], "Process now", lambda: self.run_job(["process", str(folder)], item["name"], folder), False)]
        items += [None,
                  (ICONS["folder"], "Open folder", lambda: open_path(folder), False),
                  (ICONS["delete"], "Delete", lambda: self.delete_meeting(item), True)]
        PopupMenu(self, items, widget.winfo_rootx() - 150, widget.winfo_rooty() + widget.winfo_height() + 4)

    # ----- corrections (lokalprotokoll/edit.py; quick, so they run here directly) -----
    def _can_edit(self, folder):
        if self.jobs.has(folder):
            messagebox.showinfo("Busy", "This meeting is being processed or waiting to be. Wait until it is "
                                "finished.", parent=self)
            return False
        return True

    def _after_edit(self, folder):
        self.refresh_list()
        if self.viewer_open and self.viewer.folder == Path(folder):
            self.viewer.reload(keep_scroll=True)

    def _popup_at(self, widget):
        return widget.winfo_rootx() - 150, widget.winfo_rooty() + widget.winfo_height() + 4

    def rename_meeting(self, item, widget):
        from . import edit

        def save(values):
            if values[0].strip() and self._can_edit(item["folder"]):
                edit.rename_meeting(item["folder"], values[0])
                self._after_edit(item["folder"])
        PromptPopup(self, ("Rename meeting", [("", item["name"])], save, "Rename"), *self._popup_at(widget))

    def edit_profile(self, widget):
        """Your name, used for you (the microphone) in recordings instead of "Me" /
        "Jag": shown as "Anna (Me)". And the language of the minutes. Saved as
        record.my_name and summarize.language in config.toml; processing reads
        config.toml for every job, so the next one uses them."""
        from . import edit, hardware
        from .config import load_config, save_string

        def save(name, language, device):
            previous = self.cfg["record"].get("my_name", "")
            try:
                if language != self.cfg["summarize"].get("language", "auto"):
                    save_string("summarize", "language", language, self.config_path)
                if name != previous:
                    save_string("record", "my_name", name, self.config_path)
                if device != self.cfg.get("device", {}).get("profile", "auto"):
                    save_string("device", "profile", device, self.config_path)
            except (KeyError, OSError) as e:
                messagebox.showerror("Profile", f"Could not save the profile in config.toml:\n{e}", parent=self)
                return
            self.cfg = load_config(self.config_path)  # with the models of the (new) device profile
            self.rec.cfg = self.cfg
            self._offer_model_download()
            if name == previous:
                return
            past = [f for f in edit.meetings_without_my_name(self.cfg, previous) if not self.jobs.has(f)]
            if not past:
                return
            label = f"\"{name} (Me)\"" if name else "\"Me\""
            if not messagebox.askyesno(
                    "Profile", f"New recordings will show you as {label}.\n\nAlso use it in your {len(past)} earlier "
                    f"recording{'s' if len(past) != 1 else ''}? Only your name changes in the transcript and the "
                    "minutes; the minutes are not rewritten.", parent=self):
                return
            for folder in past:
                edit.rename_speakers(output.load_meeting(folder), folder, {"0": name})
            self.refresh_list()
            if self.viewer_open and self.viewer.folder in past:
                self.viewer.reload(keep_scroll=True)
        ProfilePopup(self, (self.cfg["record"].get("my_name", ""), self.cfg["summarize"].get("language", "auto"),
                            self.cfg.get("device", {}).get("profile", "auto"), hardware.detect(), save),
                     *self._popup_at(widget))

    def _offer_model_download(self):
        """If the device profile needs models that are not downloaded, offer to
        download them (a job in the processing queue: lp.py models)."""
        from .hardware import missing_models
        missing = missing_models(self.cfg)
        if missing and not any(j["args"][:1] == ["models"] for j in self.jobs.waiting) and messagebox.askyesno(
                "Models", "This device profile uses models that are not downloaded yet:\n\n  " + "\n  ".join(missing)
                + "\n\nDownload them now? (In the processing queue; it can take a while.)", parent=self):
            self.jobs.add(["models"], "Download models")

    def redo_speakers(self, item, widget):
        folder = item["folder"]
        if not speakers.has_audio(output.load_meeting(folder), folder):
            messagebox.showinfo("Redo speakers", "The recording of this meeting was deleted, so the speakers "
                                "cannot be found again. You can still change who said what in the transcript.",
                                parent=self)
            return

        def run(choice):
            meeting = output.load_meeting(folder)
            if any(k != "0" for k in meeting.get("speaker_names", {})) and not messagebox.askyesno(
                    "Redo speakers", "The speaker names you gave will be removed, because the speakers are "
                    "numbered again. Continue?", parent=self):
                return
            args = ["rediarize", str(folder), "--summarize", "--speakers", "0" if choice == "Auto" else str(choice)]
            self.run_job(args, item["name"], folder)
        NumberPicker(self, (["Auto"] + list(range(1, 21)), None, run, "Redo speakers: how many?",
                            "Finds the speakers again and rewrites the minutes. The transcription is kept."),
                     *self._popup_at(widget))

    def find_replace(self, item, widget):
        from . import edit

        def save(values):
            find, replacement = values
            if not find.strip() or not self._can_edit(item["folder"]):
                return
            count = edit.replace_everywhere(item["folder"], find, replacement)
            self._after_edit(item["folder"])
            messagebox.showinfo("Find and replace", f"Replaced {count} time{'s' if count != 1 else ''} "
                                f"in the transcript and the minutes.", parent=self)
        PromptPopup(self, ("Find and replace (whole words, any case)", [("Find", ""), ("Replace with", "")],
                           save, "Replace"), *self._popup_at(widget))

    def saved_voices_menu(self, saved, widget):
        """The saved voices, each with "Forget" (from the Speakers tab)."""
        from . import voices

        def forget(name):
            if messagebox.askyesno("Forget voice", f"Delete the saved voice of {name}? They will no longer be "
                                   "named automatically. Names in earlier meetings stay.", parent=self):
                voices.delete_voice(self.cfg, name)
        if saved:
            items = [(ICONS["delete"], f"Forget {v['name']}  ({v['seconds']:.0f} s, {len(v.get('meetings', []))} "
                      f"meeting{'s' if len(v.get('meetings', [])) != 1 else ''})", lambda n=v["name"]: forget(n), True)
                     for v in saved]
        else:
            items = [(ICONS["speakers"], "No saved voices yet. Tick \"Remember voice\" for a named speaker.",
                      lambda: None, False)]
        PopupMenu(self, items, *self._popup_at(widget))

    def delete_audio_menu(self, folder, widget):
        """Delete the recording and/or the voice samples of a processed meeting (the
        "..." button in the top right corner of the viewer)."""
        from . import voices
        meeting = output.load_meeting(folder)
        recording = bool(speakers.audio_files(meeting, folder))
        samples = speakers.has_samples(folder) or bool(voices.voices_in_meeting(self.cfg, folder, meeting))
        items = []
        if recording:
            items.append((ICONS["delete"], "Delete recording", lambda: self.delete_audio(folder, True, False), True))
        if samples:
            items.append((ICONS["delete"], "Delete speaker voices", lambda: self.delete_audio(folder, False, True), True))
        if recording and samples:
            items.append((ICONS["delete"], "Delete recording and speaker voices",
                          lambda: self.delete_audio(folder, True, True), True))
        if not items:
            items = [(ICONS["delete"], "The recording and the speaker voices are already deleted.", lambda: None, False)]
        PopupMenu(self, items, widget.winfo_rootx() + widget.winfo_width(),
                  widget.winfo_rooty() + widget.winfo_height() + 4, align_right=True)

    def delete_audio(self, folder, recording, samples):
        from . import edit, voices
        if not self._can_edit(folder):
            return
        meeting = output.load_meeting(folder)
        what = {(True, False): "the recording", (False, True): "the speaker voices",
                (True, True): "the recording and the speaker voices"}[(recording, samples)]
        text = f"Delete {what} of \"{meeting['name']}\"? The transcript and the minutes stay.\n\n"
        if recording:
            text += "You can no longer listen to the meeting, redo the speakers or save a voice from it. "
            source = Path(meeting.get("source_audio", ""))
            if meeting.get("source_audio") and source.is_file():
                text += f"The file you imported ({source.name}) is not deleted. "
        if samples:
            text += "The Speakers tab can no longer play each person's voice. "
        saved = voices.voices_in_meeting(self.cfg, folder, meeting) if samples else []
        forget = []
        if saved:
            names = ", ".join(v["name"] for v in saved)
            one = len(saved) == 1
            text += (f"\n\n{names} {'is' if one else 'are'} also saved in memory and named automatically in later "
                     f"meetings. Delete {'this voice' if one else 'these voices'} from memory too?\n\n"
                     "Yes: delete from memory as well\nNo: keep in memory")
            answer = messagebox.askyesnocancel("Delete " + what, text.strip(), icon="warning", parent=self)
            if answer is None:
                return
            forget = saved if answer else []
        elif not messagebox.askyesno("Delete " + what, text.strip(), icon="warning", parent=self):
            return
        if self.viewer_open and self.viewer.folder == Path(folder):
            self.viewer.stop_audio()  # the player keeps the wav open, and Windows cannot delete an open file
        freed = 0
        try:
            if recording:
                freed += edit.delete_recording(folder)
            if samples:
                freed += edit.delete_voice_samples(folder)
            for v in forget:
                voices.delete_voice(self.cfg, v["name"])
        except OSError as e:
            messagebox.showerror("Delete " + what, f"Could not delete everything:\n{e}", parent=self)
        self._after_edit(folder)
        done = f"Deleted {what} ({freed / 1e6:.0f} MB)."
        if forget:
            done += " Forgot the saved voice" + ("s of " if len(forget) > 1 else " of ") + \
                ", ".join(v["name"] for v in forget) + "."
        messagebox.showinfo("Delete " + what, done, parent=self)

    def sentence_menu(self, folder, index, x, y):
        """Opened by clicking a sentence in the Transcript tab."""
        from . import edit
        meeting = output.load_meeting(folder)

        def set_speaker(indexes, speaker):
            if self._can_edit(folder):
                edit.set_speaker(folder, indexes, speaker)
                self._after_edit(folder)

        def fix_text():
            seg = meeting["segments"][index]

            def save(values):
                if values[0].strip() and self._can_edit(folder):
                    edit.set_text(folder, index, values[0])
                    self._after_edit(folder)
            PromptPopup(self, ("Fix the text", [("", seg["text"])], save, "Save"), x, y)
        SpeakerPicker(self, (meeting, index, set_speaker, fix_text), x, y)

    def run_job(self, args, name, folder):
        """Add a job for a meeting to the processing queue. Returns False if that
        meeting already has a job waiting or running."""
        if self.jobs.has(folder):
            messagebox.showinfo("Busy", "This meeting is already being processed or waiting to be.", parent=self)
            return False
        self.jobs.add(args, name, folder)
        return True

    def delete_meeting(self, item):
        if not self._can_edit(item["folder"]):
            return
        if messagebox.askyesno("Delete meeting", f"Delete \"{item['name']}\" and all its files?",
                               icon="warning", parent=self):
            if self.viewer_open and self.viewer.folder == item["folder"]:
                self.close_viewer()
            shutil.rmtree(item["folder"], ignore_errors=True)
            self.refresh_list()

    # ----- meeting list -----
    def refresh_list(self):
        for child in self.list_frame.winfo_children():
            child.destroy()
        self.rows = {}
        items = list_meetings(self.cfg)
        if not items:
            ctk.CTkLabel(self.list_frame, text="No meetings yet.\nPress Record, or import a file.",
                         font=self.f_small, text_color=MUTED, justify="center").pack(pady=30)
        for item in items:
            self._meeting_row(item)
        self.highlight_row(self.viewer.folder if self.viewer_open else None)

    def _meeting_row(self, item):
        row = ctk.CTkFrame(self.list_frame, fg_color=CARD, corner_radius=12, border_width=1, border_color=LINE)
        row.pack(fill="x", pady=3, padx=4)
        self.rows[str(item["folder"])] = row
        # Packed before the text: pack hands out space in packing order, so a long
        # name would otherwise push the button out of the row.
        more = IconButton(row, self, ICONS["more"], "", None, text_color=MUTED, pad=8)
        more.command = lambda: self.meeting_menu(item, more)
        more.pack(side="right", padx=(0, 8))
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, padx=(12, 4), pady=8)
        name = FittedName(text, item["name"], self.f_bold, INK)
        name.pack(fill="x")
        try:
            when = datetime.strptime(item["date"], "%Y-%m-%d %H:%M").strftime("%d %b %H:%M")
        except ValueError:
            when = item["date"]
        meta = f"{when} {MIDDOT} {fmt_duration(item['duration_s'])}"
        if item["processed"]:
            meta += f" {MIDDOT} {item['speakers']} speaker{'s' if item['speakers'] != 1 else ''}"
        elif self.jobs.has(item["folder"]):
            meta += f" {MIDDOT} in the processing queue"
        else:
            meta += f" {MIDDOT} not processed"
        meta_label = ctk.CTkLabel(text, text=meta, font=self.f_small, text_color=MUTED, anchor="w")
        meta_label.pack(fill="x")

        if item["processed"]:
            # Clicking the meeting opens its minutes in the panel.
            open_minutes = lambda e: self.open_viewer(item["folder"], "minutes")
            for widget in (row, text, meta_label):
                widget.bind("<ButtonRelease-1>", open_minutes)
                widget.configure(cursor="hand2")
            name.bind_all_parts("<ButtonRelease-1>", open_minutes)

    # ----- status updates -----
    def show_card(self, name):
        if self.shown_state == name:
            return
        for frame in (self.idle, self.recording):
            frame.pack_forget()
        {"idle": self.idle, "recording": self.recording}[name].pack(fill="x")
        self.shown_state = name

    def tick(self):
        self._handle_tray()
        if self.show_requested.is_set():
            self.show_requested.clear()
            self.show_window()
        self._tick_recording()
        self._tick_jobs()
        self.after(100, self.tick)

    def _tick_recording(self):
        st = self.rec.status
        recording = st["state"] == "recording"
        if self.tray:
            self.tray.set_recording(recording)
        if not recording:
            self.show_card("idle")
            return
        if self.shown_state != "recording":
            self.show_card("recording")
            self.rec_name.configure(text=st.get("name", ""))
            system_row = self.meters["system"][0]
            if st.get("mic_only"):
                system_row.pack_forget()
            else:
                system_row.pack(fill="x", padx=18, pady=3, after=self.meters["mic"][0])
        elapsed = st.get("elapsed", 0)
        self.timer.configure(text=fmt_duration(elapsed))
        for key, (row, bar) in self.meters.items():
            bar.set(st.get("levels", {}).get(key, 0))
        # The red dot blinks once per second.
        self.rec_dot.configure(text_color=RED if int(elapsed * 2) % 2 == 0 else CARD)

    def _tick_jobs(self):
        if self.shown_jobs != self.jobs.version:
            self.shown_jobs = self.jobs.version
            self._render_jobs()
            self.refresh_list()  # rows show "waiting" / "processing", and finished meetings
            with self.jobs.lock:
                new = [job for job in self.jobs.finished if job["id"] not in self.notified]
            for job in new:
                self.notified.add(job["id"])
                folder = Path(job["folder"]) if job["folder"] else None
                if self.viewer_open and folder and self.viewer.folder == folder:
                    self.viewer.reload()
                if self.tray and self.state() == "withdrawn":
                    self.tray.notify(f"{'Ready' if job['state'] == 'done' else 'Failed'}: {job['name']}")
        current = self.jobs.current
        if current:
            self._update_progress(current)

    # ----- tray and closing -----
    def _handle_tray(self):
        if not self.tray:
            return
        while True:
            try:
                action = self.tray.queue.get_nowait()
            except queue.Empty:
                return
            if action == "show":
                self.show_window()
            elif action == "record":
                self.toggle_record()
            elif action == "quit":
                self.quit_app()

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self.after(100, self.apply_window_flags)

    def tuck_away(self):
        if not self.tray:
            self.iconify()
            return
        self.viewer.stop_audio()
        self.withdraw()
        if not self.told_about_tray:
            self.told_about_tray = True
            self.tray.notify("Still running here. Click the icon to open, right-click to record or quit.")

    def on_close(self):
        """The window's close button tucks the app away in the tray (if there is one)."""
        if self.tray:
            self.tuck_away()
        else:
            self.quit_app()

    def quit_app(self):
        recording, processing = self.rec.recording(), self.jobs.busy()
        if recording or processing:
            self.show_window()
        if recording and not messagebox.askyesno("Recording", "Stop the recording and quit?\n"
                                                 "It is saved and can be processed later.", parent=self):
            return
        if processing and not messagebox.askyesno(
                "Processing", "Processing is still running. Stop it and quit?\n"
                "Recordings that are not processed yet stay in the list and can be processed later.", parent=self):
            return
        if recording:
            self.rec.quitting = True
            self.rec.stop()
            if self.rec.thread:
                self.rec.thread.join(timeout=5)
        self.jobs.stop()
        self.viewer.stop_audio()
        if self.tray:
            self.tray.stop()
        self.destroy()


def run(cfg, config_path=None):
    handle = claim_single_instance()
    if handle is None:
        return  # already running: that window comes to the front instead
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        # Our own taskbar identity, so the taskbar shows our icon and not Python's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LokalProtokoll")
    except (AttributeError, OSError):
        pass
    ctk.set_appearance_mode("system")
    window = App(cfg, config_path)
    watch_for_second_start(handle, window.show_requested)
    window.mainloop()
