"""Measure what the app uses in everyday conditions (with your other programs open),
using LibreHardwareMonitor for the whole system and psutil for the app itself.

Needs LibreHardwareMonitor running with its web server on (Options > Remote Web
Server > Run, port 8085). Takes about 3 minutes:

  1. baseline     20 s, app not running
  2. app idle     20 s, window open
  3. recording    60 s, mic + system audio (a test clip is played)
  4. processing   transcription, speaker detection and summary of that recording
  5. after        15 s after processing (is the memory freed?)

  python tests/measure_live.py                 # as above
  python tests/measure_live.py --record 120    # record longer

The window opens and records for real. The test meeting is saved in
tests/results/live/ and the results in tests/results/live_usage.md.
"""

import argparse
import json
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
RESULTS = ROOT / "tests" / "results"

LHM_URL = "http://localhost:8085/data.json"
# key: (sensor id starts with, sensor type, sensor names). The first match is used,
# so the list covers AMD, Intel and NVIDIA naming.
SENSORS = {
    "cpu_load": (("/amdcpu/", "/intelcpu/"), "Load", ("CPU Total",)),
    "cpu_watt": (("/amdcpu/", "/intelcpu/"), "Power", ("Package", "CPU Package")),
    "ram_used_gb": (("/ram/",), "Data", ("Memory Used",)),
    "gpu_load": (("/gpu-",), "Load", ("GPU Core",)),
    "vram_used_mb": (("/gpu-",), "SmallData", ("GPU Memory Used", "D3D Dedicated Memory Used")),
    "gpu_watt": (("/gpu-",), "Power", ("GPU Package", "GPU Power")),
}


def read_lhm():
    """One reading of the sensors above, as numbers."""
    data = json.loads(urllib.request.urlopen(LHM_URL, timeout=3).read())
    leaves = []

    def walk(node):
        if node.get("SensorId"):
            leaves.append(node)
        for kid in node.get("Children", []):
            walk(kid)

    walk(data)
    found = {}
    for key, (prefixes, sensor_type, names) in SENSORS.items():
        for name in names:
            node = next((n for n in leaves if n["SensorId"].startswith(prefixes) and n.get("Type") == sensor_type
                         and n.get("Text") == name), None)
            if node:
                try:
                    found[key] = float(node["Value"].split()[0].replace(",", "."))
                    break
                except (ValueError, IndexError):
                    pass
    return found


class Sampler:
    """Every second: system values from LibreHardwareMonitor, and RAM/CPU of this
    process, its children (whisper, lp.py) and Ollama."""

    def __init__(self):
        self.samples = []
        self.stop = threading.Event()
        self.procs = {}

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _our_processes(self):
        me = psutil.Process()
        tree = [me] + me.children(recursive=True)
        tree += [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("ollama")]
        return tree

    def _run(self):
        cpus = psutil.cpu_count() or 1
        while not self.stop.is_set():
            t = time.time()
            try:
                sample = read_lhm()
            except OSError:
                sample = {}
            ram = cpu = 0.0
            for p in self._our_processes():
                try:
                    proc = self.procs.setdefault(p.pid, p)
                    cpu += proc.cpu_percent(None)
                    ram += proc.memory_info().rss
                except psutil.Error:
                    pass
            sample.update(t=t, app_ram_mb=ram / 2**20, app_cpu=cpu / cpus)
            self.samples.append(sample)
            time.sleep(max(0.0, 1.0 - (time.time() - t)))


