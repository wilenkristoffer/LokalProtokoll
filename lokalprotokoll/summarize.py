"""Summarize a transcript with a local Ollama model.

Short transcripts are sent in one request. If the transcript does not fit in
num_ctx (or is longer than chunk_tokens), it is split into chunks, each chunk is
turned into notes, and the notes are summarized with the normal summary prompt.

Then the draft is checked against its source in a second request (verify), and
review_notes() lists what a person should look at before sharing: numbers and
names that are not in the transcript, and sensitive details.
"""

import json
import re
import time
import urllib.error
import urllib.request

from .config import read_text
from .output import participants, transcript_lines


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


def chat(cfg, model, prompt, temperature=None, max_tokens=None):
    """One chat request. Returns (text, stats). max_tokens caps the answer: at
    temperature 0 a model can get stuck repeating a line until num_ctx is full."""
    s = cfg["summarize"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": s["num_ctx"],
                    "temperature": s["temperature"] if temperature is None else temperature},
    }
    if max_tokens:
        body["options"]["num_predict"] = max_tokens
    if s.get("num_gpu") is not None:  # layers on the GPU; 0 = run on the processor only
        body["options"]["num_gpu"] = s["num_gpu"]
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
    model loaded for 5 minutes, which is 11-13 GB VRAM and 8-10 GB RAM for the 12B models."""
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


def _prompt_path(cfg, key):
    """Prompt files added later may be missing from an older config.toml."""
    return cfg["prompts"].get(key, f"prompts/{key}.txt")


def _fill(template, meeting, transcript, **extra):
    text = (template.replace("{transcript}", transcript)
            .replace("{meeting_name}", meeting["name"])
            .replace("{date}", meeting["date"])
            .replace("{participants}", ", ".join(participants(meeting))))
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


def minutes_language(cfg, meeting):
    """"sv" or "en": summarize.language in config.toml, or with "auto" the language
    spoken in the meeting."""
    lang = cfg["summarize"].get("language", "auto")
    if lang not in ("sv", "en"):
        lang = meeting["language"]
    return "sv" if lang == "sv" else "en"


def make_title(cfg, meeting, summary, model):
    """A short title for a meeting that was not given a name, made from its minutes.
    Returns None if the model gives nothing usable."""
    lang = minutes_language(cfg, meeting)
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
    lang = minutes_language(cfg, meeting)
    summary_prompt = read_text(cfg["prompts"][f"summary_{lang}"])
    lines = transcript_lines(meeting)
    transcript = "\n".join(lines)

    budget_chars = int((s["num_ctx"] - s["reserve_tokens"]) * s["chars_per_token"]) - len(summary_prompt)
    if s.get("chunk_tokens"):
        budget_chars = min(budget_chars, int(s["chunk_tokens"] * s["chars_per_token"]))
    start = time.perf_counter()
    if len(transcript) <= budget_chars:
        print(f"    ~{int(len(transcript) / s['chars_per_token'])} tokens, one request")
        source = transcript
        text, stats = chat(cfg, model, _fill(summary_prompt, meeting, transcript))
        stats["chunks"] = 1
    else:
        chunk_prompt = read_text(cfg["prompts"][f"chunk_{lang}"])
        chunks = _split(lines, budget_chars)
        print(f"    ~{int(len(transcript) / s['chars_per_token'])} tokens, using {len(chunks)} chunks")
        notes = []
        for i, chunk in enumerate(chunks, 1):
            print(f"    chunk {i}/{len(chunks)}")
            note, _ = chat(cfg, model, _fill(chunk_prompt, meeting, chunk, part=i, parts=len(chunks)))
            notes.append(f"--- {i}/{len(chunks)} ---\n{note}")
        source = "\n\n".join(notes)
        text, stats = chat(cfg, model, _fill(summary_prompt, meeting, source))
        stats["chunks"] = len(chunks)
    if s.get("verify", True):
        text = verify(cfg, meeting, model, text, source)
    text = _checkboxes(text)
    stats["seconds"] = round(time.perf_counter() - start, 1)
    stats["gpu_percent"], stats["vram_mb"], stats["model_mb"] = model_memory(cfg, model)
    return text, stats


def _checkboxes(text):
    """Action items as "- [ ]" tasks: the model sometimes writes them as plain bullets."""
    lines, in_tasks = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            in_tasks = line[3:].strip() in ("\xc5tg\xe4rdspunkter", "Action items")
        elif in_tasks:
            line = re.sub(r"^[-*] (?!\[[ xX]\] )", "- [ ] ", line)
        lines.append(line)
    return "\n".join(lines)


def verify(cfg, meeting, model, draft, source):
    """Second pass: the model checks its draft against the transcript (or the chunk
    notes it was made from) and lists corrections as "wrong" -> "right". Catches words
    nobody said and broken tokens such as "ej ang evicted".

    The model does not write the minutes out again: copying 1-2k tokens is exactly
    where a 12B model drops in new typos (measured: it fixed one and added two). A
    correction is applied only where its wrong text occurs verbatim in the draft."""
    s = cfg["summarize"]
    lang = minutes_language(cfg, meeting)
    prompt = _fill(read_text(_prompt_path(cfg, f"verify_{lang}")), meeting, source).replace("{summary}", draft)
    if lang != ("sv" if meeting["language"] == "sv" else "en"):
        # Minutes in another language than the meeting: every word is translated, so
        # "words that were not used in the meeting" must not be read literally.
        prompt += TRANSLATED_NOTE[lang]
    if len(prompt) > (s["num_ctx"] - 1000) * s["chars_per_token"]:
        print("    verify skipped: transcript and draft do not fit in num_ctx")
        return draft
    answer, _ = chat(cfg, model, prompt, temperature=0, max_tokens=1000)
    text, applied = draft, 0
    # One correction per line, each side up to its last quote: the minutes quote names
    # ("Oil price"), and a lazy match would cut the right side at the first inner quote.
    # The line must end at that quote. The model sometimes comments after a correction
    # ("X" -> "X" (Note: ... "testare" ...)), and the comment then ended up in the minutes.
    pairs = re.findall(r'(?m)^[^"\n]*"(.+)"\s*->\s*"(.*)"[ \t.,]*$', answer)
    for wrong, right in dict.fromkeys(pairs):
        # Never touch the headings, and never replace text with itself.
        if wrong == right or wrong.startswith("#") or wrong not in text:
            continue
        text = text.replace(wrong, right, 1)
        applied += 1
    text = re.sub(r"(?m)^\s*[-*]\s*\.?\s*$\n?", "", text)  # bullets emptied by a removal
    print(f"    verify: {applied} correction(s)")
    return text


TRANSLATED_NOTE = {
    "sv": ("\n\nObs: m\xf6tet h\xf6lls p\xe5 engelska och utkastet \xe4r skrivet p\xe5 svenska. Att orden "
           "\xe4r \xf6versatta \xe4r inget fel. R\xe4tta bara det som betyder n\xe5got annat \xe4n i "
           "transkriberingen."),
    "en": ("\n\nNote: the meeting was held in Swedish and the draft is written in English. Translated words "
           "are not errors. Only correct what means something other than in the transcript."),
}


# Number words, so that "tre veckor" in the transcript matches "3 veckor" in the minutes.
# (\xe5 = a-ring, to keep this file ASCII.)
NUMBER_WORDS = {
    "sv": ["noll", "en|ett", "tv\xe5", "tre", "fyra", "fem", "sex", "sju", "\xe5tta", "nio", "tio",
           "elva", "tolv", "tretton", "fjorton", "femton", "sexton", "sjutton", "arton", "nitton",
           "tjugo"],
    "en": ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
           "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
           "eighteen", "nineteen", "twenty"],
}
ROUND_WORDS = {
    "sv": {30: "trettio", 40: "fyrtio", 50: "femtio", 60: "sextio", 70: "sjuttio",
           80: "\xe5ttio", 90: "nittio", 100: "hundra", 1000: "tusen"},
    "en": {30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy",
           80: "eighty", 90: "ninety", 100: "hundred", 1000: "thousand"},
}


def _number_said(number, transcript, lang):
    if re.search(rf"(?<![\d,.]){re.escape(number)}(?!\d|[,.]\d)", transcript):
        return True
    if not number.isdigit():
        return False
    n = int(number)
    words = NUMBER_WORDS[lang][n] if n < len(NUMBER_WORDS[lang]) else ROUND_WORDS[lang].get(n)
    return bool(words) and re.search(rf"\b({words})\b", transcript, re.IGNORECASE) is not None


# English minutes of a Swedish meeting capitalize these; the transcript has "tisdag".
CALENDAR_EN = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "january",
               "february", "march", "april", "may", "june", "july", "august", "september", "october",
               "november", "december"}


def unsupported(meeting, summary, translated=False):
    """Numbers and names in the minutes that do not occur in the transcript. A plain
    text check, so it also catches what the verify pass missed. Returns (numbers, names).
    translated: the minutes are in another language than the meeting."""
    lang = "sv" if meeting["language"] == "sv" else "en"
    # Without the timestamps, which contain almost every number below 60.
    transcript = "\n".join(re.sub(r"^\[[\d:]+\] ", "", line) for line in transcript_lines(meeting))
    lower = transcript.lower()
    known = (meeting["name"] + " " + " ".join(participants(meeting))).lower()
    numbers, names = [], []
    for line in summary.splitlines():
        if line.startswith("#"):
            continue
        body = re.sub(r"^\s*[-*]\s+(\[.\]\s+)?", "", line).replace("**", "")
        for number in re.findall(r"(?<![\w-])\d+(?:[,.]\d+)?(?![\w-])", body):
            if number not in numbers and not _number_said(number, transcript, lang):
                numbers.append(number)
        # Capitalized words that do not start a sentence: names, products, systems.
        for m in re.finditer(r"(?<=[^.:!?\s(\"]\s)([A-Z\xc5\xc4\xd6][\w-]+)", body):
            word = m.group(1)
            base = word.lower()[:-1] if word.lower().endswith("s") else word.lower()  # "Eriks"
            if translated and word.lower() in CALENDAR_EN:
                continue
            if base not in lower and base not in known and word not in names:
                names.append(word)
    return numbers, names


def sensitive(cfg, meeting, model, summary):
    """Details that may be sensitive to share (health, security, personal, internal
    numbers), each with a suggested rewording. A finding whose quote is not in the
    minutes is dropped, since the model sometimes makes them up."""
    lang = minutes_language(cfg, meeting)
    prompt = read_text(_prompt_path(cfg, f"sensitive_{lang}")).replace("{summary}", summary)
    text, _ = chat(cfg, model, prompt, temperature=0, max_tokens=800)
    plain = re.sub(r"\s+", " ", summary.replace("**", "")).lower()
    findings = []
    for line in text.splitlines():
        m = re.match(r'\s*[-*]\s+.*?"(.+?)"', line)
        if m and re.sub(r"\s+", " ", m.group(1)).lower().strip(" .") in plain:
            findings.append("- " + re.sub(r"^\s*[-*]\s+", "", line).strip())
    return findings


def review_notes(cfg, meeting, model, summary):
    """Lines for the "check before sharing" section of summary.md. Empty if nothing was found."""
    lang = minutes_language(cfg, meeting)
    sv = lang == "sv"
    numbers, names = unsupported(meeting, summary, translated=lang != ("sv" if meeting["language"] == "sv" else "en"))
    lines = []
    if numbers:
        lines.append(("- Siffror som inte finns i transkriberingen: " if sv else
                      "- Numbers that are not in the transcript: ") + ", ".join(numbers))
    if names:
        lines.append(("- Namn och begrepp som inte finns i transkriberingen: " if sv else
                      "- Names and terms that are not in the transcript: ") + ", ".join(names))
    if cfg["summarize"].get("sensitivity_check", True):
        print("    sensitivity check")
        lines += sensitive(cfg, meeting, model, summary)
    return lines
