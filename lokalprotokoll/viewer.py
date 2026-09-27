"""The panel on the right of the main window: minutes, transcript, speakers, log.

Minutes (summary.md) are shown with simple Markdown styling: headings, bullet
lists, checkboxes, bold and italic. That is all the summary prompt produces.
"""

import os
import re
import wave
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import messagebox

import customtkinter as ctk

from . import output, speakers
from .theme import (BG, BOX, BOX_CHECKED, BULLET, CARD, CHECK, CLOSE, INK, INK_HOVER, LINE, MIDDOT, MUTED, PLAY,
                    SQUARE, fmt_duration, pick, restyle_segments, speaker_color)

STALE = ("#F3E5C4", "#3A3222")  # soft amber: "these minutes are out of date"
INLINE = re.compile(r"\*\*[^*\n]+\*\*|`[^`\n]+`|(?<![\w*])[*_][^*_\n]+[*_](?![\w*])")
TABS = {"Minutes": "minutes", "Transcript": "transcript", "Speakers": "speakers"}


def play_sound(path=None):
    """Play a wav file in the background, or stop playback when path is None."""
    try:
        import winsound
        if path is None:
            winsound.PlaySound(None, 0)
        elif Path(path).exists():
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except (ImportError, RuntimeError):
        pass


class Viewer(ctk.CTkFrame):
    def __init__(self, app):
        super().__init__(app, fg_color=CARD, corner_radius=16, border_width=1, border_color=LINE)
        self.app = app
        self.folder = None
        self.meeting = None
        self.view = None
        self.speaker_panel = None

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=(22, 12), pady=(14, 0))
        self.title_label = ctk.CTkLabel(head, text="", font=app.f_view_title, text_color=INK, anchor="w")
        self.title_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(head, text=CLOSE, width=30, height=30, corner_radius=8, font=app.f_body,
                      fg_color="transparent", hover_color=LINE, text_color=MUTED,
                      command=app.close_viewer).pack(side="right")
        self.meta_label = ctk.CTkLabel(self, text="", font=app.f_small, text_color=MUTED, anchor="w")
        self.meta_label.pack(fill="x", padx=22)

        self.bar = ctk.CTkFrame(self, fg_color="transparent")
        self.bar.pack(fill="x", padx=20, pady=(10, 8))
        self.tabs = ctk.CTkSegmentedButton(
            self.bar, values=list(TABS), font=app.f_small, height=28, selected_color=INK,
            selected_hover_color=INK_HOVER, unselected_color=BG, unselected_hover_color=LINE, fg_color=BG,
            command=lambda value: self.show(TABS[value]))
        self.tabs.pack(side="left")
        self.open_btn = app.icon_button(self.bar, "open", "Open file", self.open_file)
        self.copy_btn = app.icon_button(self.bar, "copy", "Copy", self.copy)
        self.edit_btn = app.icon_button(self.bar, "edit", "Edit", self.start_editing)
        # While editing the minutes, Save and Cancel replace the buttons above.
        self.save_btn = ctk.CTkButton(self.bar, text="Save", width=80, height=28, corner_radius=8, font=app.f_small,
                                      fg_color=INK, hover_color=INK_HOVER, text_color=BG, command=self.save_editing)
        self.cancel_btn = ctk.CTkButton(self.bar, text="Cancel", width=80, height=28, corner_radius=8,
                                        font=app.f_small, fg_color=BG, hover_color=LINE, text_color=INK,
                                        border_width=1, border_color=LINE, command=lambda: self.show("minutes"))
        self.editing = False

        # Shown above the minutes when speakers or text changed after they were written.
        self.banner = ctk.CTkFrame(self, fg_color=STALE, corner_radius=10)
        ctk.CTkLabel(self.banner, text="Speakers or text changed after these minutes were written.",
                     font=app.f_small, text_color=INK, anchor="w").pack(side="left", padx=12, pady=6)
        ctk.CTkButton(self.banner, text="Rewrite minutes", width=120, height=28, corner_radius=8, font=app.f_small,
                      fg_color=INK, hover_color=INK_HOVER, text_color=BG,
                      command=self.rewrite_minutes).pack(side="right", padx=8, pady=6)

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=(4, 8), pady=(0, 12))
        self.text = tk.Text(self.body, wrap="word", relief="flat", borderwidth=0, highlightthickness=0,
                            padx=20, pady=6, cursor="arrow", undo=False)
        self.scroll = ctk.CTkScrollbar(self.body, command=self.text.yview, button_color=LINE,
                                       button_hover_color=MUTED)
        self.text.configure(yscrollcommand=self.scroll.set)
        self._make_fonts()

    def _make_fonts(self):
        body = "Segoe UI Variable Text"
        self.fonts = {
            "body": tkfont.Font(family=body, size=11),
            "bold": tkfont.Font(family=body, size=11, weight="bold"),
            "italic": tkfont.Font(family=body, size=11, slant="italic"),
            "h1": tkfont.Font(family="Bahnschrift", size=16, weight="bold"),
            "h2": tkfont.Font(family="Bahnschrift", size=13, weight="bold"),
            "h3": tkfont.Font(family="Bahnschrift", size=11, weight="bold"),
            "mono": tkfont.Font(family="Cascadia Mono", size=9),
            "gap": tkfont.Font(family=body, size=4),
        }

    def _style_text(self):
        """(Re)apply colors and tags, so the text follows light/dark mode."""
        t, f = self.text, self.fonts
        t.configure(bg=pick(CARD), fg=pick(INK), font=f["body"], insertbackground=pick(INK),
                    selectbackground=pick(LINE), selectforeground=pick(INK), spacing1=1, spacing3=1)
        t.tag_configure("h1", font=f["h1"], spacing1=10, spacing3=6)
        t.tag_configure("h2", font=f["h2"], spacing1=16, spacing3=6)
        t.tag_configure("h3", font=f["h3"], spacing1=10, spacing3=4)
        t.tag_configure("p", spacing3=6)
        # List items: marker, tab, text. The tab stop is also the indent of wrapped
        # lines, so they line up under the text instead of under the marker.
        marker_width = max(f["body"].measure(m) for m in (BOX, BOX_CHECKED, BULLET, "10.")) + f["body"].measure("  ")
        for level in range(4):
            start = 6 + 22 * level
            t.tag_configure(f"li{level}", lmargin1=start, lmargin2=start + marker_width,
                            tabs=(start + marker_width,), spacing3=5)
        t.tag_configure("marker", foreground=pick(MUTED))
        t.tag_configure("bold", font=f["bold"])
        t.tag_configure("italic", font=f["italic"], foreground=pick(MUTED))
        t.tag_configure("code", font=f["mono"])
        t.tag_configure("gap", font=f["gap"])
        t.tag_configure("hr", font=f["gap"], spacing1=8, spacing3=8)
        t.tag_configure("who", font=f["bold"], spacing1=12)
        t.tag_configure("ts", font=f["mono"], foreground=pick(MUTED))
        t.tag_configure("para", spacing1=2, spacing3=4)
        t.tag_configure("log", font=f["mono"], spacing1=0, spacing3=0)
        t.tag_configure("hint", font=f["italic"], foreground=pick(MUTED), spacing3=4)
        t.tag_configure("match", font=f["bold"], background=pick(STALE))
        t.tag_configure("focus", background=pick(STALE))
        for n in range(12):
            t.tag_configure(f"spk{n}", foreground=pick(speaker_color(n)))

    # ----- opening -----
    def open(self, folder, view="minutes"):
        self.folder = Path(folder)
        self.meeting = output.load_meeting(self.folder)
        m = self.meeting
        count = len({s.get("speaker") for s in m["segments"] if s.get("speaker") is not None})
        try:
            from datetime import datetime
            when = datetime.strptime(m["date"], "%Y-%m-%d %H:%M").strftime("%d %b %Y, %H:%M")
        except ValueError:
            when = m["date"]
        self.title_label.configure(text=m["name"])
        self.meta_label.configure(text=f"{when}  {MIDDOT}  {fmt_duration(m['duration_s'])}  {MIDDOT}  "
                                       f"{count} speaker{'s' if count != 1 else ''}  {MIDDOT}  {m['language']}")
        self.bar.pack(fill="x", padx=20, pady=(10, 8), after=self.meta_label)
        self.show(view)

    def stop_audio(self):
        """Stop a playing voice sample (and reset its stop button to play)."""
        if self.speaker_panel is not None:
            self.speaker_panel.stop_playing()
        else:
            play_sound(None)

    def reload(self, keep_scroll=False):
        """Show the meeting again after a change. keep_scroll keeps the reading position
        (after correcting a sentence in the transcript, for example)."""
        if self.folder and (self.folder / "meeting.json").exists():
            position = self.text.yview()[0] if keep_scroll else 0
            self.open(self.folder, self.view if self.view in TABS.values() else "minutes")
            self.text.yview_moveto(position)

    def _bar_buttons(self, *buttons):
        for b in (self.open_btn, self.copy_btn, self.edit_btn, self.save_btn, self.cancel_btn):
            b.pack_forget()
        for b in buttons:
            b.pack(side="right", padx=(6, 0))

    def show(self, view):
        play_sound(None)
        self.view = view
        self.editing = False
        self.tabs.set(next(k for k, v in TABS.items() if v == view))
        restyle_segments(self.tabs)
        if self.speaker_panel is not None:
            self.speaker_panel.destroy()
            self.speaker_panel = None
        stale = view == "minutes" and self.meeting.get("summary_stale") and (self.folder / "summary.md").exists()
        if stale:
            self.banner.pack(fill="x", padx=20, pady=(0, 8), before=self.body)
        else:
            self.banner.pack_forget()
        if view == "speakers":
            self.text.pack_forget()
            self.scroll.pack_forget()
            self.speaker_panel = SpeakerPanel(self.body, self.app, self.folder, self.meeting)
            self.speaker_panel.pack(fill="both", expand=True, padx=(12, 0))
            self._bar_buttons()
            return
        self._show_text()
        if view == "minutes":
            self._bar_buttons(self.open_btn, self.copy_btn, self.edit_btn)
            path = self.folder / "summary.md"
            if path.exists():
                render_markdown(self.text, path.read_text(encoding="utf-8"))
            else:
                self._message("No minutes yet. Use \"Rewrite minutes\" in the meeting menu.")
        else:
            self._bar_buttons(self.open_btn, self.copy_btn)
            self.text.insert("end", "Click a sentence to change who said it or to fix the text.\n", ("hint",))
            render_transcript(self.text, self.meeting, self._sentence_clicked)
        self.text.configure(state="disabled")
        self.text.yview_moveto(0)

    def _sentence_clicked(self, index, event):
        self.app.sentence_menu(self.folder, index, event.x_root - 30, event.y_root + 14)

    # ----- editing the minutes by hand -----
    def start_editing(self):
        path = self.folder / "summary.md"
        if not path.exists():
            return
        self.editing = True
        self.banner.pack_forget()
        self._show_text()
        self.text.configure(font=self.fonts["body"], cursor="xterm", undo=True)
        self.text.insert("1.0", path.read_text(encoding="utf-8"))
        self.text.edit_reset()
        self.text.focus_set()
        self._bar_buttons(self.save_btn, self.cancel_btn)

    def save_editing(self):
        from .edit import save_minutes
        save_minutes(self.folder, self.text.get("1.0", "end"))
        self.reload()

    def rewrite_minutes(self):
        if self.app.worker.busy():
            messagebox.showinfo("Busy", "Wait until the current job is finished.", parent=self.app)
            return
        self.app.worker.run(["summarize", str(self.folder)], self.meeting["name"], self.folder)

    def show_log(self, title, text):
        """Show plain text (a processing log) in the panel, without tabs."""
        play_sound(None)
        self.folder, self.meeting, self.view = None, None, "log"
        if self.speaker_panel is not None:
            self.speaker_panel.destroy()
            self.speaker_panel = None
        self.title_label.configure(text=title)
        self.meta_label.configure(text="Output of the last job")
        self.bar.pack_forget()
        self._show_text()
        self.text.insert("end", text, ("log",))
        self.text.configure(state="disabled")
        self.text.yview_moveto(1)

    def show_search(self, query, results):
        """Search results from all meetings. Clicking a hit opens the meeting there."""
        from .search import snippet, terms_of
        play_sound(None)
        self.folder, self.meeting, self.view = None, None, "search"
        if self.speaker_panel is not None:
            self.speaker_panel.destroy()
            self.speaker_panel = None
        self.banner.pack_forget()
        places = sum(len(r["sentences"]) + len(r["minutes"]) for r in results)
        self.title_label.configure(text=f"Search: {query}")
        self.meta_label.configure(text=f"{places} place{'s' if places != 1 else ''} in {len(results)} "
                                       f"meeting{'s' if len(results) != 1 else ''}")
        self.bar.pack_forget()
        self._show_text()
        t = self.text
        if not results:
            self._message("Nothing found. All the words must be in the same sentence; \"quotes\" search for a phrase.")
        terms = terms_of(query)
        n = 0

        def link(text, tags, action):
            nonlocal n
            n += 1
            tag = f"hit{n}"
            start = t.index("end-1c")
            _insert_highlighted(t, text, terms, tags + (tag,))
            t.tag_bind(tag, "<Enter>", lambda e: (t.tag_configure(tag, background=pick(LINE)), t.configure(cursor="hand2")))
            t.tag_bind(tag, "<Leave>", lambda e: (t.tag_configure(tag, background=""), t.configure(cursor="arrow")))
            t.tag_bind(tag, "<ButtonRelease-1>", lambda e: action())
            return start

        for r in results:
            folder = r["folder"]
            link(r["name"], ("h2",), lambda f=folder: self.app.open_viewer(f, "minutes"))
            t.insert("end", "\n")
            t.insert("end", r["date"] + "\n", ("ts",))
            for line in r["minutes"]:
                t.insert("end", "Minutes:  ", ("marker", "li0"))
                link(snippet(line, terms), ("li0",), lambda f=folder: self.app.open_viewer(f, "minutes"))
                t.insert("end", "\n", ("li0",))
            for s in r["sentences"]:
                who = f"{s['speaker']}: " if s["speaker"] else ""
                t.insert("end", output.fmt_time(s["start"]) + "  ", ("ts", "li0"))
                link(who + snippet(s["text"], terms), ("li0",),
                     lambda f=folder, i=s["index"]: self.app.open_viewer(f, "transcript", focus=i))
                t.insert("end", "\n", ("li0",))
            if r["more"]:
                t.insert("end", f"... and {r['more']} more sentences in this meeting\n", ("italic", "li0"))
        t.configure(state="disabled")
        t.yview_moveto(0)

    def focus_sentence(self, index):
        """Scroll the transcript to a sentence and highlight it for a few seconds."""
        tag = f"seg{index}"
        ranges = self.text.tag_ranges(tag)
        if not ranges:
            return
        self.text.tag_add("focus", ranges[0], ranges[1])
        self.text.tag_raise("focus")

        def scroll():
            # The text was just drawn: wait for the layout, then put the sentence about
            # a third of the way down the view.
            self.text.update_idletasks()
            line = int(str(ranges[0]).split(".")[0])
            total = int(self.text.index("end-1c").split(".")[0])
            first, last = self.text.yview()
            self.text.yview_moveto(max(0.0, (line - 1) / max(total, 1) - (last - first) / 3))
            self.text.see(ranges[0])
        self.after(30, scroll)
        self.after(4000, lambda: self.text.tag_remove("focus", "1.0", "end"))

    def _show_text(self):
        self._style_text()
        self.scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.configure(state="normal", cursor="arrow", undo=False)
        self.text.delete("1.0", "end")

    def _message(self, text):
        self.text.insert("end", "\n" + text, ("italic",))

    # ----- buttons -----
    def _current_file(self):
        if self.folder is None:
            return None
        return self.folder / ("summary.md" if self.view == "minutes" else "transcript.md")

    def copy(self):
        if self.view == "minutes" and self._current_file().exists():
            text = self._current_file().read_text(encoding="utf-8")
        elif self.view == "transcript":
            text = "\n\n".join(output.transcript_lines(self.meeting))
        else:
            text = self.text.get("1.0", "end").strip()
        self.app.clipboard_clear()
        self.app.clipboard_append(text)
        self.app.flash(self.copy_btn, "Copied")

    def open_file(self):
        path = self._current_file()
        if path and path.exists():
            os.startfile(path)


