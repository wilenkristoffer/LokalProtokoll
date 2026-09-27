"""Find the best diarize.threshold for guessing the number of speakers ("Auto").

Uses the latest benchmark runs of the default variant: the transcripts are kept,
only speaker detection is redone for each threshold, so it is much faster than a
full benchmark. Run it again after changing the voice model.

  python tests/benchmark.py --variants tests/variants.toml --only "Default (KB large, TitaNet small)"
  python tests/tune_threshold.py                     # thresholds 0.8 ... 1.2
  python tests/tune_threshold.py 0.6 0.65 0.7        # others
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lokalprotokoll import evaluate  # noqa: E402
from lokalprotokoll.output import load_meeting  # noqa: E402

RUNS = ROOT / "tests" / "results" / "Default-KB-large-TitaNet-small"
SAMPLES = ROOT / "tests" / "samples"


def main():
    thresholds = [float(t) for t in sys.argv[1:]] or [0.8, 0.9, 1.0, 1.1, 1.2]
    folders = sorted(RUNS.glob("*/*/meeting.json"))
    if not folders:
        raise SystemExit(f"No runs in {RUNS}. Run the benchmark for the default variant first.")
    work = Path(tempfile.mkdtemp(prefix="lp_threshold_"))
    rows = []
    for meeting_json in folders:
        sample = meeting_json.parent.parent.name
        real = json.loads((SAMPLES / sample / "sample.json").read_text(encoding="utf-8"))["speakers"]
        reference = evaluate.load_reference(SAMPLES / sample / "reference.json")
        copy = work / sample
        shutil.copytree(meeting_json.parent, copy)
        for t in thresholds:
            subprocess.run([sys.executable, str(ROOT / "lp.py"), "rediarize", str(copy), "--speakers", "0",
                            "--threshold", str(t)], capture_output=True, check=True)
            r = evaluate.evaluate(load_meeting(copy), reference)
            rows.append((sample, t, r["found_speakers"], real, r["cpwer"], r["der"]))
            print(f"{sample:<16} threshold {t:<5} speakers {r['found_speakers']:>2}/{real}   "
                  f"cpWER {r['cpwer']:.1%}   DER {r['der']:.1%}", flush=True)
    shutil.rmtree(work, ignore_errors=True)

    print("\nAverage over the samples (lower is better):")
    for t in thresholds:
        mine = [row for row in rows if row[1] == t]
        off = sum(abs(row[2] - row[3]) for row in mine) / len(mine)
        print(f"  threshold {t:<5} cpWER {sum(r[4] for r in mine) / len(mine):.1%}   "
              f"DER {sum(r[5] for r in mine) / len(mine):.1%}   speaker count off by {off:.1f} on average")


if __name__ == "__main__":
    main()
