"""Write the meeting folder: meeting.json, transcript.md, summary.md."""

import json
import re
import unicodedata
from pathlib import Path

from .config import resolve
from .merge import group_paragraphs


# Name for a meeting that was not given one, until a title is made from its minutes.
DEFAULT_NAME = "M\xf6te"


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "meeting"


def new_meeting_dir(cfg, name, when):
    """Create meetings/<date>_<time>_<name>/ (with _2, _3 ... if it exists)."""
    base = Path(resolve(cfg["paths"]["meetings_dir"]))
    out = base / f"{when:%Y-%m-%d_%H%M}_{slugify(name)}"
    n = 2
    while out.exists():
        out = base / f"{when:%Y-%m-%d_%H%M}_{slugify(name)}_{n}"
        n += 1
    out.mkdir(parents=True)
    return out

# Non-ASCII letters are written as escapes (\xe4 = a-umlaut) to keep this file ASCII.
LABELS = {
    "sv": {"speaker": "Talare", "transcript": "Transkribering", "summary": "Protokoll",
           "date": "Datum", "duration": "L\xe4ngd", "language": "Spr\xe5k",
           "model": "Modell", "me": "Jag"},
    "en": {"speaker": "Speaker", "transcript": "Transcript", "summary": "Minutes",
           "date": "Date", "duration": "Duration", "language": "Language",
           "model": "Model", "me": "Me"},
}


def labels(language):
    return LABELS.get(language, LABELS["en"])


def fmt_time(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def speaker_name(meeting, speaker):
    """The name given with "lp.py rename", otherwise "Talare 1" / "Speaker 1".
    Speaker 0 is the mic track of a recording (you), shown as "Jag" / "Me"."""
    if speaker is None:
        return None
    given = meeting.get("speaker_names", {}).get(str(speaker))
    if given:
        return given
    lab = labels(meeting["language"])
    return lab["me"] if speaker == 0 else f"{lab['speaker']} {speaker}"


def transcript_lines(meeting):
    """One line per paragraph: "[00:01:23] Talare 1: text". Used for the LLM too."""
    lines = []
    for p in group_paragraphs(meeting["segments"]):
        name = speaker_name(meeting, p.get("speaker"))
        prefix = f"[{fmt_time(p['start'])}] " + (f"{name}: " if name else "")
        lines.append(prefix + p["text"])
    return lines


def _header(meeting, title):
    lab = labels(meeting["language"])
    return (f"# {title}: {meeting['name']}\n\n"
            f"- {lab['date']}: {meeting['date']}\n"
            f"- {lab['duration']}: {fmt_time(meeting['duration_s'])}\n"
            f"- {lab['language']}: {meeting['language']}\n")


def write_transcript_md(meeting, out_dir):
    lab = labels(meeting["language"])
    text = _header(meeting, lab["transcript"])
    text += f"- {lab['model']}: {Path(meeting['whisper_model']).name}, {meeting['diarizer']}\n\n"
    for p in group_paragraphs(meeting["segments"]):
        name = speaker_name(meeting, p.get("speaker"))
        who = f" {name}" if name else ""
        text += f"**[{fmt_time(p['start'])}]{who}:** {p['text']}\n\n"
    Path(out_dir, "transcript.md").write_text(text, encoding="utf-8")


def write_summary_md(meeting, summary, model, consent_note, out_dir, filename="summary.md"):
    lab = labels(meeting["language"])
    text = _header(meeting, lab["summary"])
    text += f"- {lab['model']}: {model}\n\n"
    text += summary.strip() + "\n\n---\n\n"
    text += f"_{consent_note}_\n"
    Path(out_dir, filename).write_text(text, encoding="utf-8")


def save_meeting(meeting, out_dir):
    Path(out_dir, "meeting.json").write_text(
        json.dumps(meeting, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meeting(out_dir):
    path = Path(out_dir, "meeting.json")
    if not path.exists():
        raise SystemExit(f"No meeting.json in {out_dir}")
    return json.loads(path.read_text(encoding="utf-8"))
