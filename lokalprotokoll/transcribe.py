"""Speech to text with whisper.cpp (whisper-cli.exe), run as a subprocess."""

import json
import re
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from . import audio
from .config import resolve
from .output import fmt_time

# Lines from whisper-cli worth showing live. Everything is saved to whisper.log.
SHOW_PATTERNS = ("progress =", "ggml_vulkan: 0", "ggml_vulkan: Found", "error", "failed",
                 "auto-detected language")


def _check_file(path, what):
    if not Path(path).exists():
        raise SystemExit(f"{what} not found: {path}\nSee README.md (setup) or run setup_models.py.")


def _run_whisper(cmd, log_path=None, show=True):
    """Run whisper-cli, print the interesting lines, return all output as text."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        raise SystemExit(f"whisper-cli not found: {cmd[0]}\nBuild it first, see README.md.")
    lines = []
    for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        lines.append(line)
        if show and any(p in line for p in SHOW_PATTERNS):
            print("   ", line.strip())
    proc.wait()
    output = "\n".join(lines)
    if log_path:
        Path(log_path).write_text(output, encoding="utf-8")
    if proc.returncode != 0:
        tail = "\n".join(lines[-15:])
        raise SystemExit(f"whisper-cli failed (exit code {proc.returncode}):\n{tail}")
    return output


DETECT_WINDOW_S = 30  # Whisper hears 30 s at a time


def detect_windows(wav_path, duration, count):
    """Start times of up to count 30 s windows with the most sound, one from each
    part of the meeting. The start of a recording is often silence or small talk
    ("Hello. Hi, Erik." in a Swedish meeting), so it is not enough on its own."""
    if duration <= DETECT_WINDOW_S:
        return [0.0]
    sound = np.zeros(int(duration) + 1)
    for start, end in audio.sound_runs(wav_path):
        sound[int(start):int(end) + 1] = 1
    part = duration / count
    windows = []
    for i in range(count):
        first, last = i * part, min((i + 1) * part, duration) - DETECT_WINDOW_S
        starts = np.arange(first, max(first, last) + 1, 5.0)
        best = max(starts, key=lambda s: sound[int(s):int(s) + DETECT_WINDOW_S].sum())
        if sound[int(best):int(best) + DETECT_WINDOW_S].sum() >= 10:  # seconds with sound
            windows.append(float(best))
    return windows or [0.0]


def detect_language(cfg, wav_path, duration):
    """Return a language code such as "sv" or "en" using the multilingual model.

    Whisper's own detection only listens to the first 30 s of the file (the -ot
    offset is ignored with -dl), which is often silence before the meeting starts.
    So several windows with speech, spread over the meeting, are cut out and each
    is detected; they vote with Whisper's probability."""
    t = cfg["transcribe"]
    model = resolve(t["model_detect"])
    _check_file(model, "Language detection model")
    votes = {}
    with tempfile.TemporaryDirectory(prefix="lp_detect_") as tmp, wave.open(str(wav_path), "rb") as src:
        rate = src.getframerate()
        for start in detect_windows(wav_path, duration, t.get("detect_windows", 5)):
            clip = Path(tmp) / f"{int(start)}.wav"
            src.setpos(int(start * rate))
            with wave.open(str(clip), "wb") as out:
                out.setparams(src.getparams())
                out.writeframes(src.readframes(DETECT_WINDOW_S * rate))
            cmd = [resolve(cfg["paths"]["whisper_cli"]), "-m", model, "-f", str(clip),
                   "-l", "auto", "-dl", "-t", str(t["threads"])]
            m = re.search(r"auto-detected language:\s*([a-z]+)\s*\(p = ([\d.]+)\)", _run_whisper(cmd, show=False))
            if m:
                votes[m.group(1)] = votes.get(m.group(1), 0.0) + float(m.group(2))
                print(f"    {fmt_time(start)}: {m.group(1)} ({float(m.group(2)):.0%})")
    if not votes:
        print("    Could not detect language, using sv")
        return "sv"
    return max(votes, key=votes.get)



