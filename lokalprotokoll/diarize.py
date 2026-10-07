"""Speaker diarization: who spoke when, with sherpa-onnx (diarize.backend = "sherpa",
or "none" to skip it).

Returns a list of turns: {"start": s, "end": s, "speaker": "0"}.
Speaker ids are arbitrary; merge.py renames them to Speaker 1, 2, ... in order
of first appearance.
"""

import sys
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE, read_wav_float32
from .config import resolve


def diarize(cfg, wav_path, backend=None, num_speakers=None, threshold=None):
    d = dict(cfg["diarize"])
    if threshold is not None:
        d["threshold"] = threshold
    backend = backend or d["backend"]
    if num_speakers is None:
        num_speakers = d.get("num_speakers", 0)
    if backend == "sherpa":
        return _sherpa(d, wav_path, num_speakers)
    if backend == "none":
        return []
    raise SystemExit(f"Unknown diarization backend: {backend}")


def _sherpa(d, wav_path, num_speakers):
    import sherpa_onnx

    seg_model = resolve(d["segmentation_model"])
    emb_model = resolve(d["embedding_model"])
    for path in (seg_model, emb_model):
        if not Path(path).exists():
            raise SystemExit(f"Diarization model not found: {path}\nRun: python setup_models.py")

    threads = d.get("threads", 4)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=seg_model),
            num_threads=threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model, num_threads=threads),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers if num_speakers > 0 else -1,
            threshold=d.get("threshold", 0.5),
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise SystemExit("Invalid sherpa-onnx diarization config (check model paths)")

    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    if sd.sample_rate != SAMPLE_RATE:
        raise SystemExit(f"Diarization model expects {sd.sample_rate} Hz audio")
    audio = read_wav_float32(wav_path)

    def progress(done, total):
        sys.stdout.write(f"\r    progress = {100 * done // max(total, 1):3d}%")
        sys.stdout.flush()
        return 0

    result = sd.process(audio, callback=progress).sort_by_start_time()
    print()
    turns = [{"start": r.start, "end": r.end, "speaker": str(r.speaker)} for r in result]
    margin = d.get("refine_margin", 0.2)
    # Only after automatic counting: a number given by the user is kept as it is.
    cleanup = num_speakers <= 0 and d.get("merge_similar", 0) > 0
    if margin > 0 or cleanup:
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=emb_model, num_threads=threads))
        embeddings = [_embed(extractor, audio, t["start"], t["end"]) if t["end"] - t["start"] >= MIN_EMBED_S
                      else None for t in turns]
        if margin > 0:
            turns, moved = refine_turns(turns, embeddings, margin)
            if moved:
                print(f"    Voice check: moved {moved} turns to a speaker they sound more like")
        if cleanup:
            before = len({t["speaker"] for t in turns})
            turns = merge_same_voices(turns, embeddings, d["merge_similar"])
            turns = absorb_small_speakers(turns, embeddings, d.get("min_speaker_s", 8.0))
            after = len({t["speaker"] for t in turns})
            if after < before:
                print(f"    Same voice check: {before} speakers -> {after}")
    return turns


# Turns shorter than this get no voice fingerprint (too little sound to say anything).
MIN_EMBED_S = 0.5


def _embed(extractor, audio, start, end, longest_s=20.0):
    """Normalized voice fingerprint of audio[start:end]. Long pieces use their middle
    part: TitaNet cannot take very long input, and 20 s is plenty for a voice."""
    if end - start > longest_s:
        mid = (start + end) / 2
        start, end = mid - longest_s / 2, mid + longest_s / 2
    stream = extractor.create_stream()
    stream.accept_waveform(SAMPLE_RATE, audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)])
    stream.input_finished()
    e = np.array(extractor.compute(stream))
    return e / (np.linalg.norm(e) or 1.0)


