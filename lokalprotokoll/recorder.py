"""Record the microphone and the system audio (WASAPI loopback) as two wav files.

In an online meeting the mic track is you and the system track is everyone
else. Both tracks share one start time, so their timestamps line up.
"""

import json
import math
import queue
import sys
import time
import wave
from datetime import datetime

import numpy as np


def _pyaudio():
    try:
        import pyaudiowpatch
    except ImportError:
        raise SystemExit("PyAudioWPatch is not installed: pip install -r requirements.txt")
    return pyaudiowpatch


def _microphones(p, pyaudio):
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    for i in range(p.get_device_count()):
        d = p.get_device_info_by_index(i)
        if d["hostApi"] == wasapi["index"] and d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice"):
            yield d


def find_mic(p, pyaudio, name):
    if not name:
        index = p.get_host_api_info_by_type(pyaudio.paWASAPI)["defaultInputDevice"]
        if index < 0:
            raise SystemExit("No default microphone found. See: python lp.py devices")
        return p.get_device_info_by_index(index)
    for d in _microphones(p, pyaudio):
        if name.lower() in d["name"].lower():
            return d
    raise SystemExit(f"No microphone matching '{name}'. See: python lp.py devices")


def find_loopback(p, name):
    if not name:
        return p.get_default_wasapi_loopback()
    for d in p.get_loopback_device_info_generator():
        if name.lower() in d["name"].lower():
            return d
    raise SystemExit(f"No output device matching '{name}'. See: python lp.py devices")


def list_devices():
    pyaudio = _pyaudio()
    p = pyaudio.PyAudio()
    try:
        default_mic = find_mic(p, pyaudio, "")["index"]
        default_out = p.get_default_wasapi_loopback()["index"]
        print("Microphones (record.mic_device):")
        for d in _microphones(p, pyaudio):
            print(f"  {'*' if d['index'] == default_mic else ' '} {d['name']}")
        print("System audio / speakers (record.speaker_device):")
        for d in p.get_loopback_device_info_generator():
            print(f"  {'*' if d['index'] == default_out else ' '} {d['name']}")
        print("* = Windows default, used when the setting is empty")
    finally:
        p.terminate()


class Track:
    """One input stream, downmixed to mono and written to a 16-bit wav file."""

    def __init__(self, p, pyaudio, device, path, start_time):
        self.name = device["name"]
        self.rate = int(device["defaultSampleRate"])
        self.channels = device["maxInputChannels"]
        self.start = start_time
        self.frames = 0
        self.peak = 0.0
        self.queue = queue.Queue()
        self._continue = pyaudio.paContinue
        self.wav = wave.open(str(path), "wb")
        self.wav.setnchannels(1)
        self.wav.setsampwidth(2)
        self.wav.setframerate(self.rate)
        self.stream = p.open(format=pyaudio.paInt16, channels=self.channels, rate=self.rate,
                             input=True, input_device_index=device["index"],
                             frames_per_buffer=self.rate // 10, stream_callback=self._callback)

    def _callback(self, data, frame_count, time_info, status):
        # Runs in the audio thread: only queue the data, write it from the main thread.
        self.queue.put((time.perf_counter(), data))
        return (None, self._continue)

    def _write(self, samples):
        self.wav.writeframes(samples.tobytes())
        self.frames += len(samples)

    def write_pending(self):
        while True:
            try:
                arrived, data = self.queue.get_nowait()
            except queue.Empty:
                return
            samples = np.frombuffer(data, dtype=np.int16)
            if self.channels > 1:
                samples = samples.reshape(-1, self.channels).mean(axis=1).astype(np.int16)
            if len(samples) == 0:
                continue
            # WASAPI loopback sends nothing while no sound is playing. Fill such
            # gaps with silence so this track stays in sync with the other one.
            should_start_at = int((arrived - self.start) * self.rate) - len(samples)
            gap = should_start_at - self.frames
            if gap > self.rate // 5:
                self._write(np.zeros(gap, dtype=np.int16))
            self._write(samples)
            self.peak = max(self.peak, np.abs(samples.astype(np.int32)).max() / 32768)

    def finish(self, end_time):
        self.stream.stop_stream()
        self.stream.close()
        self.write_pending()
        missing = int((end_time - self.start) * self.rate) - self.frames
        if missing > 0:
            self._write(np.zeros(missing, dtype=np.int16))
        self.wav.close()


def _level(peak):
    """Peak sample (0-1) as a meter level from 0.0 (-50 dB or less) to 1.0 (0 dB)."""
    return 0.0 if peak <= 0 else round(max(0.0, 1 + 20 * math.log10(peak) / 50), 3)


def _meter(level, width=12):
    filled = int(level * width)
    return "[" + "#" * filled + " " * (width - filled) + "]"


def record(out_dir, name, mic_name="", speaker_name="", mic_only=False, max_seconds=None,
           stop_event=None, status=None, auto_name=False):
    """Record until Ctrl+C, stop_event is set, or max_seconds. Writes mic.wav,
    system.wav and recording.json in out_dir and returns the recording info.
    auto_name: no name was given, so a title is made from the minutes later.
    status (a dict) is kept up to date with elapsed time and levels for the UI;
    without it a level meter is printed instead."""
    pyaudio = _pyaudio()
    p = pyaudio.PyAudio()
    tracks = {}
    started = datetime.now()
    try:
        mic = find_mic(p, pyaudio, mic_name)
        loopback = None if mic_only else find_loopback(p, speaker_name)
        start = time.perf_counter()
        tracks["mic"] = Track(p, pyaudio, mic, out_dir / "mic.wav", start)
        if loopback:
            tracks["system"] = Track(p, pyaudio, loopback, out_dir / "system.wav", start)
        for key, t in tracks.items():
            print(f"  {key:<7} {t.name}")
        print("Recording. Press Ctrl+C to stop.\n")
        try:
            while max_seconds is None or time.perf_counter() - start < max_seconds:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(0.1 if status is not None else 0.25)
                elapsed = time.perf_counter() - start
                levels = {}
                for key, t in tracks.items():
                    t.write_pending()
                    levels[key] = _level(t.peak)
                    t.peak = 0.0
                if status is not None:
                    status.update(elapsed=round(elapsed, 1), levels=levels)
                else:
                    e = int(elapsed)
                    line = f"\r  {e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}"
                    for key, level in levels.items():
                        line += f"   {key} {_meter(level)}"
                    sys.stdout.write(line)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            pass
        end = time.perf_counter()
        print("\n  Stopped.")
        for t in tracks.values():
            t.finish(end)
    finally:
        p.terminate()
    info = {"name": name, "auto_name": auto_name, "started": started.isoformat(timespec="seconds"),
            "duration_s": round(end - start, 1), "devices": {k: t.name for k, t in tracks.items()}}
    (out_dir / "recording.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info
