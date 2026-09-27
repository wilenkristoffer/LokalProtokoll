"""Measure accuracy against a reference transcript.

The reference is a JSON list of segments:
    [{"start": 12.3, "end": 15.0, "speaker": "Anna", "text": "..."}, ...]

Metrics:
- WER: word error rate, ignoring speakers. (substitutions + deletions + insertions)
  divided by the number of reference words. 0.10 means about 1 word in 10 is wrong.
- cpWER: speaker-attributed WER. All words of one speaker are compared with all
  words of the speaker they were matched to ("Talare 1" -> "Anna"), using the
  matching that gives the fewest errors. Words given to the wrong person count
  as errors.
- DER: diarization error rate, on time. Speech given to the wrong speaker, missed
  speech and speech where nobody spoke, divided by the total reference speech time.

Text is compared after lowercasing, removing punctuation and removing filler
sounds (um, uh, mm-hmm, oeh...). Numbers written differently ("20 000" vs
"tjugotusen") still count as errors.
"""

import itertools
import json
import re
from pathlib import Path

import numpy as np

from .merge import group_paragraphs


# Filler sounds are removed on both sides, as is usual when scoring meetings: word-for-word
# references (AMI) write every "um" and "mm-hmm", while Whisper usually leaves them out.
FILLERS = {"um", "uh", "umm", "uhm", "erm", "er", "ah", "hm", "hmm", "mm", "mhm", "mmhmm", "uhhuh",
           "eh", "\xf6h", "\xe4h", "\xf6hm", "\xe4hm", "mmm"}


def normalize(text):
    text = re.sub(r"\b(mm|uh)-(hmm|huh)\b", r"\1\2", text.lower())
    return [w for w in re.sub(r"[^\w]+", " ", text).split() if w not in FILLERS]


def edit_distance(ref, hyp):
    """Word-level Levenshtein distance, one numpy row at a time."""
    if not ref:
        return len(hyp)
    if not hyp:
        return len(ref)
    vocab = {w: i for i, w in enumerate(set(ref) | set(hyp))}
    h = np.array([vocab[w] for w in hyp])
    cols = np.arange(len(hyp) + 1)
    row = cols.copy()
    for i, word in enumerate(ref, 1):
        sub = row[:-1] + (h != vocab[word])        # substitution (or match)
        dele = row[1:] + 1                          # deletion
        new = np.empty_like(row)
        new[0] = i
        new[1:] = np.minimum(sub, dele)
        # insertion: new[j] = min over k <= j of new[k] + (j - k)
        new = cols + np.minimum.accumulate(new - cols)
        row = new
    return int(row[-1])


def _best_assignment(cost):
    """Minimum-cost matching of rows to columns (square matrix). Exact for up to 8
    speakers, greedy above that."""
    n = cost.shape[0]
    if n <= 8:
        best = min(itertools.permutations(range(n)), key=lambda p: sum(cost[i, p[i]] for i in range(n)))
        return list(best)
    free, result = set(range(n)), []
    for i in range(n):
        j = min(free, key=lambda c: cost[i, c])
        free.remove(j)
        result.append(j)
    return result


def _by_speaker(segments):
    words = {}
    for s in segments:
        words.setdefault(str(s.get("speaker")), []).extend(normalize(s["text"]))
    return words


def cp_wer(ref_segments, hyp_segments):
    """Returns (errors, reference word count, {hyp speaker: ref speaker})."""
    ref, hyp = _by_speaker(ref_segments), _by_speaker(hyp_segments)
    ref_names, hyp_names = list(ref), list(hyp)
    n = max(len(ref_names), len(hyp_names))
    cost = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            r = ref[ref_names[i]] if i < len(ref_names) else []
            h = hyp[hyp_names[j]] if j < len(hyp_names) else []
            cost[i, j] = edit_distance(r, h)
    assignment = _best_assignment(cost)
    errors = int(sum(cost[i, assignment[i]] for i in range(n)))
    mapping = {hyp_names[assignment[i]]: ref_names[i]
               for i in range(len(ref_names)) if assignment[i] < len(hyp_names)}
    return errors, sum(len(w) for w in ref.values()), mapping


def der(ref_segments, hyp_segments, step=0.01, collar=0.25):
    """Frame-based diarization error rate with the usual 0.25 s forgiveness collar
    around reference speaker changes. Returns fractions of the reference speech time:
    {"der", "missed", "false_alarm", "confusion"}; der is the sum of the other three."""
    end = max(s["end"] for s in ref_segments + hyp_segments)
    n = int(end / step) + 1

    def frames(segments):
        tracks = {}
        for s in segments:
            track = tracks.setdefault(str(s.get("speaker")), np.zeros(n, dtype=bool))
            track[int(s["start"] / step):int(s["end"] / step)] = True
        return tracks

    ref, hyp = frames(ref_segments), frames(hyp_segments)
    scored = np.ones(n, dtype=bool)
    for s in ref_segments:
        for t in (s["start"], s["end"]):
            scored[max(0, int((t - collar) / step)):int((t + collar) / step)] = False

    ref_names, hyp_names = list(ref), list(hyp)
    k = max(len(ref_names), len(hyp_names))
    overlap = np.zeros((k, k))
    for i, r in enumerate(ref_names):
        for j, h in enumerate(hyp_names):
            overlap[i, j] = np.sum(ref[r] & hyp[h] & scored)
    assignment = _best_assignment(-overlap)
    correct = sum(overlap[i, assignment[i]] for i in range(k))
    n_ref = sum(np.sum(t & scored) for t in ref.values())
    ref_count = sum(t.astype(int) for t in ref.values()) * scored
    hyp_count = sum(t.astype(int) for t in hyp.values()) * scored if hyp else np.zeros(n)
    if not n_ref:
        return {"der": 0.0, "missed": 0.0, "false_alarm": 0.0, "confusion": 0.0}
    parts = {
        "missed": np.sum(np.maximum(ref_count - hyp_count, 0)),        # speech nobody was given
        "false_alarm": np.sum(np.maximum(hyp_count - ref_count, 0)),   # "speech" where nobody spoke
        "confusion": np.sum(np.minimum(ref_count, hyp_count)) - correct,  # given to the wrong speaker
    }
    result = {k: round(float(v / n_ref), 4) for k, v in parts.items()}
    result["der"] = round(sum(result.values()), 4)
    return result


def evaluate(meeting, reference):
    ref_words = [w for s in reference for w in normalize(s["text"])]
    hyp_words = [w for s in meeting["segments"] for w in normalize(s["text"])]
    wer_errors = edit_distance(ref_words, hyp_words)
    hyp = [dict(s, speaker=s.get("speaker")) for s in meeting["segments"]]
    cp_errors, n_ref, mapping = cp_wer(reference, hyp)
    # Speaker turns (consecutive sentences by the same speaker joined), as a
    # diarization system reports them; pauses between sentences are not errors.
    diarization = der(reference, group_paragraphs(hyp))
    return {
        "reference_words": len(ref_words),
        "hypothesis_words": len(hyp_words),
        "wer": round(wer_errors / len(ref_words), 4) if ref_words else None,
        "cpwer": round(cp_errors / n_ref, 4) if n_ref else None,
        "der": diarization["der"],
        "der_parts": {k: v for k, v in diarization.items() if k != "der"},
        "reference_speakers": len({s["speaker"] for s in reference}),
        "found_speakers": len({s.get("speaker") for s in hyp if s.get("speaker") is not None}),
        "speaker_mapping": mapping,
    }


def load_reference(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data["segments"] if isinstance(data, dict) else data