def transcribe(cfg, wav_path, out_dir, language, name="whisper", separate_track=False):
    """Transcribe wav_path. Returns (segments, model_path).
    Each segment is {"start": s, "end": s, "text": str}. Raw output: <name>.json.
    separate_track: the file is one side of a recording (mic or system), which is
    silent while the other side talks. Timestamps are then corrected to the sound."""
    t = cfg["transcribe"]
    model = resolve(t["model_sv"] if language == "sv" else t["model_en"])
    _check_file(model, "Whisper model")
    out_base = Path(out_dir) / name
    # "-ml 1 -sow" gives one entry per word with its own timestamps; the words
    # are grouped into sentence segments below.
    cmd = [resolve(cfg["paths"]["whisper_cli"]), "-m", model, "-f", str(wav_path),
           "-l", language, "-t", str(t["threads"]), "-oj", "-of", str(out_base), "-pp",
           "-ml", "1", "-sow"]
    if t.get("use_vad"):
        vad = resolve(t["vad_model"])
        _check_file(vad, "VAD model")
        cmd += ["--vad", "-vm", vad]
        if "vad_min_silence_ms" in t:
            cmd += ["--vad-min-silence-duration-ms", str(t["vad_min_silence_ms"])]
        if "vad_speech_pad_ms" in t:
            cmd += ["--vad-speech-pad-ms", str(t["vad_speech_pad_ms"])]
    prompt = t.get(f"prompt_{language}", "")
    if prompt:
        cmd += ["--prompt", prompt]
    cmd += [str(a) for a in t.get("extra_args", [])]

    _run_whisper(cmd, log_path=Path(out_dir) / f"{name}.log")

    raw = Path(str(out_base) + ".json").read_bytes().decode("utf-8", errors="replace")
    data = json.loads(raw)
    words = []
    for item in data.get("transcription", []):
        text = item["text"].strip()
        if text:
            words.append({"start": item["offsets"]["from"] / 1000.0,
                          "end": item["offsets"]["to"] / 1000.0, "text": text})
    if not separate_track:
        return group_words(words), model
    runs = audio.sound_runs(wav_path)
    segments = group_words(snap_to_sound(words, runs))
    return audio.trim_to_sound(segments, runs), model


def _longest_silence(start, end, runs):
    """Longest stretch without sound between start and end (seconds)."""
    t, longest = start, 0.0
    for s, e in runs:
        if e <= start or s >= end:
            continue
        longest = max(longest, s - t)
        t = max(t, e)
    return max(longest, end - t)


def snap_to_sound(words, runs, max_word_s=1.5, min_pause_s=1.0):
    """Fix words that whisper.cpp placed on the wrong side of a pause.

    With VAD, a word next to a pause can be stretched over it. Whether the word
    was said before or after the pause follows from the sentence:

    - It starts a sentence (the previous word ended one): it was said after the
      pause ("Good." 30.1-38.8 s, said at 38.3 s). Move it to the last stretch
      of sound that begins inside it, or to the next stretch of sound.
    - It continues a sentence: it was said before the pause and its end was
      stretched ("...med bara att addera." 8.4-23.9 s, said at 8.2 s). Keep the
      start and cap the length.

    Sometimes the first word of a sentence is left behind before the pause and
    the second one is stretched ("One" 9.7 s, "more" 10.2-21.3 s, both said at
    21.5 s). A sentence-starting word with (almost) no sound under it cannot have
    been said there either: it is moved to where the sound resumes, and the
    words after it follow as the same sentence.

    A long word without a pause inside is just loosely timed within continuous
    speech: its start is kept and its length capped.
    """
    previous_moved = False
    for i, w in enumerate(words):
        starts_sentence = i == 0 or previous_moved or words[i - 1]["text"].endswith((".", "?", "!"))
        previous_moved = False
        after = [s for s, e in runs if s >= w["start"]]
        if w["end"] - w["start"] > max_word_s:
            if starts_sentence and _longest_silence(w["start"], w["end"], runs) >= min_pause_s:
                inside = [s for s, e in runs if w["start"] <= s < w["end"]]
                start = inside[-1] if inside else (after[0] if after else w["start"])
                w["end"] = w["end"] if inside else max(w["end"], start + 0.2)
                w["start"] = start
                previous_moved = True
            else:
                w["end"] = w["start"] + max_word_s
        elif starts_sentence and after and _sound_under(w["start"], w["end"], runs) < 0.1:
            w["start"], w["end"] = after[0], max(w["end"], after[0] + 0.1)
            previous_moved = True
    return words


def _sound_under(start, end, runs):
    """Seconds of sound between start and end."""
    return sum(max(0.0, min(e, end) - max(s, start)) for s, e in runs)


def group_words(words, max_gap_s=1.0, max_word_s=1.5):
    """Group words into segments. A new segment starts after a sentence ends,
    after a pause, or at a word that is suspiciously long.

    With VAD, whisper.cpp removes silence before transcribing, so one of its own
    segments can span a long pause (for example while someone else talks), and
    the first word after a pause gets a start time from before the pause.
    Splitting here keeps each segment inside one stretch of speech.
    """
    segments = []
    for w in words:
        current = segments[-1] if segments else None
        word = [round(w["start"], 2), round(w["end"], 2), w["text"]]
        if (current is None or current["text"].endswith((".", "?", "!"))
                or w["start"] - current["end"] > max_gap_s
                or w["end"] - w["start"] > max_word_s):
            segments.append(dict(w, words=[word]))
        else:
            current["end"] = w["end"]
            current["text"] += " " + w["text"]
            current["words"].append(word)
    # Each segment keeps its words [start, end, text], so speakers can be assigned
    # per word later (Whisper sometimes writes no punctuation at all, and then a
    # segment can hold several speakers).
    return segments
