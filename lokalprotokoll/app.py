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
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import output, recorder
from .config import PROJECT_DIR, resolve
from .theme import (BG, CARD, CHECK, DOT, INK, INK_HOVER, LINE, MIDDOT, MUTED, OTHERS, RED, RED_HOVER,
                    SQUARE, YOU, fmt_duration, restyle_segments, speaker_color)
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
         "rename": "\ue8ac", "redo": "\ue895"}

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


# ---------------------------------------------------------------- background work

class Worker:
    """Runs one job at a time in a background thread: a recording or an lp.py
    command. The window reads self.status regularly to show progress."""

    def __init__(self, cfg, config_path):
        self.cfg = cfg
        self.config_path = config_path
        self.status = {"state": "idle"}
        self.stop_event = threading.Event()
        self.proc = None
        self.thread = None
        self.quitting = False

    def busy(self):
        return self.status["state"] in ("recording", "processing")

    def start_recording(self, name, num_speakers, mic_only):
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

    def stop_recording(self):
        self.stop_event.set()

    def _record(self, out_dir, name, num_speakers, mic_only, auto_name):
        rec = self.cfg["record"]
        try:
            info = recorder.record(out_dir, name, rec.get("mic_device", ""), rec.get("speaker_device", ""),
                                   mic_only=mic_only, stop_event=self.stop_event, status=self.status,
                                   auto_name=auto_name)
        except (Exception, SystemExit) as e:
            shutil.rmtree(out_dir, ignore_errors=True)
            self.status = {"state": "error", "name": name, "message": str(e), "log": [str(e)], "folder": ""}
            return
        if info["duration_s"] < 2:
            shutil.rmtree(out_dir, ignore_errors=True)
            self.status = {"state": "idle"}
            return
        if self.quitting:
            return
        args = ["process", str(out_dir)]
        if num_speakers:
            args += ["--speakers", str(num_speakers)]
        self._run_cli(args, name, out_dir)

    def run(self, args, name, folder=None):
        self.status = {"state": "processing", "name": name, "folder": str(folder or ""),
                       "stage": "Starting", "step": 0, "log": []}
        self.thread = threading.Thread(target=self._run_cli, args=(args, name, folder), daemon=True)
        self.thread.start()

    def _run_cli(self, args, name, folder):
        status = {"state": "processing", "name": name, "folder": str(folder or ""),
                  "stage": "Starting", "step": 0, "log": []}
        self.status = status
        python = sys.executable.replace("pythonw.exe", "python.exe")
        cmd = [python, str(PROJECT_DIR / "lp.py")]
        if self.config_path:
            cmd += ["--config", str(self.config_path)]
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(cmd + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     cwd=PROJECT_DIR, env=env,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for raw in self.proc.stdout:
            for line in raw.decode("utf-8", errors="replace").replace("\r", "\n").splitlines():
                if not line.strip():
                    continue
                status["log"] = (status["log"] + [line.rstrip()])[-400:]
                m = re.fullmatch(r"\[([a-z ]+)\]", line.strip())
                if m:
                    step, _, detail = m.group(1).partition(" ")
                    if step in STEPS:
                        status["step"] = STEPS.index(step)
                        status["stage"] = f"{STEP_LABELS[step]} ({detail})" if detail else STEP_LABELS[step]
                if line.startswith("Output: "):
                    status["folder"] = line[len("Output: "):].strip()
        self.proc.wait()
        if status["folder"] and Path(status["folder"]).is_dir():
            Path(status["folder"], "process.log").write_text("\n".join(status["log"]), encoding="utf-8")
        if self.proc.returncode == 0:
            self.status = dict(status, state="done")
        else:
            tail = [line for line in status["log"] if "progress =" not in line][-6:]
            self.status = dict(status, state="error", message="\n".join(tail) or "Failed")
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
        self.win = ctk.CTkToplevel(self.app)
        self.win.overrideredirect(True)
        self.win.transient(self.app)
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
        self.worker = Worker(cfg, config_path)
        self.shown_state = None
        self.viewer_open = False
        self.closed_width = LEFT_WIDTH
        self.rows = {}
        self.told_about_tray = False

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
        self._build_processing()
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
        self.bind("<Escape>", lambda e: self.close_viewer() if self.viewer_open else None)
        self.after(200, self.apply_window_flags)
        self.refresh_list()
        self.tick()

    def _set_window_icon(self):
        try:
            path = Path(tempfile.gettempdir()) / "lokalprotokoll.ico"
            app_icon_image(256).save(path, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
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

    def _build_processing(self):
        f = self.processing = ctk.CTkFrame(self.card, fg_color="transparent")
        self.proc_name = ctk.CTkLabel(f, text="", font=self.f_title, text_color=INK, anchor="w")
        self.proc_name.pack(fill="x", padx=16, pady=(16, 0))
        self.proc_stage = ctk.CTkLabel(f, text="", font=self.f_body, text_color=MUTED, anchor="w", justify="left")
        self.proc_stage.pack(fill="x", padx=16)
        self.proc_bar = ctk.CTkProgressBar(f, height=6, corner_radius=3, progress_color=INK, fg_color=LINE)
        self.proc_bar.pack(fill="x", padx=16, pady=(10, 6))
        self.proc_line = ctk.CTkLabel(f, text="", font=self.f_mono, text_color=MUTED, anchor="w")
        self.proc_line.pack(fill="x", padx=16, pady=(0, 14))
        # Only packed when it has buttons (an empty frame would still be 200 px high).
        self.proc_buttons = ctk.CTkFrame(f, fg_color="transparent", height=0)

    def _build_list(self):
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

    def open_viewer(self, folder=None, view="minutes", log=None):
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
        else:
            self.viewer.open(folder, view)
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
        state = self.worker.status["state"]
        if state == "recording":
            self.worker.stop_recording()
        elif not self.worker.busy():
            name = self.name_entry.get().strip()
            value = self.speakers_choice
            self.worker.start_recording(name, 0 if value == "Auto" else int(value), self.mic_only.get())

    def import_file(self):
        if self.worker.busy():
            messagebox.showinfo("Busy", "Wait until the current job is finished.", parent=self)
            return
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
        self.worker.run(args, self.name_entry.get().strip() or Path(path).stem)

    def meeting_menu(self, item, widget):
        folder = item["folder"]
        if item["processed"]:
            items = [(ICONS["minutes"], "View minutes", lambda: self.open_viewer(folder, "minutes"), False),
                     (ICONS["transcript"], "View transcript", lambda: self.open_viewer(folder, "transcript"), False),
                     (ICONS["speakers"], "Name speakers", lambda: self.open_viewer(folder, "speakers"), False),
                     (ICONS["rewrite"], "Rewrite minutes", lambda: self.run_job(["summarize", str(folder)], item), False),
                     None,
                     (ICONS["rename"], "Rename...", lambda: self.rename_meeting(item, widget), False),
                     (ICONS["redo"], "Redo speakers...", lambda: self.redo_speakers(item, widget), False),
                     (ICONS["find"], "Find and replace...", lambda: self.find_replace(item, widget), False)]
        else:
            items = [(ICONS["play"], "Process now", lambda: self.run_job(["process", str(folder)], item), False)]
        items += [None,
                  (ICONS["folder"], "Open folder", lambda: open_path(folder), False),
                  (ICONS["delete"], "Delete", lambda: self.delete_meeting(item), True)]
        PopupMenu(self, items, widget.winfo_rootx() - 150, widget.winfo_rooty() + widget.winfo_height() + 4)

    # ----- corrections (lokalprotokoll/edit.py; quick, so they run here directly) -----
    def _can_edit(self, folder):
        if self.worker.busy() and self.worker.status.get("folder") == str(folder):
            messagebox.showinfo("Busy", "This meeting is being processed. Wait until it is finished.", parent=self)
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

    def redo_speakers(self, item, widget):
        folder = item["folder"]

        def run(choice):
            meeting = output.load_meeting(folder)
            if any(k != "0" for k in meeting.get("speaker_names", {})) and not messagebox.askyesno(
                    "Redo speakers", "The speaker names you gave will be removed, because the speakers are "
                    "numbered again. Continue?", parent=self):
                return
            args = ["rediarize", str(folder), "--summarize", "--speakers", "0" if choice == "Auto" else str(choice)]
            self.run_job(args, item)
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

    def run_job(self, args, item):
        if self.worker.busy():
            messagebox.showinfo("Busy", "Wait until the current job is finished.", parent=self)
            return
        self.worker.run(args, item["name"], item["folder"])

    def delete_meeting(self, item):
        if self.worker.busy() and self.worker.status.get("folder") == str(item["folder"]):
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
        text = ctk.CTkFrame(row, fg_color="transparent")
        text.pack(side="left", fill="x", expand=True, padx=(12, 4), pady=8)
        name = ctk.CTkLabel(text, text=item["name"], font=self.f_bold, text_color=INK, anchor="w")
        name.pack(fill="x")
        try:
            when = datetime.strptime(item["date"], "%Y-%m-%d %H:%M").strftime("%d %b %H:%M")
        except ValueError:
            when = item["date"]
        meta = f"{when} {MIDDOT} {fmt_duration(item['duration_s'])}"
        if item["processed"]:
            meta += f" {MIDDOT} {item['speakers']} speaker{'s' if item['speakers'] != 1 else ''}"
        else:
            meta += f" {MIDDOT} not processed"
        meta_label = ctk.CTkLabel(text, text=meta, font=self.f_small, text_color=MUTED, anchor="w")
        meta_label.pack(fill="x")

        more = IconButton(row, self, ICONS["more"], "", None, text_color=MUTED, pad=8)
        more.command = lambda: self.meeting_menu(item, more)
        more.pack(side="right", padx=(0, 8))
        if item["processed"]:
            # Clicking the meeting opens its minutes in the panel.
            for widget in (row, text, name, meta_label):
                widget.bind("<ButtonRelease-1>", lambda e: self.open_viewer(item["folder"], "minutes"))
                widget.configure(cursor="hand2")

    # ----- status updates -----
    def show_card(self, name):
        if self.shown_state == name:
            return
        for frame in (self.idle, self.recording, self.processing):
            frame.pack_forget()
        if name == "processing":
            self.proc_stage.configure(text_color=MUTED)
        {"idle": self.idle, "recording": self.recording, "processing": self.processing}[name].pack(fill="x")
        self.shown_state = name

    def tick(self):
        self._handle_tray()
        st = self.worker.status
        state = st["state"]
        if self.tray:
            self.tray.set_recording(state == "recording")
        if state == "recording":
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
        elif state == "processing":
            self.show_card("processing")
            self.proc_name.configure(text=st.get("name", ""))
            self.proc_stage.configure(text=st.get("stage", "") + "...")
            # whisper and diarization print "progress = 52%"; use it inside the current step.
            last = next((line for line in reversed(st.get("log", [])) if line.strip()), "")
            m = re.search(r"progress =\s*(\d+)%", last)
            within = int(m.group(1)) / 100 if m else 0.1
            self.proc_bar.set((st.get("step", 0) + within) / len(STEPS))
            self.proc_line.configure(text=f"{m.group(1)}%" if m else last.strip()[:52])
            if self.proc_buttons.winfo_children():
                self._set_proc_buttons([])
        elif state in ("done", "error"):
            if self.shown_state != state:
                self.show_card("processing")
                self.shown_state = state
                self.refresh_list()
                self._show_result(st)
                folder = Path(st["folder"]) if st.get("folder") else None
                if self.viewer_open and folder and self.viewer.folder == folder:
                    self.viewer.reload()
                if self.tray and self.state() == "withdrawn":
                    self.tray.notify(f"{'Ready' if state == 'done' else 'Failed'}: {self.proc_name.cget('text')}")
        else:
            self.show_card("idle")
        self.after(100, self.tick)

    def _show_result(self, st):
        folder = Path(st["folder"]) if st.get("folder") else None
        name = st.get("name", "")
        if folder and (folder / "meeting.json").exists():
            # The meeting may have got a title from its minutes while processing.
            try:
                name = output.load_meeting(folder)["name"]
            except (SystemExit, ValueError, KeyError):
                pass
        self.proc_name.configure(text=name)
        if st["state"] == "done":
            self.proc_stage.configure(text=f"{CHECK}  Done", text_color=YOU)
            self.proc_bar.set(1)
            self.proc_line.configure(text="")
            buttons = []
            if folder and (folder / "summary.md").exists():
                buttons.append(("Minutes", lambda: self.open_viewer(folder, "minutes")))
            if folder and (folder / "meeting.json").exists():
                buttons.append(("Speakers", lambda: self.open_viewer(folder, "speakers")))
        else:
            self.proc_stage.configure(text="Something went wrong", text_color=RED)
            lines = st.get("message", "").splitlines()
            self.proc_line.configure(text=lines[-1][:52] if lines else "")
            log = st.get("message", "") + "\n\n" + "\n".join(st.get("log", []))
            buttons = [("Show log", lambda: self.open_viewer(log=(f"Log - {st.get('name', '')}", log)))]
        buttons.append(("OK", self.dismiss))
        self._set_proc_buttons(buttons)

    def _set_proc_buttons(self, buttons):
        for child in self.proc_buttons.winfo_children():
            child.destroy()
        if not buttons:
            self.proc_buttons.pack_forget()
            return
        self.proc_buttons.pack(fill="x", padx=12, pady=(0, 14))
        for i, (text, command) in enumerate(buttons):
            last = i == len(buttons) - 1
            ctk.CTkButton(self.proc_buttons, text=text, width=10, height=34, corner_radius=10, font=self.f_small,
                          fg_color=INK if last else BG, hover_color=INK_HOVER if last else LINE,
                          text_color=BG if last else INK, border_width=0 if last else 1, border_color=LINE,
                          command=command).pack(side="left", expand=True, fill="x", padx=4)

    def dismiss(self):
        self.worker.status = {"state": "idle"}

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
        state = self.worker.status["state"]
        if state in ("recording", "processing"):
            self.show_window()
        if state == "recording":
            if not messagebox.askyesno("Recording", "Stop the recording and quit?\n"
                                       "It is saved and can be processed later.", parent=self):
                return
            self.worker.quitting = True
            self.worker.stop_recording()
            if self.worker.thread:
                self.worker.thread.join(timeout=5)
        elif state == "processing":
            if not messagebox.askyesno("Processing", "Processing is still running. Stop it and quit?", parent=self):
                return
            if self.worker.proc:
                self.worker.proc.terminate()
        self.viewer.stop_audio()
        if self.tray:
            self.tray.stop()
        self.destroy()


def run(cfg, config_path=None):
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass
    ctk.set_appearance_mode("system")
    App(cfg, config_path).mainloop()
