"""Download the models LokalProtokoll needs into models/.

  python setup_models.py                      # the models for this computer (device profile)
  python setup_models.py --profile cpu        # the models for another profile (desktop, laptop, small, cpu)
  python setup_models.py --kb-size medium     # also try a smaller KB-Whisper
  python setup_models.py --kb-variant strict  # more verbatim transcripts

Files that already exist are skipped.
"""

import argparse
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WHISPER_DIR = ROOT / "models" / "whisper"
DIAR_DIR = ROOT / "models" / "diarization"

HF = "https://huggingface.co"
SHERPA = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


def download(url, dest):
    dest = Path(dest)
    if dest.exists():
        print(f"  exists: {dest.relative_to(ROOT)}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    print(f"  downloading {url}")

    last = [-1]

    def progress(blocks, block_size, total):
        if total > 0:
            done = min(blocks * block_size, total)
            percent = 100 * done // total
            if percent != last[0]:
                last[0] = percent
                sys.stdout.write(f"\r    {done / 1e6:8.1f} / {total / 1e6:.1f} MB")
                sys.stdout.flush()

    urllib.request.urlretrieve(url, tmp, progress)
    print()
    tmp.replace(dest)
    return dest


def kb_whisper(size, quant, variant):
    revision = "main" if variant == "standard" else variant
    remote = "ggml-model-q5_0.bin" if quant == "q5_0" else "ggml-model.bin"
    name = f"kb-whisper-{size}"
    if variant != "standard":
        name += f"-{variant}"
    if quant == "q5_0":
        name += "-q5_0"
    dest = download(f"{HF}/KBLab/kb-whisper-{size}/resolve/{revision}/{remote}", WHISPER_DIR / f"{name}.bin")
    return dest


def whisper_file(path):
    """Download a Whisper model named in config.toml or a device profile, from its
    file name: kb-whisper-<size>-q5_0.bin (KBLab) or ggml-<name>.bin (whisper.cpp)."""
    name = Path(path).name
    if name.startswith("kb-whisper-"):
        size = name[len("kb-whisper-"):].split("-")[0].removesuffix(".bin")
        return kb_whisper(size, "q5_0" if name.endswith("-q5_0.bin") else "full",
                          "strict" if "-strict" in name else "standard")
    if name.startswith("ggml-"):
        return download(f"{HF}/ggerganov/whisper.cpp/resolve/main/{name}", WHISPER_DIR / name)
    raise SystemExit(f"Do not know where to download {name}; put it in models/whisper yourself.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["auto", "desktop", "laptop", "small", "cpu"],
                        help="Download the speech models of this device profile (default: device.profile in "
                             "config.toml; auto = chosen from this computer's graphics card)")
    parser.add_argument("--kb-size", choices=["tiny", "base", "small", "medium", "large"],
                        help="Download this KB-Whisper size instead of the profile's")
    parser.add_argument("--kb-quant", default="q5_0", choices=["q5_0", "full"])
    # Note: for "large", the whisper.cpp file of the "strict" variant is identical to "standard".
    parser.add_argument("--kb-variant", default="standard", choices=["standard", "strict"])
    parser.add_argument("--compare", action="store_true",
                        help="Also download the alternative models used in tests/variants.toml (about 1.9 GB)")
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from lokalprotokoll import hardware
    from lokalprotokoll.config import load_config

    cfg = load_config(profile=False)
    if args.profile:
        cfg.setdefault("device", {})["profile"] = args.profile
    info = hardware.detect()
    profile = hardware.apply_profile(cfg, info)
    print(f"This computer: {hardware.describe(info)}")
    print(f"Device profile: {hardware.PROFILES[profile]['label'] if profile else 'custom (config.toml)'}\n")
    t = cfg["transcribe"]

    print("Swedish speech model (KB-Whisper):")
    if args.kb_size:
        kb = kb_whisper(args.kb_size, args.kb_quant, args.kb_variant)
    else:
        kb = whisper_file(t["model_sv"])

    print("English + language detection model (Whisper):")
    for path in dict.fromkeys((t["model_en"], t["model_detect"])):
        whisper_file(path)

    print("Voice activity detection (Silero):")
    download(f"{HF}/ggml-org/whisper-vad/resolve/main/ggml-silero-v6.2.0.bin",
             WHISPER_DIR / "ggml-silero-v6.2.0.bin")

    print("Speaker diarization (sherpa-onnx):")
    seg_dir = DIAR_DIR / "sherpa-onnx-pyannote-segmentation-3-0"
    if not (seg_dir / "model.onnx").exists():
        archive = download(f"{SHERPA}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
                           DIAR_DIR / "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
        with tarfile.open(archive) as tar:
            tar.extractall(DIAR_DIR)
        archive.unlink()
    else:
        print(f"  exists: {seg_dir.relative_to(ROOT)}")
    # Note: "recongition" is spelled that way in the real release URL.
    download(f"{SHERPA}/speaker-recongition-models/nemo_en_titanet_small.onnx",
             DIAR_DIR / "nemo_en_titanet_small.onnx")

    if args.compare:
        print("Alternative models for comparing (tests/variants.toml):")
        kb_whisper("medium", "q5_0", "standard")
        kb_whisper("small", "q5_0", "standard")
        for name in ("3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
                     "wespeaker_en_voxceleb_resnet34_LM.onnx",
                     "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"):
            download(f"{SHERPA}/speaker-recongition-models/{name}", DIAR_DIR / name)

    if args.kb_size and kb.name != Path(t["model_sv"]).name:
        print(f"\nTo use {kb.name}, set in config.toml:\n  [device] profile = \"custom\"\n"
              f"  model_sv = \"models/whisper/{kb.name}\"")
    print("\nDone.")


if __name__ == "__main__":
    main()
