"""Voice samples per speaker, so you can hear who is who and give them names.

After diarization, each speaker gets a short clip in speakers/ (taken from
their longest segments) and speakers.html shows a play button per speaker.
"""

import html
import re
import wave
from pathlib import Path

import numpy as np

from .output import fmt_time, speaker_name

MIN_SAMPLE_SECONDS = 6
MAX_SAMPLE_SECONDS = 12
GAP_SECONDS = 0.4


def speaker_stats(meeting):
    """{speaker: {"talk_s": total seconds, "segments": [...]}} for all speakers."""
    stats = {}
    for seg in meeting["segments"]:
        if seg.get("speaker") is None:
            continue
        s = stats.setdefault(seg["speaker"], {"talk_s": 0.0, "segments": []})
        s["talk_s"] += seg["end"] - seg["start"]
        s["segments"].append(seg)
    return dict(sorted(stats.items()))


def pick_sample(segments):
    """The longest segments until about MIN_SAMPLE_SECONDS, in time order."""
    chosen, total = [], 0.0
    for seg in sorted(segments, key=lambda s: s["end"] - s["start"], reverse=True):
        if total >= MIN_SAMPLE_SECONDS:
            break
        chosen.append(seg)
        total += seg["end"] - seg["start"]
    return sorted(chosen, key=lambda s: s["start"])


def clip_path(out_dir, speaker):
    return Path(out_dir, "speakers", f"speaker_{speaker}.wav")


def has_samples(out_dir):
    return any(Path(out_dir, "speakers").glob("speaker_*.wav"))


def audio_files(meeting, out_dir):
    """The meeting's audio files that still exist: the mix, the tracks and the raw recording."""
    names = {"audio_16k.wav", "mic.wav", "system.wav", *meeting.get("track_files", {}).values()}
    return [p for p in (Path(out_dir, n) for n in sorted(names)) if p.exists()]


def has_audio(meeting, out_dir):
    """True if the audio the speakers are found in and cut from is still there
    (it can be deleted after processing)."""
    names = set(meeting.get("track_files", {}).values()) or {"audio_16k.wav"}
    return all(Path(out_dir, n).exists() for n in names)


def make_samples(meeting, out_dir):
    """Write speakers/speaker_N.wav for every speaker, then speakers.html.
    Without the audio (deleted), the clips there are kept as they are."""
    if not has_audio(meeting, out_dir):
        write_html(meeting, out_dir)
        return
    stats = speaker_stats(meeting)
    clip_dir = Path(out_dir, "speakers")
    clip_dir.mkdir(exist_ok=True)
    for old in clip_dir.glob("speaker_*.wav"):
        old.unlink()
    if not stats:
        return
    track_files = meeting.get("track_files", {})
    for speaker, s in stats.items():
        # In a recording, cut the sample from the speaker's own track (mic or system).
        source = track_files.get(s["segments"][0].get("track"), "audio_16k.wav")
        with wave.open(str(Path(out_dir, source)), "rb") as src:
            rate, params = src.getframerate(), src.getparams()
            silence = b"\x00\x00" * int(GAP_SECONDS * rate)
            s["sample"] = pick_sample(s["segments"])
            frames = b""
            for seg in s["sample"]:
                src.setpos(int(seg["start"] * rate))
                frames += src.readframes(int((seg["end"] - seg["start"]) * rate)) + silence
            clip = np.frombuffer(frames[: MAX_SAMPLE_SECONDS * rate * 2], dtype=np.int16)
            # Normalize the volume: a headset mic can be much quieter than system audio.
            peak = np.abs(clip.astype(np.int32)).max() if len(clip) else 0
            if peak > 0:
                clip = (clip * (0.7 * 32767 / peak)).astype(np.int16)
            with wave.open(str(clip_path(out_dir, speaker)), "wb") as dst:
                dst.setparams(params)
                dst.writeframes(clip.tobytes())
    write_html(meeting, out_dir, stats)


def write_html(meeting, out_dir, stats=None):
    if not has_samples(out_dir):  # the voice samples were deleted (or never made)
        Path(out_dir, "speakers.html").unlink(missing_ok=True)
        return
    stats = stats or speaker_stats(meeting)
    rows = ""
    for speaker, s in stats.items():
        quote = " ... ".join(seg["text"] for seg in s.get("sample") or pick_sample(s["segments"]))
        rows += (f"<div class='spk'><h2>{html.escape(speaker_name(meeting, speaker))}</h2>"
                 f"<p class='meta'>{fmt_time(s['talk_s'])} talk time, {len(s['segments'])} segments</p>"
                 f"<audio controls preload='none' src='speakers/speaker_{speaker}.wav'></audio>"
                 f"<blockquote>{html.escape(quote)}</blockquote></div>")
    page = ("<!doctype html><meta charset='utf-8'><title>Speakers</title><style>"
            "body{font-family:sans-serif;margin:16px;max-width:800px}"
            ".spk{border:1px solid #ccc;padding:8px 16px;margin-bottom:12px}"
            ".meta{color:#666}blockquote{color:#333;font-style:italic}code{background:#eee}</style>"
            f"<h1>{html.escape(meeting['name'])}</h1>"
            f"<p>To name the speakers, run: <code>python lp.py rename \"{html.escape(str(out_dir))}\"</code></p>"
            + rows)
    Path(out_dir, "speakers.html").write_text(page, encoding="utf-8")


def replace_names(text, renames):
    """Replace old speaker names with new ones in text (e.g. summary.md).
    Placeholders make swaps like "Talare 1" <-> "Talare 2" safe."""
    for i, (old, _) in enumerate(renames):
        text = re.sub(r"\b" + re.escape(old) + r"\b", f"\x00{i}\x00", text, flags=re.IGNORECASE)
    for i, (_, new) in enumerate(renames):
        text = text.replace(f"\x00{i}\x00", new)
    return text
