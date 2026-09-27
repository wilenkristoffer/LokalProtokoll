"""Run the pipeline on the test samples and measure accuracy, optionally for
several setups ("variants") to find out which works best.

  python tests/fetch_samples.py                          # download the samples once
  python tests/benchmark.py                              # all samples, current config.toml
  python tests/benchmark.py ami_es2004a                  # one sample
  python tests/benchmark.py --variants tests/variants.toml             # compare setups
  python tests/benchmark.py --variants tests/variants.toml --only "KB medium"
  python tests/benchmark.py --set diarize.threshold=0.6 -- --speakers 3
  python tests/benchmark.py --rescore       # score the last runs again, without processing

--set SECTION.KEY=VALUE changes a config value (as in lp.py). Arguments after --
go to "lp.py process". A variants file lists named setups, each with "set" and/or
"args" (see tests/variants.toml).

Each sample folder in tests/samples/ has:
  audio.<ext>      the recording
  reference.json   [{start, end, speaker, text}, ...]
  sample.json      {"language": "sv", "speakers": 2, "description": "..."}

Results: tests/results/<variant>/<sample>/ (the processed meeting) and one line per
sample and variant in tests/results/history.csv, to compare runs over time.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "samples"
RESULTS = ROOT / "tests" / "results"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from lokalprotokoll import evaluate  # noqa: E402
from lokalprotokoll.output import load_meeting, slugify  # noqa: E402
from monitor import ResourceMonitor, hardware_info  # noqa: E402


def run_sample(sample_dir, variant):
    info = json.loads((sample_dir / "sample.json").read_text(encoding="utf-8"))
    audio = next(p for p in sample_dir.iterdir() if p.stem == "audio")
    run_dir = RESULTS / slugify(variant["name"]) / sample_dir.name
    shutil.rmtree(run_dir, ignore_errors=True)  # only the latest run is kept; history.csv keeps all
    sets = [f"paths.meetings_dir={run_dir.as_posix()}"] + variant.get("set", [])
    cmd = [sys.executable, str(ROOT / "lp.py")]
    for s in sets:
        cmd += ["--set", s]
    cmd += ["process", str(audio), "--name", sample_dir.name, "--lang", info["language"], "--no-summary"]
    if info.get("speakers"):
        cmd += ["--speakers", str(info["speakers"])]
    cmd += variant.get("args", [])

    print(f"  {sample_dir.name} ...", end="", flush=True)
    monitor = ResourceMonitor()
    monitor.start()  # records idle GPU memory before the work starts
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", env=dict(os.environ, PYTHONUNBUFFERED="1"))
    monitor.attach(proc.pid)
    lines = [(time.time(), line.rstrip()) for line in proc.stdout]  # timestamped as they arrive
    proc.wait()
    monitor.stop()
    folder = next((line[len("Output: "):].strip() for _, line in lines if line.startswith("Output: ")), None)
    if proc.returncode != 0 or not folder:
        print(" FAILED")
        print("\n".join(line for _, line in lines[-30:]))
        return None

    meeting = load_meeting(folder)
    result = evaluate.evaluate(meeting, evaluate.load_reference(sample_dir / "reference.json"))
    seconds = sum(meeting.get("timings", {}).values())
    result.update(sample=sample_dir.name, variant=variant["name"], folder=folder, seconds=round(seconds, 1),
                  date=datetime.now().strftime("%Y-%m-%d %H:%M"),
                  audio_minutes=round(meeting["duration_s"] / 60, 1),
                  realtime=round(meeting["duration_s"] / seconds, 1) if seconds else None,
                  resources=stage_resources(lines, monitor))
    Path(folder, "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    t, d = result["resources"].get("transcribe", {}), result["resources"].get("diarize", {})
    print(f" WER {result['wer']:.1%}  cpWER {result['cpwer']:.1%}  DER {result['der']:.1%}"
          f"  speakers {result['found_speakers']}/{result['reference_speakers']}  {result['realtime']}x realtime"
          f"  | transcribe VRAM {t.get('peak_vram_mb')} MB, diarize RAM {d.get('peak_ram_mb')} MB")
    return result


def stage_resources(lines, monitor):
    """Resource use per stage. lp.py prints "[transcribe]" when a stage starts and
    "[transcribe] 12.3 s" when it ends; "transcribe mic/system" count as transcribe."""
    stages, open_stages = {}, {}
    for t, line in lines:
        m = re.fullmatch(r"\[([a-z ]+)\]( [\d.]+ s)?", line.strip())
        if not m:
            continue
        name = m.group(1).split()[0]
        if m.group(2) is None:
            open_stages[m.group(1)] = t
        elif m.group(1) in open_stages:
            stages.setdefault(name, []).append((open_stages.pop(m.group(1)), t))
    result = {}
    for name, spans in stages.items():
        parts = [monitor.summary(a, b) for a, b in spans]
        result[name] = {
            "peak_ram_mb": max((p["peak_ram_mb"] or 0) for p in parts),
            "avg_cpu_pct": max((p["avg_cpu_pct"] or 0) for p in parts),
            "peak_vram_mb": max((p["peak_vram_mb"] or 0) for p in parts),
            "avg_gpu_pct": max((p["avg_gpu_pct"] or 0) for p in parts),
        }
    if lines:
        result["total"] = monitor.summary(lines[0][0], lines[-1][0])
    return result


def load_variants(args, extra):
    if not args.variants:
        name = args.name or ("current config" + (" + " + " ".join(args.set + extra) if args.set or extra else ""))
        return [{"name": name, "set": args.set, "args": extra}]
    variants = tomllib.loads(Path(args.variants).read_text(encoding="utf-8"))["variant"]
    for v in variants:
        v["set"] = list(v.get("set", [])) + args.set
        v["args"] = list(v.get("args", [])) + extra
    if args.only:
        variants = [v for v in variants if v["name"] in args.only]
    return variants


def print_table(results):
    """Per variant: every sample, plus the average weighted by reference words."""
    print(f"\n{'Variant':<28}{'Sample':<24}{'WER':>7}{'cpWER':>8}{'DER':>7}{'speakers':>10}{'speed':>9}")
    for name in dict.fromkeys(r["variant"] for r in results):
        rows = [r for r in results if r["variant"] == name]
        for r in rows:
            print(f"{name[:27]:<28}{r['sample'][:23]:<24}{r['wer']:>7.1%}{r['cpwer']:>8.1%}{r['der']:>7.1%}"
                  f"{r['found_speakers']:>6}/{r['reference_speakers']:<3}{r['realtime']:>8}x")
        if len(rows) > 1:
            words = sum(r["reference_words"] for r in rows)
            avg = {k: sum(r[k] * r["reference_words"] for r in rows) / words for k in ("wer", "cpwer", "der")}
            speed = sum(r["audio_minutes"] for r in rows) * 60 / max(sum(r["seconds"] for r in rows), 1)
            print(f"{'':<28}{'= average':<24}{avg['wer']:>7.1%}{avg['cpwer']:>8.1%}{avg['der']:>7.1%}"
                  f"{'':>10}{speed:>8.1f}x")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("samples", nargs="*", help="Sample names (default: all)")
    parser.add_argument("--variants", help="TOML file with the setups to compare")
    parser.add_argument("--only", action="append", help="Run only this variant from the file (repeatable)")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="Config override for every run")
    parser.add_argument("--name", help="Name for this run in history.csv (without --variants)")
    parser.add_argument("--note", default="", help="Free text saved in history.csv")
    parser.add_argument("--rescore", action="store_true",
                        help="Only score the runs in the last report again (after changing evaluate.py)")
    args, extra = parser.parse_known_args()
    extra = [a for a in extra if a != "--"]
    if args.rescore:
        return rescore(args.note)

    names = args.samples
    if not names and SAMPLES.exists():
        names = sorted(p.name for p in SAMPLES.iterdir() if (p / "reference.json").exists())
    if not names:
        raise SystemExit("No samples found. Run: python tests/fetch_samples.py")

    results = []
    for variant in load_variants(args, extra):
        print(f"\n=== {variant['name']}")
        # A variant with language = "sv" only runs on Swedish samples (e.g. Swedish models).
        chosen = [n for n in names if not variant.get("language")
                  or json.loads((SAMPLES / n / "sample.json").read_text(encoding="utf-8"))["language"]
                  == variant["language"]]
        results += [r for r in (run_sample(SAMPLES / name, variant) for name in chosen) if r]
    if not results:
        raise SystemExit("Nothing was processed.")

    RESULTS.mkdir(parents=True, exist_ok=True)
    history = RESULTS / "history.csv"
    new_file = not history.exists()
    with open(history, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["date", "variant", "sample", "wer", "cpwer", "der", "speakers_found",
                             "speakers_ref", "audio_min", "seconds", "realtime", "transcribe_vram_mb",
                             "transcribe_gpu_pct", "transcribe_ram_mb", "diarize_ram_mb", "diarize_cpu_pct",
                             "peak_ram_mb", "note"])
        for r in results:
            t, d, total = (r["resources"].get(k, {}) for k in ("transcribe", "diarize", "total"))
            writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), r["variant"], r["sample"], r["wer"],
                             r["cpwer"], r["der"], r["found_speakers"], r["reference_speakers"],
                             r["audio_minutes"], r["seconds"], r["realtime"], t.get("peak_vram_mb"),
                             t.get("avg_gpu_pct"), t.get("peak_ram_mb"), d.get("peak_ram_mb"),
                             d.get("avg_cpu_pct"), total.get("peak_ram_mb"), args.note])
    print_table(results)
    report = write_report(collect_saved(), args.note)
    print(f"\nLower is better for WER, cpWER and DER.\nHistory: {history}\nReport:  {report} "
          "(latest result of every variant and sample)")


def _average(rows, key):
    words = sum(r["reference_words"] for r in rows)
    return sum(r[key] * r["reference_words"] for r in rows) / words if words else 0


def _stage_speed(rows, prefixes):
    """Minutes of audio per minute spent in the stages whose names start with prefixes."""
    seconds = 0.0
    for r in rows:
        timings = load_meeting(r["folder"]).get("timings", {})
        seconds += sum(v for k, v in timings.items() if k.startswith(prefixes))
    return sum(r["audio_minutes"] for r in rows) * 60 / max(seconds, 0.1)


def collect_saved():
    """The latest result of every variant and sample, from the evaluation.json files
    in tests/results/<variant>/<sample>/<meeting>/, in the order of tests/variants.toml."""
    results = []
    for path in RESULTS.glob("*/*/*/evaluation.json"):
        r = json.loads(path.read_text(encoding="utf-8"))
        if "variant" not in r or not Path(r["folder"], "meeting.json").exists():
            continue
        r.setdefault("date", datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
        results.append(r)
    latest = {}
    for r in sorted(results, key=lambda r: r["date"]):
        latest[(r["variant"], r["sample"])] = r
    order = []
    variants_file = ROOT / "tests" / "variants.toml"
    if variants_file.exists():
        order = [v["name"] for v in tomllib.loads(variants_file.read_text(encoding="utf-8")).get("variant", [])]
    rank = {name: i for i, name in enumerate(order)}
    return sorted(latest.values(), key=lambda r: (rank.get(r["variant"], len(rank)), r["variant"], r["sample"]))


def rescore(note):
    """Score all saved runs again with the current evaluate.py, without processing
    anything (e.g. after changing how text is compared)."""
    results = []
    for r in collect_saved():
        fresh = evaluate.evaluate(load_meeting(r["folder"]),
                                  evaluate.load_reference(SAMPLES / r["sample"] / "reference.json"))
        r = dict(r, **fresh)
        Path(r["folder"], "evaluation.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(r)
    print_table(results)
    report = write_report(results, (note or "") + " (rescored)")
    print(f"\nReport: {report}")


def write_report(results, note=""):
    """tests/results/report.md (and report.json): hardware, a summary per variant and
    language, and every sample in detail."""
    hw = hardware_info()
    samples = {r["sample"]: json.loads((SAMPLES / r["sample"] / "sample.json").read_text(encoding="utf-8"))
               for r in results}
    gpu = ", ".join(f"{g['name']} ({g['vram_gb']} GB, driver {g.get('driver', '?')})" for g in hw.get("gpus", []))
    lines = ["# Benchmark report", "",
             f"Written: {datetime.now():%Y-%m-%d %H:%M}" + (f" - {note}" if note else "")
             + ". Each variant shows its latest run; the dates are in the table at the end.", "",
             f"Hardware: {hw.get('cpu', '?')} ({hw.get('cores', '?')} cores / {hw.get('threads', '?')} threads), "
             f"{hw.get('ram_gb', '?')} GB RAM, {gpu or 'GPU unknown'}, {hw.get('os', '')}", "",
             "Lower is better for WER, cpWER and DER. Speeds are minutes of audio per minute of processing: "
             "for transcription (including language detection), for speaker detection, and in total (no "
             "summary). VRAM is the extra GPU memory during transcription; RAM is the peak of the whole run. "
             "CPU is the average load of all cores during speaker detection.", "",
             "## Summary per variant", "",
             "| Variant | Language | WER | cpWER | DER | Speed total | Transcribe | Speakers | VRAM (transcribe) "
             "| GPU load | RAM (peak) | CPU (speakers) | Whisper model |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for variant in dict.fromkeys(r["variant"] for r in results):
        for language in ("sv", "en"):
            rows = [r for r in results if r["variant"] == variant and samples[r["sample"]]["language"] == language]
            if not rows:
                continue
            res = [r["resources"] for r in rows]
            speed = sum(r["audio_minutes"] for r in rows) * 60 / max(sum(r["seconds"] for r in rows), 1)
            stt_speed, spk_speed = _stage_speed(rows, ("transcribe", "detect")), _stage_speed(rows, ("diarize",))
            model = Path(load_meeting(rows[0]["folder"])["whisper_model"])
            size = f"{model.stat().st_size / 2**30:.2f} GB" if model.exists() else "?"
            vram = max((x.get("transcribe", {}).get("peak_vram_mb") or 0) for x in res)
            gpu_load = max((x.get("transcribe", {}).get("avg_gpu_pct") or 0) for x in res)
            ram = max((x.get("total", {}).get("peak_ram_mb") or 0) for x in res)
            cpu = max((x.get("diarize", {}).get("avg_cpu_pct") or 0) for x in res)
            lines.append(f"| {variant} | {language} | {_average(rows, 'wer'):.1%} | {_average(rows, 'cpwer'):.1%} | "
                         f"{_average(rows, 'der'):.1%} | {speed:.1f}x | {stt_speed:.1f}x | {spk_speed:.1f}x | {vram} MB | {gpu_load:.0f}% | {ram} MB | "
                         f"{cpu:.0f}% | {model.name} ({size}) |")
    lines += ["", "## Every sample", "",
              "| Variant | Sample | WER | cpWER | DER | Speakers found/real | Minutes | Seconds | Speed | Measured |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['variant']} | {r['sample']} | {r['wer']:.1%} | {r['cpwer']:.1%} | {r['der']:.1%} | "
                     f"{r['found_speakers']}/{r['reference_speakers']} | {r['audio_minutes']} | {r['seconds']:.0f} | "
                     f"{r['realtime']}x | {r.get('date', '')} |")
    lines += ["", "## Samples", ""] + [f"- **{name}**: {info.get('description', '')}" for name, info in samples.items()]
    path = RESULTS / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (RESULTS / "report.json").write_text(json.dumps({"hardware": hw, "note": note, "results": results},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    return path


if __name__ == "__main__":
    main()