def _insert_highlighted(text, s, terms, tags):
    """Insert s with the search words marked (tag "match")."""
    folded = s.casefold()
    spans = []
    for term in terms:
        start = folded.find(term)
        while start >= 0:
            spans.append((start, start + len(term)))
            start = folded.find(term, start + len(term))
    pos = 0
    for a, b in sorted(spans):
        if a < pos:
            continue
        text.insert("end", s[pos:a], tags)
        text.insert("end", s[a:b], tags + ("match",))
        pos = b
    text.insert("end", s[pos:], tags)


def _insert_inline(text, s, tags):
    """Insert a line with **bold**, *italic* / _italic_ and `code` spans."""
    pos = 0
    for m in INLINE.finditer(s):
        text.insert("end", s[pos:m.start()], tags)
        token = m.group(0)
        if token.startswith("**"):
            text.insert("end", token[2:-2], tags + ("bold",))
        elif token.startswith("`"):
            text.insert("end", token[1:-1], tags + ("code",))
        else:
            text.insert("end", token[1:-1], tags + ("italic",))
        pos = m.end()
    text.insert("end", s[pos:] + "\n", tags)


def render_markdown(text, md):
    lines = md.splitlines()
    # Skip the file's own title and meta list; the panel header shows them.
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), 0)
    for line in lines[first:]:
        s = line.rstrip()
        stripped = s.lstrip()
        level = min((len(s) - len(stripped)) // 2, 3)
        task = re.match(r"[-*] \[( |x|X)\] (.*)", stripped)
        bullet = re.match(r"[-*] (.*)", stripped)
        number = re.match(r"(\d+[.)]) (.*)", stripped)
        if s.startswith("### "):
            text.insert("end", s[4:] + "\n", ("h3",))
        elif s.startswith("## "):
            text.insert("end", s[3:] + "\n", ("h2",))
        elif s.startswith("# "):
            text.insert("end", s[2:] + "\n", ("h1",))
        elif task:
            text.insert("end", (BOX if task.group(1) == " " else BOX_CHECKED) + "\t", (f"li{level}", "marker"))
            _insert_inline(text, task.group(2), (f"li{level}",))
        elif bullet and stripped != "---":
            text.insert("end", BULLET + "\t", (f"li{level}", "marker"))
            _insert_inline(text, bullet.group(1), (f"li{level}",))
        elif number:
            text.insert("end", number.group(1) + "\t", (f"li{level}", "marker"))
            _insert_inline(text, number.group(2), (f"li{level}",))
        elif stripped in ("---", "***"):
            text.insert("end", "\n", ("hr",))
        elif not stripped:
            text.insert("end", "\n", ("gap",))
        else:
            _insert_inline(text, stripped, ("p",))


def render_transcript(text, meeting, on_click=None):
    """Paragraphs (consecutive sentences by the same speaker) with the speaker's name
    and time. With on_click, every sentence is highlighted under the mouse and calls
    on_click(segment index, event) when clicked."""
    segs = meeting["segments"]
    i = 0
    while i < len(segs):
        speaker = segs[i].get("speaker")
        last = i
        while last + 1 < len(segs) and segs[last + 1].get("speaker") == speaker:
            last += 1
        name = output.speaker_name(meeting, speaker)
        if name:
            text.insert("end", name, ("who", f"spk{speaker % 12}"))
            text.insert("end", "   " + output.fmt_time(segs[i]["start"]) + "\n", ("ts",))
        else:
            text.insert("end", output.fmt_time(segs[i]["start"]) + "\n", ("ts", "who"))
        for k in range(i, last + 1):
            tag = f"seg{k}"
            text.insert("end", segs[k]["text"], ("para", tag))
            text.insert("end", " " if k < last else "\n", ("para",))
            if on_click:
                text.tag_bind(tag, "<Enter>", lambda e, t=tag: (text.tag_configure(t, background=pick(LINE)),
                                                                text.configure(cursor="hand2")))
                text.tag_bind(tag, "<Leave>", lambda e, t=tag: (text.tag_configure(t, background=""),
                                                                text.configure(cursor="arrow")))
                text.tag_bind(tag, "<ButtonRelease-1>", lambda e, k=k: on_click(k, e))
        i = last + 1


class SpeakerPanel(ctk.CTkFrame):
    """Listen to each speaker's voice sample and give them a name."""

    def __init__(self, parent, app, folder, meeting):
        super().__init__(parent, fg_color="transparent")
        self.app, self.folder, self.meeting = app, folder, meeting
        self.entries = {}
        self.remember = {}   # speaker -> BooleanVar for "Remember voice"
        self.playing = None  # (button, id of the timer that resets it when the clip ends)
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(2, 6))
        ctk.CTkLabel(head, text="Play each voice and type a name.", font=app.f_small,
                     text_color=MUTED).pack(side="left")
        self.voices_btn = app.icon_button(head, "speakers", "Saved voices", self.show_saved_voices)
        self.voices_btn.pack(side="right")
        recognized = meeting.get("recognized", {})

        rows = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=LINE,
                                      scrollbar_button_hover_color=MUTED)
        rows.pack(fill="both", expand=True)
        stats = speakers.speaker_stats(meeting)
        total = sum(s["talk_s"] for s in stats.values()) or 1
        for speaker, s in stats.items():
            row = ctk.CTkFrame(rows, fg_color=BG, corner_radius=12)
            row.pack(fill="x", pady=4, padx=4)
            color = speaker_color(speaker)
            clip = speakers.clip_path(folder, speaker)
            button = ctk.CTkButton(row, text=PLAY, width=38, height=38, corner_radius=19, border_spacing=0,
                                   fg_color=color, hover_color=color, text_color="#FFFFFF", font=app.f_body)
            button.configure(command=lambda b=button, c=clip: self.toggle_play(b, c))
            button.pack(side="left", padx=(12, 10), pady=10)
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", fill="x", expand=True, pady=8, padx=(0, 12))
            entry = ctk.CTkEntry(col, font=app.f_body, height=32, border_width=1, border_color=LINE,
                                 fg_color=CARD, text_color=INK)
            entry.insert(0, output.speaker_name(meeting, speaker))
            entry.pack(fill="x")
            quote = " ... ".join(seg["text"] for seg in speakers.pick_sample(s["segments"]))
            share = round(100 * s["talk_s"] / total)
            ctk.CTkLabel(col, text=f"{fmt_duration(s['talk_s'])} talk time {MIDDOT} {share}%",
                         font=app.f_small, text_color=MUTED, anchor="w").pack(fill="x", pady=(2, 0))
            ctk.CTkLabel(col, text=f"\"{quote[:140]}\"", font=app.f_small, text_color=MUTED, anchor="w",
                         justify="left", wraplength=360).pack(fill="x")
            self.entries[speaker] = entry
            if speaker == 0:
                continue  # the mic track is you: your name is my_name in config.toml
            extra = ctk.CTkFrame(col, fg_color="transparent")
            extra.pack(fill="x", pady=(4, 0))
            match = recognized.get(str(speaker))
            if match:
                ctk.CTkLabel(extra, text=f"{CHECK} Recognized from saved voice ({round(100 * match['score'])}% similar)",
                             font=app.f_small, text_color=speaker_color(0), anchor="w").pack(side="left", padx=(0, 12))
            self.remember[speaker] = ctk.BooleanVar(value=False)
            ctk.CTkCheckBox(extra, text="Remember voice" if not match else "Update saved voice",
                            variable=self.remember[speaker], font=app.f_small, text_color=MUTED, fg_color=INK,
                            hover_color=INK_HOVER, border_color=MUTED, checkmark_color=BG, checkbox_width=16,
                            checkbox_height=16).pack(side="left")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=6, pady=(8, 0))
        self.rewrite = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(bottom, text="Rewrite minutes with the names", variable=self.rewrite, font=app.f_small,
                        text_color=INK, fg_color=INK, hover_color=INK_HOVER, border_color=MUTED, checkmark_color=BG,
                        checkbox_width=18, checkbox_height=18).pack(side="left")
        ctk.CTkButton(bottom, text="Save names", height=36, width=130, corner_radius=10, font=app.f_button,
                      fg_color=INK, hover_color=INK_HOVER, text_color=BG, command=self.save).pack(side="right")

    def toggle_play(self, button, clip):
        """Play a voice sample; the button shows stop while it plays. Pressing it
        again (or playing another speaker) stops it."""
        was_playing = self.playing is not None and self.playing[0] is button
        self.stop_playing()
        if was_playing or not Path(clip).exists():
            return
        play_sound(clip)
        # winsound cannot tell when a clip ends, so reset the button after its length.
        with wave.open(str(clip), "rb") as w:
            length_ms = int(1000 * w.getnframes() / w.getframerate())
        button.configure(text=SQUARE)
        self.playing = (button, self.after(length_ms + 150, self.stop_playing))

    def stop_playing(self):
        play_sound(None)
        if self.playing is not None:
            button, timer = self.playing
            self.playing = None
            self.after_cancel(timer)
            if button.winfo_exists():
                button.configure(text=PLAY)

    def destroy(self):
        self.stop_playing()
        super().destroy()

    def save(self):
        names = [f"{n}={e.get().strip()}" for n, e in self.entries.items()
                 if e.get().strip() and e.get().strip() != output.speaker_name(self.meeting, n)]
        to_remember = [n for n, var in self.remember.items() if var.get()]
        if not names and not to_remember:
            return
        if self.app.worker.busy():
            messagebox.showinfo("Busy", "Wait until the current job is finished.", parent=self.app)
            return
        self.stop_playing()
        if to_remember and not self._remember_voices(to_remember):
            return
        if names:
            args = ["rename", str(self.folder)] + names
            if self.rewrite.get():
                args.append("--summarize")
            self.app.worker.run(args, self.meeting["name"], self.folder)

    def _remember_voices(self, numbers):
        """Save the voices of the ticked speakers under the names typed. Returns False
        if nothing should happen yet (a speaker still has a placeholder name)."""
        from .voices import save_voice
        label = output.labels(self.meeting["language"])["speaker"]
        todo = []
        for n in numbers:
            name = self.entries[n].get().strip()
            if not name or name == f"{label} {n}":
                messagebox.showinfo("Remember voice", f"Type the person's name for {label} {n} first.",
                                    parent=self.app)
                return False
            todo.append((n, name))
        if not messagebox.askyesno(
                "Remember voice", "Save the voice of " + ", ".join(name for _, name in todo) + "?\n\n"
                "They will be named automatically in later meetings. A voice fingerprint is personal data: "
                "tell them, and delete it under \"Saved voices\" when it is no longer needed.", parent=self.app):
            return False
        messages = [save_voice(self.app.cfg, name, self.folder, self.meeting, n)[1] for n, name in todo]
        messagebox.showinfo("Remember voice", "\n\n".join(messages), parent=self.app)
        for var in self.remember.values():
            var.set(False)
        return True

    def show_saved_voices(self):
        from .voices import list_voices
        saved = list_voices(self.app.cfg)
        self.app.saved_voices_menu(saved, self.voices_btn)
