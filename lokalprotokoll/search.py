"""Search all meetings: transcripts, minutes and meeting names.

A sentence matches when it contains all the search words (case does not matter,
and a word also matches inside longer words: "budget" finds "budgeten").
"Quoted words" are searched as one phrase.
"""

import json
import re
from pathlib import Path

from . import output
from .config import resolve


def terms_of(query):
    """ 'budget "nya planen"' -> ["budget", "nya planen"] (casefolded)."""
    return [t.strip('"').casefold() for t in re.findall(r'"[^"]+"|\S+', query) if t.strip('"')]


def snippet(text, terms, width=110):
    """The part of text around the first search word, with "..." where it was cut."""
    if len(text) <= width:
        return text
    folded = text.casefold()
    first = min((folded.find(t) for t in terms if t in folded), default=0)
    start = max(0, min(first - width // 3, len(text) - width))
    return ("..." if start > 0 else "") + text[start:start + width].strip() + ("..." if start + width < len(text) else "")


def _matches(text, terms):
    folded = text.casefold()
    return all(t in folded for t in terms)


def search(cfg, query, max_hits_per_meeting=50):
    """Newest meetings first: [{"folder", "name", "date", "name_match",
    "sentences": [{"index", "start", "speaker", "text"}], "minutes": [line, ...]}]."""
    terms = terms_of(query)
    if not terms:
        return []
    base = Path(resolve(cfg["paths"]["meetings_dir"]))
    results = []
    for folder in sorted((p for p in base.glob("*") if (p / "meeting.json").exists()), reverse=True):
        try:
            meeting = json.loads((folder / "meeting.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        sentences = [{"index": i, "start": s["start"], "speaker": output.speaker_name(meeting, s.get("speaker")),
                      "text": s["text"]}
                     for i, s in enumerate(meeting["segments"]) if _matches(s["text"], terms)]
        minutes = []
        summary = folder / "summary.md"
        if summary.exists():
            lines = summary.read_text(encoding="utf-8").splitlines()
            # Skip the title and meta lines at the top; they repeat the meeting name and date.
            first = next((i for i, line in enumerate(lines) if line.startswith("## ")), 0)
            minutes = [line.strip(" -*") for line in lines[first:]
                       if line.strip() and not line.startswith("#") and _matches(line, terms)]
        name_match = _matches(meeting["name"], terms)
        if sentences or minutes or name_match:
            results.append({"folder": folder, "name": meeting["name"], "date": meeting["date"],
                            "name_match": name_match, "sentences": sentences[:max_hits_per_meeting],
                            "more": max(0, len(sentences) - max_hits_per_meeting), "minutes": minutes})
    return results
