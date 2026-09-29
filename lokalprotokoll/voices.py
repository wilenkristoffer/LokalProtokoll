"""Saved voices: remember a person's voice, and name them automatically in later meetings.

A voice is a fingerprint from the same voice model as speaker detection (TitaNet):
the duration-weighted average of the fingerprints of the person's longest
segments. It is stored as voices/<name>.json in the project folder.

A voice fingerprint is biometric personal data (GDPR): tell people when you save
their voice, and delete it when it is no longer needed (lp.py voices --delete).

Measured on the test recordings (tests/, first half vs second half): the same
person scores 0.84-0.95 with enough speech, different people mostly below 0.45.
A speaker is only named when the best voice scores at least MATCH_SCORE and beats
the next best voice by MATCH_MARGIN; otherwise the speaker keeps "Talare N".
"""

import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import numpy as np

from .audio import read_wav_float32
from .config import resolve

MATCH_SCORE = 0.75
MATCH_MARGIN = 0.10
MIN_SAVE_S = 20        # speech needed to save a voice
MIN_MATCH_S = 10       # speech needed to recognize a speaker
MAX_SPEECH_S = 90      # speech used per fingerprint (the longest segments)


def voices_dir(cfg):
    return Path(resolve(cfg["paths"].get("voices_dir", "voices")))


def _file(cfg, name):
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", slug).strip("-").lower() or "voice"
    return voices_dir(cfg) / f"{slug}.json"


def _extractor(cfg):
    import sherpa_onnx
    d = cfg["diarize"]
    return sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=resolve(d["embedding_model"]), num_threads=d.get("threads", 4)))


def _audio_for(folder, meeting, segments, cache):
    """The audio the speaker is on: their own track in a recording, else the mix."""
    track = meeting.get("track_files", {}).get(segments[0].get("track"), "audio_16k.wav")
    if track not in cache:
        cache[track] = read_wav_float32(Path(folder, track))
    return cache[track]


def fingerprint(extractor, audio, segments, min_segment_s=1.0):
    """(normalized fingerprint, seconds of speech used) from the longest segments."""
    from .diarize import _embed
    total, acc = 0.0, None
    for seg in sorted(segments, key=lambda s: s["end"] - s["start"], reverse=True):
        length = seg["end"] - seg["start"]
        if length < min_segment_s or total >= MAX_SPEECH_S:
            continue
        e = length * _embed(extractor, audio, seg["start"], seg["end"])
        acc = e if acc is None else acc + e
        total += length
    if acc is None:
        return None, 0.0
    return acc / (np.linalg.norm(acc) or 1.0), total


def speaker_fingerprints(cfg, folder, meeting, min_s=0.0):
    """{speaker number: (fingerprint, seconds)} for everyone except the mic track (you)."""
    extractor, cache, result = _extractor(cfg), {}, {}
    numbers = sorted({s["speaker"] for s in meeting["segments"]
                      if isinstance(s.get("speaker"), int) and s.get("track") != "mic"})
    for n in numbers:
        segs = [s for s in meeting["segments"] if s.get("speaker") == n]
        e, seconds = fingerprint(extractor, _audio_for(folder, meeting, segs, cache), segs)
        if e is not None and seconds >= min_s:
            result[n] = (e, seconds)
    return result


def list_voices(cfg):
    folder = voices_dir(cfg)
    voices = []
    for path in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            v = json.loads(path.read_text(encoding="utf-8"))
            v["file"] = path
            v["embedding"] = np.array(v["embedding"], dtype=np.float32)
            voices.append(v)
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return voices


def voices_in_meeting(cfg, folder, meeting):
    """The saved voices of people in this meeting: saved from it, or named in it
    (by hand or recognized)."""
    names = {n.casefold() for k, n in meeting.get("speaker_names", {}).items() if k != "0"}
    return [v for v in list_voices(cfg)
            if v["name"].casefold() in names or Path(folder).name in v.get("meetings", [])]


def save_voice(cfg, name, folder, meeting, speaker):
    """Save (or improve) the voice of one speaker under name. Returns (seconds, message)."""
    from .speakers import has_audio
    segs = [s for s in meeting["segments"] if s.get("speaker") == speaker]
    if not segs:
        return 0.0, f"No speech from speaker {speaker}."
    if not has_audio(meeting, folder):
        return 0.0, f"The recording of this meeting was deleted, so the voice of {name} cannot be saved from it."
    e, seconds = fingerprint(_extractor(cfg), _audio_for(folder, meeting, segs, {}), segs)
    if e is None or seconds < MIN_SAVE_S:
        return seconds, (f"Only {seconds:.0f} s of clear speech from {name}; at least {MIN_SAVE_S} s is needed "
                         "for a reliable voice. Try again with a longer meeting.")
    path = _file(cfg, name)
    record = {"name": name, "embedding": e, "seconds": seconds, "meetings": [], "created": str(date.today())}
    if path.exists():
        # Saved before: average with the old voice, weighted by speech time.
        old = json.loads(path.read_text(encoding="utf-8"))
        combined = np.array(old["embedding"]) * old["seconds"] + e * seconds
        record.update(old, embedding=combined / np.linalg.norm(combined), seconds=old["seconds"] + seconds)
    record["meetings"] = sorted(set(record.get("meetings", [])) | {Path(folder).name})
    record["updated"] = str(date.today())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(record, embedding=[round(float(x), 6) for x in record["embedding"]]),
                               ensure_ascii=False, indent=1), encoding="utf-8")
    return seconds, f"Saved the voice of {name} ({record['seconds']:.0f} s of speech in total)."


def delete_voice(cfg, name):
    path = _file(cfg, name)
    if path.exists():
        path.unlink()
        return True
    return False


def recognize(cfg, folder, meeting):
    """Match the meeting's speakers with the saved voices.
    Returns {speaker number: {"name", "score"}} for the clear matches only."""
    voices = list_voices(cfg)
    if not voices:
        return {}
    prints = speaker_fingerprints(cfg, folder, meeting, min_s=MIN_MATCH_S)
    scores = {(n, i): float(e @ v["embedding"]) for n, (e, _) in prints.items() for i, v in enumerate(voices)}
    matches, used_voices = {}, set()
    # Best pairs first, so one voice names at most one speaker.
    for (n, i), score in sorted(scores.items(), key=lambda kv: kv[1], reverse=True):
        if n in matches or i in used_voices or score < MATCH_SCORE:
            continue
        others = [s for (m, j), s in scores.items() if m == n and j != i]
        if others and score - max(others) < MATCH_MARGIN:
            continue
        matches[n] = {"name": voices[i]["name"], "score": round(score, 2)}
        used_voices.add(i)
    return matches
