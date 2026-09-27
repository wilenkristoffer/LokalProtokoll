"""Audio conversion (ffmpeg) and wav reading."""

import subprocess
import wave

import numpy as np

SAMPLE_RATE = 16000


def convert_to_wav(ffmpeg, src, dst):
    """Convert any audio/video file to 16 kHz mono 16-bit wav."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(src), "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        raise SystemExit(f"ffmpeg not found ({ffmpeg}). Install it or set paths.ffmpeg in config.toml.")
    except subprocess.CalledProcessError:
        raise SystemExit(f"ffmpeg failed to convert {src}")


def mix_to_wav(ffmpeg, sources, dst):
    """Mix several wav files into one 16 kHz mono wav (used for listening and
    language detection when mic and system audio are recorded separately)."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    for src in sources:
        cmd += ["-i", str(src)]
    cmd += ["-filter_complex", f"amix=inputs={len(sources)}:duration=longest:normalize=0",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, check=True)


def peak(path):
    """Loudest sample in a 16-bit wav, from 0.0 (silent) to 1.0."""
    with wave.open(str(path), "rb") as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return float(np.abs(samples.astype(np.int32)).max()) / 32768 if len(samples) else 0.0


def sound_runs(path, frame_s=0.02, min_sound_s=0.1, below_loudest_db=30):
    """Stretches of sound in a track as [(start, end), ...] in seconds.

    "Sound" is relative to the track itself: anything within below_loudest_db
    of its loud parts. Microphone levels differ a lot (a headset mic can be
    30 dB quieter than system audio), so a fixed threshold does not work.
    Sounds shorter than min_sound_s (clicks, keyboard) are ignored.
    """
    samples = read_wav_float32(path)
    frame = int(frame_s * SAMPLE_RATE)
    n = len(samples) // frame
    rms = np.sqrt(np.mean(samples[: n * frame].reshape(n, frame) ** 2, axis=1))
    nonzero = rms[rms > 0]
    if len(nonzero) == 0:
        return []
    threshold = np.percentile(nonzero, 98) * 10 ** (-below_loudest_db / 20)
    runs, run_start = [], None
    for i, loud in enumerate(np.append(rms > threshold, False)):
        if loud and run_start is None:
            run_start = i
        elif not loud and run_start is not None:
            if i - run_start >= min_sound_s / frame_s:
                runs.append((round(run_start * frame_s, 2), round(i * frame_s, 2)))
            run_start = None
    return runs


def trim_to_sound(segments, runs):
    """Move each segment's start/end inward to the first/last sound inside it."""
    for seg in segments:
        inside = [(s, e) for s, e in runs if s < seg["end"] and e > seg["start"]]
        if inside:
            seg["start"] = max(seg["start"], inside[0][0])
            seg["end"] = min(seg["end"], inside[-1][1])
    return segments


def wav_duration(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def read_wav_float32(path):
    """Return mono samples as float32 in [-1, 1]. Expects the 16 kHz wav made above."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != SAMPLE_RATE:
            raise ValueError(f"{path} must be 16 kHz mono 16-bit wav")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
