"""Play a meeting's audio with pause and resume (winsound can only start and stop).

Reads the wav a little at a time from a PortAudio callback, so an hour-long
meeting is not loaded into memory. Uses PyAudioWPatch, which the recorder
already needs.
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
    def active(self):
        """True while playing or paused (i.e. until stopped or the end)."""
        return self.stream is not None

    @property
    def playing(self):
        return self.stream is not None and self.stream.is_active()

    def seconds(self):
        return self.position / self.rate, self.frames / self.rate

    def play(self, path):
        """Start playing path from the beginning (stops what was playing)."""
        self.stop()
        import pyaudiowpatch as pyaudio
        self.wav = wave.open(str(path), "rb")
        self.path, self.rate, self.frames = path, self.wav.getframerate(), self.wav.getnframes()
        self.position, self.finished = 0, False
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
        if self.playing:
            self.stream.stop_stream()

    def resume(self):
        if self.stream is not None and not self.stream.is_active() and not self.finished:
            self.stream.start_stream()

    def stop(self):
        if self.stream is not None:
            self.stream.stop_stream()
            self.stream.close()
            self.stream = None
        with self.lock:
            if self.wav is not None:
                self.wav.close()
                self.wav = None
        self.position, self.finished = 0, False
