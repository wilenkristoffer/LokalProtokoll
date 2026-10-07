"""Play a meeting's audio with pause, seek and skip (winsound can only start and stop).

Reads the wav a little at a time from a PortAudio callback, so an hour-long
meeting is not loaded into memory. Uses PyAudioWPatch, which the recorder
already needs.

The file stays open from load() until close(); the output stream only exists
while playing. Pausing closes it and keeps the position, so play, pause, seek
and playing again after the end all work the same way.
"""

import threading
import wave


class Player:
    def __init__(self):
        self.pa = None
        self.stream = None
        self.wav = None
        self.path = None
        self.frames = 0
        self.rate = 16000
        self.position = 0  # frames played
        self.finished = False
        self.lock = threading.Lock()

    @property
    def loaded(self):
        return self.wav is not None

    @property
    def playing(self):
        return self.stream is not None and self.stream.is_active()

    def seconds(self):
        """(position, length) in seconds."""
        return self.position / self.rate, self.frames / self.rate

    def load(self, path):
        """Open path, paused at the start (closes what was open)."""
        self.close()
        self.wav = wave.open(str(path), "rb")
        self.path, self.rate, self.frames = path, self.wav.getframerate(), self.wav.getnframes()
        self.position, self.finished = 0, False

    def play(self):
        """Play from the current position (from the start again after the end)."""
        if self.wav is None or self.playing:
            return
        import pyaudiowpatch as pyaudio
        self._close_stream()
        if self.finished or self.position >= self.frames:
            self.seek(0)
        if self.pa is None:
            self.pa = pyaudio.PyAudio()
        width, channels = self.wav.getsampwidth(), self.wav.getnchannels()

        def callback(in_data, count, time_info, status):
            with self.lock:
                if self.wav is None:
                    return b"", pyaudio.paComplete
                data = self.wav.readframes(count)
                self.position += len(data) // (width * channels)
            if len(data) < count * width * channels:
                self.finished = True
                return data, pyaudio.paComplete
            return data, pyaudio.paContinue

        self.stream = self.pa.open(format=self.pa.get_format_from_width(width), channels=channels, rate=self.rate,
                                   output=True, stream_callback=callback)

    def pause(self):
        self._close_stream()

    def seek(self, seconds):
        """Jump to a time in seconds (clamped to the length); keeps playing if it was."""
        if self.wav is None:
            return
        frame = max(0, min(int(seconds * self.rate), self.frames))
        with self.lock:
            self.wav.setpos(frame)
            self.position = frame
            self.finished = frame >= self.frames

    def close(self):
        self._close_stream()
        with self.lock:
            if self.wav is not None:
                self.wav.close()
                self.wav = None
        self.position, self.frames, self.finished = 0, 0, False

    def _close_stream(self):
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
