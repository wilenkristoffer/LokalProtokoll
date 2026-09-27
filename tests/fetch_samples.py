"""Download test recordings with correct reference transcripts into tests/samples/.

  python tests/fetch_samples.py            # all samples (about 120 MB)
  python tests/fetch_samples.py riksdag_hd108

Samples:
  ami_es2004a     English project meeting, 4 people, 17.5 min (AMI corpus, CC BY 4.0).
                  Word-for-word reference with a time for every word.
  ami_is1009a     English project meeting, 4 people, 14 min (AMI corpus, CC BY 4.0).
  riksdag_hd108   Swedish parliament debate, 3 people taking turns plus the chair, 25.5 min
                  (Riksdagen open data). The reference is the official protocol,
                  which is lightly edited, not word for word: WER looks worse than
                  it is. Good for comparing models and for speaker separation.
  synthetic_en    Two Windows text-to-speech voices, 70 s. Made locally.

Each sample gets audio.<ext>, reference.json ([{start, end, speaker, text}]) and
sample.json ({"language", "speakers", "description"}). Existing files are kept.
"""

import html
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
import wave
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
CACHE = ROOT / "tests" / "samples" / "_downloads"

AMI_AUDIO = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/{m}/audio/{m}.Mix-Headset.wav"
AMI_ANNOTATIONS = "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/ami_public_manual_1.6.2.zip"
RIKSDAG_PAGE = "https://www.riksdagen.se/sv/webb-tv/video/interpellationsdebatt/x_{id}/"


def download(url, dest):
    dest = Path(dest)
    if dest.exists():
        print(f"  exists: {dest.relative_to(ROOT)}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "LokalProtokoll-tests"})
    with urllib.request.urlopen(request, timeout=120) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)
    return dest


