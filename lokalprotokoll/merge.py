"""Combine transcript segments with speaker turns by timestamp overlap."""

import re


def _words(text):
    return set(re.findall(r"\w+", text.lower()))


def remove_echo(mic_segments, system_segments, min_overlap=0.6):
    """Without a headset the mic also picks up the other participants from the
    speakers. Drop mic segments whose words mostly also appear in the system
    audio at the same time (+-1 s). Returns (kept, number_dropped)."""
    kept = []
    for m in mic_segments:
        words = _words(m["text"])
        near = [s for s in system_segments if s["start"] < m["end"] + 1 and s["end"] > m["start"] - 1]
        heard = set().union(*(_words(s["text"]) for s in near)) if near else set()
        # Single words are kept: dropping them removes real words too often.
        if len(words) >= 2 and len(words & heard) / len(words) >= min_overlap:
            continue
        kept.append(m)
    return kept, len(mic_segments) - len(kept)


def _speaker_at(start, end, turns):
    """The speaker of the turn that overlaps start-end most, or of the nearest turn."""
    best, best_overlap = None, 0.0
    for turn in turns:
        overlap = min(end, turn["end"]) - max(start, turn["start"])
        if overlap > best_overlap:
            best, best_overlap = turn, overlap
    if best is None:
        mid = (start + end) / 2
        best = min(turns, key=lambda t: abs((t["start"] + t["end"]) / 2 - mid))
    return best["speaker"]


def _split_by_speaker(seg, turns):
    """Give every word a speaker and cut the segment where the speaker changes.
    A single word between two words of another speaker is treated as noise."""
    words = seg.get("words")
    if not words:  # meetings processed before words were kept
        return [dict(seg, speaker=_speaker_at(seg["start"], seg["end"], turns))]
    labels = [_speaker_at(w[0], w[1], turns) for w in words]
    for i in range(1, len(labels) - 1):
        if labels[i - 1] == labels[i + 1] != labels[i]:
            labels[i] = labels[i - 1]
    parts, first = [], 0
    for i in range(1, len(words) + 1):
        if i == len(words) or labels[i] != labels[first]:
            chunk = words[first:i]
            parts.append(dict(seg, start=chunk[0][0], end=chunk[-1][1], words=chunk,
                              text=" ".join(w[2] for w in chunk), speaker=labels[first]))
            first = i
    return parts


# A segment is normally one sentence and gets one speaker: that scored best in the
# benchmark, because speaker turns are not exact to the word. A "sentence" longer than
# this means Whisper wrote no punctuation (it happens in casual conversation); such a
# segment can hold several speakers, so it is split per word where the speaker changes.
LONG_SEGMENT_S = 15


def assign_speakers(segments, turns):
    """Return new segments with a "speaker": the speaker of the turn that overlaps the
    segment most, or per word for segments longer than LONG_SEGMENT_S. Speakers are
    renumbered 1, 2, 3 ... in order of first appearance."""
    if not turns:
        return [dict(seg, speaker=None) for seg in segments]
    result = []
    for seg in segments:
        if seg["end"] - seg["start"] > LONG_SEGMENT_S:
            result += _split_by_speaker(seg, turns)
        else:
            result.append(dict(seg, speaker=_speaker_at(seg["start"], seg["end"], turns)))
    numbers = {}
    for seg in result:
        if seg["speaker"] not in numbers:
            numbers[seg["speaker"]] = len(numbers) + 1
        seg["speaker"] = numbers[seg["speaker"]]
    return result


def group_paragraphs(segments):
    """Join consecutive segments by the same speaker into paragraphs."""
    paragraphs = []
    for seg in segments:
        if paragraphs and paragraphs[-1]["speaker"] == seg["speaker"]:
            paragraphs[-1]["end"] = seg["end"]
            paragraphs[-1]["text"] += " " + seg["text"]
        else:
            paragraphs.append(dict(seg))
    return paragraphs
