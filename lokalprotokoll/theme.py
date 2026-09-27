"""Colors and small styling helpers shared by the app and the viewer.

Colors are (light, dark) pairs, which CustomTkinter widgets accept directly.
Plain tkinter widgets (the text viewer) need one color: use pick().
Non-ASCII symbols are written as escapes to keep this file ASCII.
"""

import customtkinter as ctk

BG = ("#F4F1EA", "#161513")
CARD = ("#FBFAF6", "#211F1C")
LINE = ("#E4DFD4", "#312E29")
INK = ("#1F1D1A", "#ECE7DD")
INK_HOVER = ("#3A3732", "#D6D0C4")
MUTED = ("#77716A", "#9A948A")
RED = ("#D9412B", "#E5533D")
RED_HOVER = ("#BD3420", "#CC4531")
YOU = ("#2F7D6D", "#4FA893")
OTHERS = ("#C27A22", "#DB9A45")
SPEAKER_COLORS = [("#C27A22", "#DB9A45"), ("#3E6FB0", "#6E9BDB"), ("#A8457A", "#D173A5"),
                  ("#6B8E23", "#98BF4A"), ("#8A5CC2", "#B08BE3"), ("#B5543B", "#DE7F66"),
                  ("#2B8FA3", "#58B8CC")]

DOT, SQUARE, CHECK, MORE, MIDDOT, PLAY, CLOSE = "\u25cf", "\u25a0", "\u2713", "\u22ef", "\xb7", "\u25b6", "\u2715"
BULLET, BOX, BOX_CHECKED = "\u2022", "\u2610", "\u2611"


def pick(color):
    """The light or dark half of a color pair, for plain tkinter widgets."""
    return color[0] if ctk.get_appearance_mode() == "Light" else color[1]


def speaker_color(speaker):
    """Speaker 0 (you, in a recording) is green; the others cycle through a palette."""
    if not speaker:
        return YOU
    return SPEAKER_COLORS[(speaker - 1) % len(SPEAKER_COLORS)]


def restyle_segments(seg):
    """CTkSegmentedButton has one text color for all segments; recolor them so the
    selected one is light-on-ink and the others ink-on-light."""
    for value, button in getattr(seg, "_buttons_dict", {}).items():
        button.configure(text_color=BG if value == seg.get() else INK)


def fmt_duration(seconds):
    s = int(seconds or 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
