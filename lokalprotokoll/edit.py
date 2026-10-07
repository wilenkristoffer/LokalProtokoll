"""Corrections to a processed meeting: speakers, text, the meeting name.

These are quick (no models run), so the app calls them directly. After a change
that affects who said what, the minutes are marked as out of date
(meeting["summary_stale"]) until they are rewritten. Also deleting the audio and
the voice samples of a meeting when you are done with them.
"""

import re
from pathlib import Path

from . import output, speakers


def _save(meeting, folder, samples=False):
    output.write_transcript_md(meeting, folder)
    if samples and speakers.has_samples(folder):  # deleted voice samples are not made again
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


def rename_speakers(meeting, folder, new_names, in_summary=True):
    """Give speakers names ({"1": "Erik", "0": "Anna"}) in meeting.json,
    transcript.md and speakers.html, and with in_summary also in summary.md by a
    plain text replace: the rest of the minutes stays word for word. Returns the
    (old, new) names that changed."""
    names = meeting.setdefault("speaker_names", {})
    before = {n: output.speaker_name(meeting, int(n)) for n in new_names}
    for number, name in new_names.items():
        name = output.plain_name(name, meeting) if number == "0" else name.strip()
        if name:
            names[number] = name
        else:
            names.pop(number, None)
    renames = [(before[n], output.speaker_name(meeting, int(n))) for n in new_names]
    renames = [(old, new) for old, new in renames if old != new]
    if not renames:
        return []
    output.write_transcript_md(meeting, folder)
    speakers.write_html(meeting, folder)
    summary = Path(folder, "summary.md")
    if in_summary and summary.exists():
        # Your unnamed label "Jag" / "Me" is also an ordinary word: match its capitals.
        me = output.labels(meeting["language"])["me"]
        text = speakers.replace_names(summary.read_text(encoding="utf-8"), renames, exact_case=(me,))
        summary.write_text(text, encoding="utf-8")
    output.save_meeting(meeting, folder)
    return renames


def meetings_without_my_name(cfg, previous=""):
    """Recordings where you (the mic track) are still "Jag" / "Me", or have the
    name you used before (previous). Mic-only recordings have no mic speaker."""
    from .config import resolve
    base = Path(resolve(cfg["paths"]["meetings_dir"]))
    found = []
    for meeting_json in sorted(base.glob("*/meeting.json")):
        try:
            meeting = output.load_meeting(meeting_json.parent)
        except (SystemExit, ValueError, KeyError):
            continue
        if not meeting.get("track_files") or not any(s.get("speaker") == 0 for s in meeting["segments"]):
            continue
        if meeting.get("speaker_names", {}).get("0", "") in ("", previous):
            found.append(meeting_json.parent)
    return found


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


def delete_recording(folder):
    """Delete the meeting's audio when you are done with it. The transcript, the
    minutes and the voice samples stay. Returns the bytes freed."""
    meeting = output.load_meeting(folder)
    freed = 0
    for path in speakers.audio_files(meeting, folder):
        freed += path.stat().st_size
        path.unlink()
    return freed


def delete_voice_samples(folder):
    """Delete the short voice sample of each speaker (speakers/ and speakers.html).
    Saved voices are separate: see voices.delete_voice. Returns the bytes freed."""
    freed = 0
    clip_dir = Path(folder, "speakers")
    for path in clip_dir.glob("*.wav"):
        freed += path.stat().st_size
        path.unlink()
    if clip_dir.exists() and not any(clip_dir.iterdir()):
        clip_dir.rmdir()
    Path(folder, "speakers.html").unlink(missing_ok=True)
    meeting = output.load_meeting(folder)
    meeting["voice_samples_deleted"] = True  # so "lp.py rename" does not cut them again
    output.save_meeting(meeting, folder)
    return freed


def save_minutes(folder, text):
    """Save minutes edited by hand. They are then up to date again."""
    Path(folder, "summary.md").write_text(text.rstrip() + "\n", encoding="utf-8")
    meeting = output.load_meeting(folder)
    meeting.pop("summary_stale", None)
    output.save_meeting(meeting, folder)
