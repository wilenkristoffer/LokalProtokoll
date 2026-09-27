"""Corrections to a processed meeting: speakers, text, the meeting name.

These are quick (no models run), so the app calls them directly. After a change
that affects who said what, the minutes are marked as out of date
(meeting["summary_stale"]) until they are rewritten.
"""

import re
from pathlib import Path

from . import output, speakers


def _save(meeting, folder, samples=False):
    output.write_transcript_md(meeting, folder)
    if samples:
        speakers.make_samples(meeting, folder)
    else:
        speakers.write_html(meeting, folder)
    output.save_meeting(meeting, folder)


def paragraph_of(meeting, index):
    """Indexes of the segments in the same paragraph as segment index: the
    neighbours with the same speaker, as the transcript shows them together."""
    segs = meeting["segments"]
    speaker = segs[index].get("speaker")
    first = index
    while first > 0 and segs[first - 1].get("speaker") == speaker:
        first -= 1
    last = index
    while last + 1 < len(segs) and segs[last + 1].get("speaker") == speaker:
        last += 1
    return list(range(first, last + 1))


def next_speaker_number(meeting):
    numbers = [s["speaker"] for s in meeting["segments"] if isinstance(s.get("speaker"), int)]
    return max(numbers, default=0) + 1


def set_speaker(folder, indexes, speaker):
    """Give the segments at indexes to speaker (a number; a new one is allowed)."""
    meeting = output.load_meeting(folder)
    for i in indexes:
        meeting["segments"][i]["speaker"] = speaker
    meeting["summary_stale"] = True
    _save(meeting, folder, samples=True)
    return meeting


def set_text(folder, index, text):
    """Replace the text of one segment (e.g. a misheard word)."""
    meeting = output.load_meeting(folder)
    seg = meeting["segments"][index]
    seg["text"] = text.strip()
    # The word timings no longer match the text; the segment is used as a whole.
    seg.pop("words", None)
    meeting["summary_stale"] = True
    _save(meeting, folder)
    return meeting


def replace_everywhere(folder, find, replacement, whole_words=True):
    """Replace a word or phrase in the whole transcript and the minutes, ignoring
    case (a misheard name, for example). Returns the number of replacements."""
    meeting = output.load_meeting(folder)
    body = re.escape(find.strip())
    pattern = re.compile(rf"(?<!\w){body}(?!\w)" if whole_words else body, re.IGNORECASE)
    count = 0
    for seg in meeting["segments"]:
        seg["text"], n = pattern.subn(replacement, seg["text"])
        if n:
            seg.pop("words", None)
            count += n
    summary = Path(folder, "summary.md")
    if summary.exists():
        text, n = pattern.subn(replacement, summary.read_text(encoding="utf-8"))
        summary.write_text(text, encoding="utf-8")
        count += n
    if count:
        _save(meeting, folder)
    return count


def rename_meeting(folder, name):
    """Give the meeting a new name (it then no longer gets an automatic title)."""
    meeting = output.load_meeting(folder)
    old = meeting["name"]
    meeting["name"] = name.strip()
    meeting["auto_name"] = False
    summary = Path(folder, "summary.md")
    if summary.exists():
        lines = summary.read_text(encoding="utf-8").split("\n")
        if lines and lines[0].startswith("# ") and lines[0].endswith(old):
            lines[0] = lines[0][: -len(old)] + meeting["name"]
            summary.write_text("\n".join(lines), encoding="utf-8")
    _save(meeting, folder)
    return meeting


def save_minutes(folder, text):
    """Save minutes edited by hand. They are then up to date again."""
    Path(folder, "summary.md").write_text(text.rstrip() + "\n", encoding="utf-8")
    meeting = output.load_meeting(folder)
    meeting.pop("summary_stale", None)
    output.save_meeting(meeting, folder)