def refine_turns(turns, embeddings, margin, min_s=0.8, rounds=2):
    """Check every voice turn against the average voice of each speaker and move it
    if it sounds clearly more like another speaker (by at least margin).
    embeddings: the voice fingerprint of each turn, or None.

    The segmentation step sometimes treats a quick reply by someone else as the
    same voice ("Yeah, I only have one eye." / "Oh, my God, I'm sorry." / "Me
    too."), and then the reply never gets its own fingerprint. Checking each turn
    on its own catches that. Turns shorter than min_s are too short to judge."""
    turns = [dict(t) for t in turns]
    embeddings = [e if t["end"] - t["start"] >= min_s else None for t, e in zip(turns, embeddings)]
    totals, counts = {}, {}
    for t, e in zip(turns, embeddings):
        if e is not None:
            totals[t["speaker"]] = totals.get(t["speaker"], 0) + (t["end"] - t["start"]) * e
            counts[t["speaker"]] = counts.get(t["speaker"], 0) + 1
    moved = 0
    for _ in range(rounds):
        for t, e in zip(turns, embeddings):
            # A speaker's last turn is never moved, so the check cannot make a
            # speaker disappear (someone who only says one short thing).
            if e is None or counts[t["speaker"]] <= 1:
                continue
            weight = t["end"] - t["start"]
            scores = {}
            for speaker, total in totals.items():
                # Leave the turn itself out of its own speaker's average.
                voice = total - weight * e if speaker == t["speaker"] else total
                norm = np.linalg.norm(voice)
                scores[speaker] = float(e @ voice / norm) if norm else -1.0
            best = max(scores, key=scores.get)
            if best != t["speaker"] and scores[best] - scores[t["speaker"]] >= margin:
                totals[t["speaker"]] = totals[t["speaker"]] - weight * e
                totals[best] = totals[best] + weight * e
                counts[t["speaker"]] -= 1
                counts[best] += 1
                t["speaker"] = best
                moved += 1
    return turns, moved



def _voices(turns, embeddings):
    """Each speaker's average voice (normalized, weighted by turn length; None if
    no turn has a fingerprint) and total speaking time."""
    totals, seconds = {}, {}
    for t, e in zip(turns, embeddings):
        length = t["end"] - t["start"]
        seconds[t["speaker"]] = seconds.get(t["speaker"], 0.0) + length
        if e is not None:
            totals[t["speaker"]] = totals.get(t["speaker"], 0) + length * e
    voices = {}
    for speaker in seconds:
        norm = np.linalg.norm(totals.get(speaker, 0))
        voices[speaker] = totals[speaker] / norm if norm else None
    return voices, seconds


def merge_same_voices(turns, embeddings, min_similarity):
    """Join speakers whose average voices are this similar (cosine, 0-1): the
    clustering often splits one person in two, e.g. when they sound different
    over a bad connection. Always joins the most similar pair first.

    On the test recordings and real online meetings, two different people were
    at most 0.44 alike, and one person split in two 0.73-0.82."""
    turns = [dict(t) for t in turns]
    while True:
        voices, seconds = _voices(turns, embeddings)
        speakers = [s for s, v in voices.items() if v is not None]
        pairs = [(float(voices[a] @ voices[b]), a, b) for i, a in enumerate(speakers) for b in speakers[i + 1:]]
        similarity, a, b = max(pairs, default=(-1.0, None, None))
        if similarity < min_similarity:
            return turns
        keep, gone = (a, b) if seconds[a] >= seconds[b] else (b, a)
        for t in turns:
            if t["speaker"] == gone:
                t["speaker"] = keep


def absorb_small_speakers(turns, embeddings, min_seconds):
    """A "speaker" who talks less than min_seconds in the whole meeting is almost
    always a piece of someone else: a cough, a laugh, two people talking at once.
    A few seconds of sound give no reliable voice, so automatic counting makes
    many of those. Each of their turns goes to the speaker it sounds most like
    (or, with no fingerprint, the speaker of the nearest turn in time)."""
    voices, seconds = _voices(turns, embeddings)
    real = [s for s in seconds if seconds[s] >= min_seconds and voices[s] is not None]
    if not real or min_seconds <= 0:
        return turns
    turns = [dict(t) for t in turns]
    small = [i for i, t in enumerate(turns) if t["speaker"] not in real]
    for i in small:
        if embeddings[i] is not None:
            turns[i]["speaker"] = max(real, key=lambda s: float(embeddings[i] @ voices[s]))
    kept = [t for t in turns if t["speaker"] in real]
    for i in small:
        t = turns[i]
        if t["speaker"] not in real:
            middle = (t["start"] + t["end"]) / 2
            t["speaker"] = min(kept, key=lambda k: abs((k["start"] + k["end"]) / 2 - middle))["speaker"]
    return turns