def save_sample(name, reference, info):
    out = SAMPLES / name
    out.mkdir(parents=True, exist_ok=True)
    (out / "reference.json").write_text(json.dumps(reference, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "sample.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    words = sum(len(s["text"].split()) for s in reference)
    print(f"  {name}: {len(reference)} segments, {words} words, "
          f"{len({s['speaker'] for s in reference})} speakers")


# ----- AMI -----

def ami(meeting):
    name = f"ami_{meeting.lower()}"
    download(AMI_AUDIO.format(m=meeting), SAMPLES / name / "audio.wav")
    archive = download(AMI_ANNOTATIONS, CACHE / "ami_public_manual_1.6.2.zip")
    with zipfile.ZipFile(archive) as z:
        speakers = _ami_speaker_names(z.read("corpusResources/meetings.xml"), meeting)
        reference = []
        for letter, global_name in speakers.items():
            words = _ami_words(z.read(f"words/{meeting}.{letter}.words.xml"))
            segments = ET.fromstring(z.read(f"segments/{meeting}.{letter}.segments.xml"))
            for seg in segments.iter("segment"):
                child = next(iter(seg), None)
                href = child.get("href", "") if child is not None else ""
                ids = re.findall(r"id\(([^)]+)\)", href)
                if not ids:
                    continue
                span = _ami_range(words, ids[0], ids[-1])
                if span:
                    reference.append({"start": span[0]["start"], "end": span[-1]["end"],
                                      "speaker": global_name, "text": " ".join(w["text"] for w in span)})
    reference.sort(key=lambda s: s["start"])
    save_sample(name, reference, {"language": "en", "speakers": len(speakers),
                                  "description": f"AMI meeting {meeting}, English, headset mix, word-for-word reference"})


def _ami_speaker_names(xml, meeting):
    """{"A": "MEO015", ...} for one meeting."""
    root = ET.fromstring(xml)
    for m in root.iter("meeting"):
        if m.get("observation") == meeting:
            return {s.get("nxt_agent"): s.get("global_name") for s in m.iter("speaker")}
    raise SystemExit(f"{meeting} not found in meetings.xml")


def _ami_words(xml):
    """Ordered list of {"id", "start", "end", "text"} (punctuation is skipped)."""
    words = []
    for w in ET.fromstring(xml).iter("w"):
        if w.get("punc") == "true" or not w.get("starttime") or not (w.text or "").strip():
            continue
        wid = next(v for k, v in w.attrib.items() if k.endswith("id"))
        words.append({"id": wid, "start": float(w.get("starttime")), "end": float(w.get("endtime")),
                      "text": w.text.strip()})
    return words


def _ami_range(words, first_id, last_id):
    index = {w["id"]: i for i, w in enumerate(words)}
    # Ranges can start or end on a skipped element (punctuation): widen to the nearest word.
    def position(wid, forward):
        if wid in index:
            return index[wid]
        num = int(re.search(r"(\d+)$", wid).group(1))
        prefix = wid[: -len(str(num))]
        for step in range(1, 50):
            candidate = f"{prefix}{num + step if forward else num - step}"
            if candidate in index:
                return index[candidate]
        return None
    a, b = position(first_id, True), position(last_id, False)
    return words[a:b + 1] if a is not None and b is not None and a <= b else []


# ----- Riksdagen -----

def riksdag(debate_id="hd108"):
    name = f"riksdag_{debate_id.lower()}"
    page = download(RIKSDAG_PAGE.format(id=debate_id.lower()), CACHE / f"riksdag_{debate_id}.html")
    text = page.read_text(encoding="utf-8")
    data = json.loads(re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                                text, re.S).group(1))
    video = _find_key(data, "audioUrl")
    speeches = data["props"]["pageProps"]["contentApiData"]["speakers"]
    download(video["audioUrl"], SAMPLES / name / "audio.mp3")
    reference = []
    for s in speeches:
        body = html.unescape(re.sub(r"<[^>]+>", " ", s.get("speechText") or ""))
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            reference.append({"start": float(s["startPosition"]),
                              "end": float(s["startPosition"]) + float(s["speechSeconds"]),
                              "speaker": s.get("speakerShort") or s["speaker"], "text": body})
    # The chair (talman) also speaks between the speeches but is not in the protocol,
    # so there is one more voice than there are speakers in the reference.
    save_sample(name, reference, {
        "language": "sv", "speakers": len({s["speaker"] for s in reference}) + 1,
        "description": f"Riksdagen interpellation debate {debate_id.upper()}, Swedish, 3 debaters and the chair. "
                       "Reference is the official (lightly edited) protocol, timed per speech"})


def _find_key(obj, key):
    """The first dict inside obj (nested JSON) that has key."""
    if isinstance(obj, dict):
        if key in obj:
            return obj
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_key(child, key)
        if found:
            return found
    return None


# ----- synthetic -----

SYNTHETIC_LINES = [
    ("Hazel", "Good morning everyone. Let us start the weekly project meeting. The main topic today is the website launch."),
    ("Zira", "Thanks. The design is finished, but the payment integration is still not tested. I think we need one more week."),
    ("Hazel", "One more week is fine. So we move the launch date from October first to October eighth. Does everyone agree?"),
    ("Zira", "Yes, I agree. I will finish the payment tests by Friday."),
    ("Hazel", "Good. I will inform the customer about the new date today. What about the marketing budget?"),
    ("Zira", "We have not decided yet. I suggest we ask finance for twenty thousand euros, but we need numbers from last year first."),
    ("Hazel", "Okay, the budget question stays open until next week. Anything else? No? Then we are done. Thank you."),
]


def synthetic():
    """Two Windows TTS voices taking turns, with 1 s between turns."""
    name = "synthetic_en"
    if (SAMPLES / name / "audio.wav").exists():
        print(f"  exists: tests/samples/{name}/audio.wav")
        return
    tmp = Path(tempfile.mkdtemp())
    script = ["Add-Type -AssemblyName System.Speech"]
    for i, (voice, text) in enumerate(SYNTHETIC_LINES):
        script.append(f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                      f"$s.SelectVoice('Microsoft {voice} Desktop'); "
                      f"$s.SetOutputToWaveFile('{tmp / f'{i}.wav'}'); $s.Speak('{text}'); $s.Dispose()")
    result = subprocess.run(["powershell", "-NoProfile", "-Command", "; ".join(script)], capture_output=True, text=True)
    if result.returncode != 0:
        print("  skipped: the Windows voices Hazel and Zira are not installed")
        return
    clips, t, reference = [], 1.0, []
    for i, (voice, text) in enumerate(SYNTHETIC_LINES):
        with wave.open(str(tmp / f"{i}.wav"), "rb") as w:
            rate = w.getframerate()
            clip = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        reference.append({"start": round(t, 2), "end": round(t + len(clip) / rate, 2), "speaker": voice, "text": text})
        clips.append((t, clip))
        t += len(clip) / rate + 1.0
    mix = np.zeros(int((t + 1) * rate), dtype=np.int16)
    for start, clip in clips:
        mix[int(start * rate):int(start * rate) + len(clip)] = clip
    (SAMPLES / name).mkdir(parents=True, exist_ok=True)
    with wave.open(str(SAMPLES / name / "audio.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(mix.tobytes())
    save_sample(name, reference, {"language": "en", "speakers": 2,
                                  "description": "Two Windows TTS voices taking turns, 70 s"})


SOURCES = {"ami_es2004a": lambda: ami("ES2004a"), "ami_is1009a": lambda: ami("IS1009a"),
           "riksdag_hd108": lambda: riksdag("hd108"), "synthetic_en": synthetic}


def main():
    names = sys.argv[1:] or list(SOURCES)
    for name in names:
        if name not in SOURCES:
            raise SystemExit(f"Unknown sample {name}. Choose from: {', '.join(SOURCES)}")
        print(f"\n{name}")
        SOURCES[name]()
    print("\nDone. Run: python tests/benchmark.py")


if __name__ == "__main__":
    main()
