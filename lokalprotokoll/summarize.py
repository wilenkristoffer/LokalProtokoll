"""Summarize a transcript with a local Ollama model.

Short transcripts are sent in one request. If the transcript does not fit in
num_ctx, it is split into chunks, each chunk is turned into notes, and the notes
are summarized with the normal summary prompt.
"""

import json
import re
import time
import urllib.error
import urllib.request

from .config import read_text
from .output import transcript_lines


def _post(url, body, timeout=3600):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def installed_models(cfg):
    try:
        tags = _get(cfg["summarize"]["ollama_url"] + "/api/tags")
    except urllib.error.URLError:
        raise SystemExit("Cannot reach Ollama. Is it running? (start the Ollama app)")
    return {m["name"] for m in tags.get("models", [])}


def check_model(cfg, model):
    names = installed_models(cfg)
    if model not in names and model + ":latest" not in names:
        raise SystemExit(f"Ollama model {model} is not installed. Run: ollama pull {model}")


def chat(cfg, model, prompt):
    """One chat request. Returns (text, stats)."""
    s = cfg["summarize"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": s["num_ctx"], "temperature": s["temperature"]},
    }
    if s.get("disable_thinking"):
        body["think"] = False
    url = s["ollama_url"] + "/api/chat"
    try:
        resp = _post(url, body)
    except urllib.error.HTTPError:
        if "think" not in body:
            raise
        # Some models reject the think option; retry without it.
        del body["think"]
        resp = _post(url, body)

    text = resp["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    prompt_tokens = resp.get("prompt_eval_count", 0)
    answer_tokens = resp.get("eval_count", 0)
    if prompt_tokens + answer_tokens >= s["num_ctx"] - 16:
        print(f"    WARNING: used {prompt_tokens + answer_tokens} of num_ctx={s['num_ctx']} tokens."
              " The model may not have seen the whole transcript. Raise num_ctx.")
    eval_s = resp.get("eval_duration", 0) / 1e9
    stats = {"prompt_tokens": prompt_tokens, "answer_tokens": answer_tokens,
             "tokens_per_s": round(answer_tokens / eval_s, 1) if eval_s else None}
    return text, stats


def unload(cfg, model):
    """Tell Ollama to free the model's GPU memory and RAM now. Otherwise it keeps the
    model loaded for 5 minutes, which is about 11 GB VRAM and 7 GB RAM for gemma3:12b."""
    if not cfg["summarize"].get("unload_after", True):
        return
    try:
        _post(cfg["summarize"]["ollama_url"] + "/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
    except (urllib.error.URLError, OSError):
        pass


def model_memory(cfg, model):
    """(percent of the loaded model in VRAM, VRAM in MB, total size in MB) from Ollama.
    100% means the model runs fully on the GPU; the size includes the context (num_ctx)."""
    try:
        for m in _get(cfg["summarize"]["ollama_url"] + "/api/ps").get("models", []):
            if m["name"] == model or m["name"] == model + ":latest":
                size, vram = m.get("size", 0), m.get("size_vram", 0)
                return round(100 * vram / max(size, 1)), round(vram / 2**20), round(size / 2**20)
    except (urllib.error.URLError, KeyError):
        pass
    return None, None, None


def _fill(template, meeting, transcript, **extra):
    text = (template.replace("{transcript}", transcript)
            .replace("{meeting_name}", meeting["name"])
            .replace("{date}", meeting["date"]))
    for key, value in extra.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def _split(lines, max_chars):
    chunks, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append(current)
    return ["\n".join(c) for c in chunks]


def make_title(cfg, meeting, summary, model):
    """A short title for a meeting that was not given a name, made from its minutes.
    Returns None if the model gives nothing usable."""
    lang = "sv" if meeting["language"] == "sv" else "en"
    prompt = read_text(cfg["prompts"][f"title_{lang}"]).replace("{summary}", summary)
    text, _ = chat(cfg, model, prompt)
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        return None
    title = re.sub(r"^(titel|title)\s*:\s*", "", lines[0].strip(), flags=re.IGNORECASE)
    title = title.strip(" \"'*#.`")
    return title[:70] or None


def summarize(cfg, meeting, model):
    """Return (summary_markdown, stats)."""
    s = cfg["summarize"]
    lang = "sv" if meeting["language"] == "sv" else "en"
    summary_prompt = read_text(cfg["prompts"][f"summary_{lang}"])
    lines = transcript_lines(meeting)
    transcript = "\n".join(lines)

    budget_chars = int((s["num_ctx"] - s["reserve_tokens"]) * s["chars_per_token"]) - len(summary_prompt)
    start = time.perf_counter()
    if len(transcript) <= budget_chars:
        print(f"    ~{int(len(transcript) / s['chars_per_token'])} tokens, one request")
        text, stats = chat(cfg, model, _fill(summary_prompt, meeting, transcript))
        stats["chunks"] = 1
    else:
        chunk_prompt = read_text(cfg["prompts"][f"chunk_{lang}"])
        chunks = _split(lines, budget_chars)
        print(f"    Transcript too long for num_ctx={s['num_ctx']}, using {len(chunks)} chunks")
        notes = []
        for i, chunk in enumerate(chunks, 1):
            print(f"    chunk {i}/{len(chunks)}")
            note, _ = chat(cfg, model, _fill(chunk_prompt, meeting, chunk, part=i, parts=len(chunks)))
            notes.append(f"--- {i}/{len(chunks)} ---\n{note}")
        text, stats = chat(cfg, model, _fill(summary_prompt, meeting, "\n\n".join(notes)))
        stats["chunks"] = len(chunks)
    stats["seconds"] = round(time.perf_counter() - start, 1)
    stats["gpu_percent"], stats["vram_mb"], stats["model_mb"] = model_memory(cfg, model)
    return text, stats
