"""What this computer has (graphics card, memory, processor) and which device
profile suits it.

A device profile is a set of models that fits a kind of computer. The heavy
step is the summary model, which wants its whole size (plus the context) in the
graphics card's own memory (VRAM); if it does not fit, Ollama runs part of it on
the processor and it gets many times slower. See PROFILES and docs/MODELS.md.

Chosen with device.profile in config.toml (or Profile in the app): "auto" picks
from the detected hardware, "custom" uses the model settings in config.toml as
they are.
"""

import ctypes
import os

# Display adapters in the registry: driver name and dedicated video memory.
DISPLAY_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
# Not real graphics cards (remote desktop, virtual displays, screen capture drivers).
NOT_A_GPU = ("basic display", "basic render", "remote", "virtual", "parsec", "citrix", "vmware", "hyper-v",
             "idd", "spacedesk", "displaylink", "mirage")
# Integrated graphics share the main memory, and Windows reports a small or made-up amount.
INTEGRATED = ("intel(r) uhd", "intel(r) hd", "intel(r) iris", "iris(r) xe", "intel(r) arc(tm) graphics",
              "radeon(tm) graphics", "radeon graphics", "radeon(tm) vega", "radeon vega", "radeon 7", "radeon 8")

GB = 2**30


def gpus():
    """[{"name", "vram_gb", "integrated"}] from the Windows registry, biggest VRAM first."""
    try:
        import winreg
    except ImportError:
        return []
    found = {}
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS)
    except OSError:
        return []
    with root:
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                with winreg.OpenKey(root, winreg.EnumKey(root, i)) as key:
                    name = str(winreg.QueryValueEx(key, "DriverDesc")[0]).strip()
                    vram = 0
                    for value in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
                        try:
                            raw = winreg.QueryValueEx(key, value)[0]
                            vram = int.from_bytes(raw, "little") if isinstance(raw, bytes) else int(raw)
                            break
                        except OSError:
                            continue
            except OSError:
                continue
            low = name.lower()
            if any(word in low for word in NOT_A_GPU):
                continue
            integrated = any(word in low for word in INTEGRATED) or vram < 2 * GB
            found[name] = {"name": name, "vram_gb": round(vram / GB, 1), "integrated": integrated}
    return sorted(found.values(), key=lambda g: (not g["integrated"], g["vram_gb"]), reverse=True)


def ram_gb():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return round(status.ullTotalPhys / GB)
    except (AttributeError, OSError):
        return 0


def has_battery():
    """True on a laptop (a battery is present), so the app can say so."""
    class PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    try:
        status = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            return False
        return status.BatteryFlag not in (128, 255)  # 128 = no battery, 255 = unknown
    except (AttributeError, OSError):
        return False


def detect():
    """Everything the profile choice needs, plus a short description to show."""
    cards = gpus()
    best = next((g for g in cards if not g["integrated"]), None)
    info = {"gpus": cards, "gpu": best["name"] if best else None, "vram_gb": best["vram_gb"] if best else 0,
            "ram_gb": ram_gb(), "threads": os.cpu_count() or 4, "laptop": has_battery()}
    info["profile"] = suggested_profile(info)
    return info


def suggested_profile(info):
    """The summary model must fit in VRAM with its context (measured, docs/MODELS.md):
    gemma4:12b 9.2 GB, gemma4:e4b 5.6 GB, gemma4:e2b 3.9 GB. Windows and other programs
    also use 1-2 GB of the card that drives the screen."""
    vram = info["vram_gb"]
    if vram >= 12:
        return "desktop"
    if vram >= 8:
        return "laptop"
    if vram >= 4:
        return "small"
    return "cpu"


def describe(info):
    gpu = f"{info['gpu']} ({info['vram_gb']:.0f} GB)" if info["gpu"] else "no separate graphics card"
    kind = "laptop" if info["laptop"] else "computer"
    return f"{gpu}, {info['ram_gb']} GB RAM, {info['threads']} threads ({kind})"


# Settings per profile. Only these keys are changed; everything else comes from
# config.toml. The measurements behind each choice are in docs/MODELS.md.
LARGE_WHISPER = {"model_sv": "models/whisper/kb-whisper-large-q5_0.bin",
                 "model_en": "models/whisper/ggml-large-v3-turbo-q5_0.bin",
                 "model_detect": "models/whisper/ggml-large-v3-turbo-q5_0.bin"}

PROFILES = {
    # Whisper large needs only 1.8 GB of VRAM, so every graphics card gets the best
    # transcription; only the summary model gets smaller.
    "desktop": {
        "label": "Desktop GPU (12 GB+)",
        "transcribe": LARGE_WHISPER,
        "summarize": {"model": "gemma4:12b", "num_ctx": 32768},
    },
    "laptop": {
        "label": "Laptop GPU (8-12 GB)",
        "transcribe": LARGE_WHISPER,
        "summarize": {"model": "gemma4:e4b", "num_ctx": 16384},
    },
    "small": {
        "label": "Small GPU (4-8 GB)",
        "transcribe": LARGE_WHISPER,
        "summarize": {"model": "gemma4:e2b", "num_ctx": 16384},
    },
    "cpu": {
        "label": "No GPU (processor only)",
        # On the processor, large Whisper models run at about real time (an hour per
        # meeting hour); small ones about 5 times faster.
        "transcribe": {"model_sv": "models/whisper/kb-whisper-small-q5_0.bin",
                       "model_en": "models/whisper/ggml-small-q5_1.bin",
                       "model_detect": "models/whisper/ggml-small-q5_1.bin"},
        # e4b over e2b: 1.5 times slower on the processor, but clearly better minutes.
        "summarize": {"model": "gemma4:e4b", "num_ctx": 16384},
    },
}


def missing_models(cfg):
    """What the models in cfg need that is not downloaded: speech model files and
    the Ollama summary model. Empty if everything is there (or Ollama is not running,
    which processing reports itself)."""
    from pathlib import Path

    from .config import resolve
    t = cfg["transcribe"]
    missing = [Path(p).name for p in dict.fromkeys((t["model_sv"], t["model_en"], t["model_detect"]))
               if not Path(resolve(p)).exists()]
    try:
        from .summarize import installed_models
        names = installed_models(cfg)
        model = cfg["summarize"]["model"]
        if model not in names and model + ":latest" not in names:
            missing.append(model)
    except SystemExit:
        pass
    return missing


def resolve_profile(cfg, info=None):
    """The profile name in use: device.profile, or the suggested one for "auto".
    Returns None for "custom"."""
    name = cfg.get("device", {}).get("profile", "auto")
    if name == "custom":
        return None
    if name not in PROFILES:
        name = (info or detect())["profile"]
    return name


def apply_profile(cfg, info=None):
    """Put the profile's model settings into cfg (in place). Returns the profile name."""
    name = resolve_profile(cfg, info)
    if name:
        for section, values in PROFILES[name].items():
            if isinstance(values, dict):
                cfg.setdefault(section, {}).update(values)
    cfg.setdefault("device", {})["active"] = name or "custom"
    return name