def summarize_phase(samples, start, end, baseline):
    rows = [s for s in samples if start <= s["t"] <= end]
    if not rows:
        return {}

    def avg(key):
        vals = [s[key] for s in rows if key in s]
        return sum(vals) / len(vals) if vals else None

    def peak(key):
        vals = [s[key] for s in rows if key in s]
        return max(vals) if vals else None

    out = {"seconds": round(end - start), "cpu_load_avg": avg("cpu_load"), "cpu_load_max": peak("cpu_load"),
           "ram_used_gb_max": peak("ram_used_gb"), "gpu_load_avg": avg("gpu_load"), "gpu_load_max": peak("gpu_load"),
           "vram_used_mb_max": peak("vram_used_mb"), "cpu_watt_avg": avg("cpu_watt"), "gpu_watt_avg": avg("gpu_watt"),
           "app_cpu_avg": avg("app_cpu"), "app_cpu_max": peak("app_cpu"), "app_ram_mb_max": peak("app_ram_mb")}
    if baseline:
        for key, base in (("ram_used_gb_max", "ram_used_gb"), ("vram_used_mb_max", "vram_used_mb")):
            if out.get(key) is not None and baseline.get(base) is not None:
                out[key.replace("_max", "_extra")] = out[key] - baseline[base]
    return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in out.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--record", type=int, default=60, help="Seconds to record (default 60)")
    parser.add_argument("--baseline", type=int, default=20, help="Seconds for the baseline and idle phases")
    args = parser.parse_args()

    try:
        first = read_lhm()
    except OSError:
        raise SystemExit("LibreHardwareMonitor's web server is not reachable on http://localhost:8085 "
                         "(Options > Remote Web Server > Run).")
    missing = [k for k in SENSORS if k not in first]
    print("Sensors found:", ", ".join(f"{k}={v}" for k, v in first.items()), "| missing:", missing or "none")

    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    import customtkinter as ctk
    from lokalprotokoll import app as appmod
    from lokalprotokoll.config import load_config

    sampler = Sampler()
    sampler.start()
    phases = {}
    t0 = time.time()
    print(f"1/4 baseline ({args.baseline} s, app not running)")
    time.sleep(args.baseline)
    phases["1 baseline (app not running)"] = (t0, time.time())

    cfg = load_config()
    cfg["paths"]["meetings_dir"] = str(RESULTS / "live")
    ctk.set_appearance_mode("system")
    app = appmod.App(cfg, tray=False)
    app.name_entry.insert(0, "Live usage test")
    app.speakers_seg.set("1")
    marks = {}
    clip = ROOT / "tests" / "samples" / "synthetic_en" / "audio.wav"

    def play_clips():
        import winsound
        while app.worker.status["state"] == "recording":
            winsound.PlaySound(str(clip), winsound.SND_FILENAME)

    def step():
        now = time.time()
        state = app.worker.status["state"]
        if "idle_start" not in marks:
            marks["idle_start"] = now
            print(f"2/4 app open, idle ({args.baseline} s)")
        elif "rec_start" not in marks and now - marks["idle_start"] >= args.baseline:
            marks["rec_start"] = now
            print(f"3/4 recording ({args.record} s)")
            app.toggle_record()
            if clip.exists():
                app.after(1000, lambda: threading.Thread(target=play_clips, daemon=True).start())
        elif "rec_end" not in marks and "rec_start" in marks and now - marks["rec_start"] >= args.record:
            marks["rec_end"] = now
            print("4/4 processing (transcription, speakers, summary)")
            app.toggle_record()
        elif "rec_end" in marks and "done" not in marks and state in ("done", "error"):
            marks["done"] = now
            marks["folder"] = app.worker.status.get("folder")
            marks["state"] = state
            print("   done; measuring 15 s more to see if the memory is freed")
        elif "done" in marks and now - marks["done"] >= 15:
            marks["after_end"] = now
            app.destroy()
            return
        app.after(250, step)

    app.after(500, step)
    app.mainloop()
    time.sleep(1)
    sampler.stop.set()

    phases["2 app open, idle"] = (marks["idle_start"], marks["rec_start"])
    phases["3 recording"] = (marks["rec_start"] + 2, marks["rec_end"])
    phases["4 processing"] = (marks["rec_end"], marks["done"])
    phases["5 after processing, app open"] = (marks["done"] + 3, marks["after_end"])
    base_rows = [s for s in sampler.samples if phases["1 baseline (app not running)"][0] <= s["t"]
                 <= phases["1 baseline (app not running)"][1]]
    baseline = {k: sum(s[k] for s in base_rows if k in s) / max(1, sum(1 for s in base_rows if k in s))
                for k in ("ram_used_gb", "vram_used_mb")}
    results = {name: summarize_phase(sampler.samples, a, b, baseline) for name, (a, b) in phases.items()}

    # Stages inside the processing phase, from the meeting's own timings.
    timings, disk = {}, {}
    folder = Path(marks["folder"]) if marks.get("folder") else None
    if folder and (folder / "meeting.json").exists():
        timings = json.loads((folder / "meeting.json").read_text(encoding="utf-8")).get("timings", {})
        for f in folder.glob("*.wav"):
            disk[f.name] = round(f.stat().st_size / 2**20, 1)

    out = {"date": datetime.now().isoformat(timespec="minutes"), "record_seconds": args.record,
           "result": marks.get("state"), "phases": results, "stage_seconds": timings, "files_mb": disk,
           "samples": sampler.samples}
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "live_usage.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    write_markdown(out)


def write_markdown(out):
    def f(v, unit=""):
        return "-" if v is None else f"{v:g}{unit}"
    lines = [f"# Resource use in everyday conditions ({out['date']})", "",
             "System values from LibreHardwareMonitor (the whole computer, with the other programs that were open). "
             "'App' is LokalProtokoll itself: the window, the recorder, whisper, lp.py and Ollama.", "",
             "| Phase | CPU total avg / max | App CPU avg / max | RAM used (extra) | App RAM | GPU load avg / max "
             "| VRAM used (extra) | CPU W | GPU W |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, p in out["phases"].items():
        lines.append(f"| {name} ({p.get('seconds')} s) | {f(p.get('cpu_load_avg'), '%')} / {f(p.get('cpu_load_max'), '%')} "
                     f"| {f(p.get('app_cpu_avg'), '%')} / {f(p.get('app_cpu_max'), '%')} "
                     f"| {f(p.get('ram_used_gb_max'), ' GB')} ({f(p.get('ram_used_gb_extra'), ' GB')}) "
                     f"| {f(p.get('app_ram_mb_max'), ' MB')} "
                     f"| {f(p.get('gpu_load_avg'), '%')} / {f(p.get('gpu_load_max'), '%')} "
                     f"| {f(p.get('vram_used_mb_max'), ' MB')} ({f(p.get('vram_used_mb_extra'), ' MB')}) "
                     f"| {f(p.get('cpu_watt_avg'))} | {f(p.get('gpu_watt_avg'))} |")
    if out["stage_seconds"]:
        lines += ["", "Processing stages (seconds): " + ", ".join(f"{k} {v}" for k, v in out["stage_seconds"].items())]
    if out["files_mb"]:
        lines += ["", "Audio files kept for this recording (MB): " + ", ".join(f"{k} {v}" for k, v in out["files_mb"].items())]
    path = RESULTS / "live_usage.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nSaved: {path}")


if __name__ == "__main__":
    main()
