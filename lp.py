"""LokalProtokoll command line.

  python lp.py app
  python lp.py record [--name "Styrelsemote"] [--speakers 2] [--mic-only]
  python lp.py devices
  python lp.py process <audio file or recording folder> [--name "Styrelsemote"] [--lang sv] [--speakers 3]
  python lp.py rediarize <meeting folder> [--speakers 3] [--threshold 1.0]
  python lp.py rename <meeting folder> [1="Anna" 2="Erik"] [--summarize]
  python lp.py summarize <meeting folder> [--llm qwen3:14b]
  python lp.py compare <meeting folder> [--models gemma4:12b qwen3:14b]
  python lp.py search "budget"
"""

import argparse
import html
import json
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from lokalprotokoll import audio, diarize, merge, output, speakers, summarize, transcribe
from lokalprotokoll.config import load_config, resolve
from lokalprotokoll.output import new_meeting_dir, slugify


class Timer:
    """Prints and records how long each stage takes."""

    def __init__(self):
        self.times = {}

    @contextmanager
    def stage(self, name):
        print(f"\n[{name}]")
        start = time.perf_counter()
        yield
        self.times[name] = round(time.perf_counter() - start, 1)
        print(f"[{name}] {self.times[name]} s")

    def report(self, audio_seconds=None):
        print("\nTiming:")
        for name, secs in self.times.items():
            speed = f"  ({audio_seconds / secs:.1f}x realtime)" if audio_seconds and secs else ""
            print(f"  {name:<18} {secs:8.1f} s{speed}")
        print(f"  {'total':<18} {sum(self.times.values()):8.1f} s")


def run_diarization(cfg, meeting, out_dir, timer, backend, num_speakers, threshold=None):
    backend = backend or cfg["diarize"]["backend"]
    # In a recording the mic track is always you (speaker 0), so only the
    # other segments are diarized, using the system audio track.
    mine = [s for s in meeting["segments"] if s.get("track") == "mic"]
    others = [s for s in meeting["segments"] if s.get("track") != "mic"]
    wav = Path(out_dir) / meeting.get("track_files", {}).get("system", "audio_16k.wav")
    turns = []
    if backend != "none":
        with timer.stage("diarize"):
            turns = diarize.diarize(cfg, wav, backend, num_speakers, threshold)
        Path(out_dir, "diarization.json").write_text(json.dumps(turns, indent=2), encoding="utf-8")
    others = merge.assign_speakers(others, turns)
    meeting["segments"] = sorted(mine + others, key=lambda s: s["start"])
    if backend == "none":
        meeting["diarizer"] = "none"
        return
    print(f"    {len({s['speaker'] for s in others})} speakers")
    meeting["diarizer"] = backend
    names = meeting.get("speaker_names", {})
    if any(k != "0" for k in names):
        print("    Speaker numbers changed, so earlier names were removed. Run rename again.")
        meeting["speaker_names"] = {k: v for k, v in names.items() if k == "0"}
    recognize_voices(cfg, meeting, out_dir)
    meeting.pop("voice_samples_deleted", None)  # new speakers: new samples to name them by
    speakers.make_samples(meeting, out_dir)
    print(f"    Voice samples: {Path(out_dir, 'speakers.html')}")


def recognize_voices(cfg, meeting, out_dir):
    """Name speakers whose voice was saved earlier (lokalprotokoll/voices.py)."""
    from lokalprotokoll import voices

    meeting.pop("recognized", None)
    if not voices.list_voices(cfg):
        return
    matches = voices.recognize(cfg, out_dir, meeting)
    if matches:
        names = meeting.setdefault("speaker_names", {})
        for n, match in matches.items():
            names[str(n)] = match["name"]
            print(f"    Recognized {output.labels(meeting['language'])['speaker']} {n} as {match['name']} "
                  f"(similarity {match['score']:.2f})")
        meeting["recognized"] = {str(n): m for n, m in matches.items()}


def run_summary(cfg, meeting, out_dir, timer, model):
    model = model or cfg["summarize"]["model"]
    summarize.check_model(cfg, model)
    with timer.stage("summarize"):
        print(f"    model {model}, num_ctx {cfg['summarize']['num_ctx']}")
        text, stats = summarize.summarize(cfg, meeting, model)
        print(f"    {stats['answer_tokens']} tokens at {stats['tokens_per_s']} tok/s, GPU {stats['gpu_percent']}%, VRAM {stats['vram_mb']} MB")
        # Meetings recorded before automatic titles existed are called "Mote".
        if meeting.get("auto_name") or meeting["name"] in ("Mote", output.DEFAULT_NAME):
            # No name was given: make a title from the minutes.
            meeting["auto_name"] = True
            title = summarize.make_title(cfg, meeting, text, model)
            if title:
                meeting["name"] = title
                print(f"    title: {title}")
                output.write_transcript_md(meeting, out_dir)
                speakers.write_html(meeting, out_dir)
        review = summarize.review_notes(cfg, meeting, model, text)
        print(f"    {len(review)} point(s) to check before sharing")
    lang = "sv" if meeting["language"] == "sv" else "en"
    output.write_summary_md(meeting, text, model, cfg["prompts"][f"consent_note_{lang}"], out_dir,
                            review=review)
    summarize.unload(cfg, model)  # free the GPU memory and RAM right away
    meeting["summary_model"] = model
    meeting.pop("summary_stale", None)  # the minutes match the transcript again
    meeting["summary_stats"] = stats


def run_pipeline(cfg, args, out_dir, meeting, timer):
    """Detect language, transcribe, diarize and summarize. Expects audio_16k.wav
    in out_dir, plus mic_16k.wav and system_16k.wav for a two-track recording."""
    wav = out_dir / "audio_16k.wav"
    duration = audio.wav_duration(wav)
    print(f"    {output.fmt_time(duration)} of audio")

    language = args.lang or cfg["transcribe"]["language"]
    if language == "auto":
        with timer.stage("detect"):
            language = transcribe.detect_language(cfg, wav, duration)
            print(f"    language: {language}")

    # A normal file is one track (""). A recording has a "mic" and a "system" track.
    track_files = meeting.get("track_files")
    tracks = {}
    model = ""
    for track, filename in (track_files or {"": "audio_16k.wav"}).items():
        if track and audio.peak(out_dir / filename) < 0.01:
            print(f"\n[{track}] silent, skipped")
            tracks[track] = []
            continue
        with timer.stage(f"transcribe {track}".strip()):
            segs, model = transcribe.transcribe(cfg, out_dir / filename, out_dir, language,
                                                f"whisper_{track}" if track else "whisper",
                                                separate_track=bool(track))
        print(f"    {len(segs)} segments")
        for seg in segs:
            if track:
                seg["track"] = track
            if track == "mic":
                seg["speaker"] = 0
        tracks[track] = segs

    if track_files:
        rec = cfg["record"]
        if rec.get("echo_filter") and tracks["mic"] and tracks["system"]:
            tracks["mic"], dropped = merge.remove_echo(tracks["mic"], tracks["system"],
                                                       rec.get("echo_word_overlap", 0.6))
            if dropped:
                print(f"    Echo filter: removed {dropped} mic segments also heard in the system audio")
        if rec.get("my_name"):
            meeting.setdefault("speaker_names", {})["0"] = rec["my_name"]
    segments = sorted((s for segs in tracks.values() for s in segs), key=lambda s: s["start"])

    meeting.update({"duration_s": round(duration, 1), "language": language,
                    "whisper_model": model, "diarizer": None, "segments": segments})
    run_diarization(cfg, meeting, out_dir, timer, args.diarizer, args.speakers, args.threshold)
    output.write_transcript_md(meeting, out_dir)
    output.save_meeting(meeting, out_dir)

    if not args.no_summary:
        run_summary(cfg, meeting, out_dir, timer, args.llm)
    meeting["timings"] = timer.times
    output.save_meeting(meeting, out_dir)
    timer.report(duration)
    print(f"\nDone: {out_dir}")


def cmd_process(cfg, args):
    src = Path(args.audio)
    if src.is_dir():
        return process_recording(cfg, args, src)
    if not src.exists():
        raise SystemExit(f"File not found: {src}")
    when = datetime.fromtimestamp(src.stat().st_mtime)
    name = args.name or src.stem
    out_dir = new_meeting_dir(cfg, name, when)
    print(f"Output: {out_dir}")
    timer = Timer()
    with timer.stage("convert"):
        audio.convert_to_wav(resolve(cfg["paths"]["ffmpeg"]), src, out_dir / "audio_16k.wav")
    # Without --name the file name is only a placeholder; a title is made from the minutes.
    meeting = {"name": name, "auto_name": not args.name, "date": f"{when:%Y-%m-%d %H:%M}",
               "source_audio": str(src.resolve())}
    run_pipeline(cfg, args, out_dir, meeting, timer)


def process_recording(cfg, args, folder):
    """Process a folder made by "lp.py record" (mic.wav, plus system.wav unless --mic-only)."""
    info_path = folder / "recording.json"
    if not info_path.exists():
        raise SystemExit(f"{folder} is not a recording folder (no recording.json)")
    if not (folder / "mic.wav").exists():
        raise SystemExit(f"{folder} is already processed (the raw audio is gone). "
                         "Use rediarize or summarize instead.")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    when = datetime.fromisoformat(info["started"])
    ffmpeg = resolve(cfg["paths"]["ffmpeg"])
    timer = Timer()
    meeting = {"name": args.name or info["name"], "auto_name": bool(info.get("auto_name")) and not args.name,
               "date": f"{when:%Y-%m-%d %H:%M}", "source_audio": str(folder.resolve())}
    raw = [folder / "mic.wav"]
    with timer.stage("convert"):
        if (folder / "system.wav").exists():
            raw.append(folder / "system.wav")
            audio.convert_to_wav(ffmpeg, raw[0], folder / "mic_16k.wav")
            audio.convert_to_wav(ffmpeg, raw[1], folder / "system_16k.wav")
            audio.mix_to_wav(ffmpeg, [folder / "mic_16k.wav", folder / "system_16k.wav"],
                             folder / "audio_16k.wav")
            meeting["track_files"] = {"mic": "mic_16k.wav", "system": "system_16k.wav"}
        else:
            # Mic only (in-person meeting): everyone is on one track, so diarize it all.
            audio.convert_to_wav(ffmpeg, raw[0], folder / "audio_16k.wav")
    run_pipeline(cfg, args, folder, meeting, timer)
    if cfg["record"].get("delete_raw_audio"):
        for path in raw:
            path.unlink()


def cmd_record(cfg, args):
    from lokalprotokoll import recorder

    rec = cfg["record"]
    name = args.name or output.DEFAULT_NAME
    out_dir = new_meeting_dir(cfg, name, datetime.now())
    print(f"Output: {out_dir}")
    info = recorder.record(out_dir, name, rec.get("mic_device", ""), rec.get("speaker_device", ""),
                           mic_only=args.mic_only, auto_name=not args.name)
    if info["duration_s"] < 2:
        print("Recording too short, not processed.")
        return
    if args.no_process:
        print(f'Saved. Process it later with: python lp.py process "{out_dir}"')
        return
    process_recording(cfg, args, out_dir)


def cmd_evaluate(cfg, args):
    """Compare a processed meeting with a reference transcript (see evaluate.py)."""
    from lokalprotokoll import evaluate

    meeting = output.load_meeting(args.folder)
    result = evaluate.evaluate(meeting, evaluate.load_reference(args.reference))
    Path(args.folder, "evaluation.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                    encoding="utf-8")
    print(f"Reference words: {result['reference_words']}   found: {result['hypothesis_words']}")
    print(f"WER    {result['wer']:.1%}   words wrong, missing or extra")
    print(f"cpWER  {result['cpwer']:.1%}   same, and each word must have the right speaker")
    parts = result["der_parts"]
    print(f"DER    {result['der']:.1%}   speaking time: {parts['confusion']:.1%} wrong speaker, "
          f"{parts['missed']:.1%} missed, {parts['false_alarm']:.1%} false alarm")
    print(f"Speakers: {result['found_speakers']} found, {result['reference_speakers']} in the reference")
    for ours, theirs in result["speaker_mapping"].items():
        print(f"  {output.speaker_name(meeting, int(ours) if ours.isdigit() else None) or ours} = {theirs}")


def cmd_remember(cfg, args):
    """Save a speaker's voice so they are named automatically in later meetings."""
    from lokalprotokoll import voices

    meeting = output.load_meeting(args.folder)
    name = args.name or meeting.get("speaker_names", {}).get(str(args.speaker))
    if not name:
        raise SystemExit("Give a name: --name \"Anna Svensson\" (or name the speaker first with rename)")
    seconds, message = voices.save_voice(cfg, name, args.folder, meeting, args.speaker)
    print(message)
    if seconds < voices.MIN_SAVE_S:
        raise SystemExit(1)


def cmd_voices(cfg, args):
    from lokalprotokoll import voices

    if args.delete:
        found = voices.delete_voice(cfg, args.delete)
        print(f"Deleted the voice of {args.delete}." if found else f"No saved voice called {args.delete}.")
        return
    saved = voices.list_voices(cfg)
    if not saved:
        print("No saved voices.")
    for v in saved:
        print(f"  {v['name']:<28} {v['seconds']:5.0f} s of speech, from {len(v.get('meetings', []))} meeting(s), "
              f"updated {v.get('updated', '?')}")


def cmd_search(cfg, args):
    from lokalprotokoll import search

    results = search.search(cfg, args.query)
    if not results:
        print("Nothing found.")
        return
    for r in results:
        print(f"\n{r['name']}  ({r['date']})  {r['folder']}")
        for line in r["minutes"]:
            print(f"  minutes: {line}")
        for s in r["sentences"]:
            who = f"{s['speaker']}: " if s["speaker"] else ""
            print(f"  [{output.fmt_time(s['start'])}] {who}{s['text']}")
        if r["more"]:
            print(f"  ... and {r['more']} more sentences")


def cmd_app(cfg, args):
    from lokalprotokoll import app

    app.run(cfg, args.config)


def cmd_devices(cfg, args):
    from lokalprotokoll import recorder

    recorder.list_devices()


def cmd_rediarize(cfg, args):
    meeting = output.load_meeting(args.folder)
    if not speakers.has_audio(meeting, args.folder):
        raise SystemExit("The recording of this meeting was deleted, so the speakers cannot be found again.")
    timer = Timer()
    run_diarization(cfg, meeting, args.folder, timer, args.diarizer, args.speakers, args.threshold)
    output.write_transcript_md(meeting, args.folder)
    if args.summarize:
        run_summary(cfg, meeting, args.folder, timer, args.llm)
    elif Path(args.folder, "summary.md").exists():
        meeting["summary_stale"] = True  # new speakers, old minutes
    output.save_meeting(meeting, args.folder)
    timer.report(meeting["duration_s"])


def play(path):
    """Play a wav file in the background (Windows only). None stops playback."""
    try:
        import winsound
    except ImportError:
        return
    if path is None:
        winsound.PlaySound(None, 0)
    elif Path(path).exists():
        winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC)


def ask_names(meeting, folder):
    """Play each speaker's sample and ask for a name. Returns {"1": "Anna", ...}."""
    answers = {}
    print("For each speaker: type a name, press Enter to keep it, or p + Enter to play again.")
    try:
        for speaker, s in speakers.speaker_stats(meeting).items():
            current = output.speaker_name(meeting, speaker)
            quote = " ... ".join(seg["text"] for seg in speakers.pick_sample(s["segments"]))
            print(f"\n{current}  ({output.fmt_time(s['talk_s'])} talk time)")
            print(f"  \"{quote[:200]}\"")
            clip = speakers.clip_path(folder, speaker)
            play(clip)
            while True:
                answer = input("  Name: ").strip()
                if answer.lower() == "p":
                    play(clip)
                    continue
                if answer:
                    answers[str(speaker)] = answer
                break
    finally:
        play(None)
    return answers


def cmd_rename(cfg, args):
    """Give speakers real names: updates transcript.md, summary.md and speakers.html."""
    meeting = output.load_meeting(args.folder)
    existing = {str(n) for n in speakers.speaker_stats(meeting)}
    if not existing:
        raise SystemExit("This meeting has no speakers (diarization was off).")
    if not speakers.has_samples(args.folder) and not meeting.get("voice_samples_deleted"):
        speakers.make_samples(meeting, args.folder)

    if args.names:
        new_names = {}
        for item in args.names:
            number, sep, name = item.partition("=")
            if not sep or not number.strip().isdigit():
                raise SystemExit(f'Use the form 1="Anna Svensson", got: {item}')
            if number.strip() not in existing:
                raise SystemExit(f"No speaker {number.strip()} in this meeting "
                                 f"(speakers: {', '.join(sorted(existing, key=int))})")
            new_names[number.strip()] = name.strip()
    else:
        new_names = ask_names(meeting, args.folder)

    renames = []
    for number, name in new_names.items():
        old = output.speaker_name(meeting, int(number))
        if old != name:
            renames.append((old, name))
            print(f"  {old} -> {name}")
    if not renames:
        print("No changes.")
        return
    meeting.setdefault("speaker_names", {}).update(new_names)

    output.write_transcript_md(meeting, args.folder)
    speakers.write_html(meeting, args.folder)
    if args.summarize:
        run_summary(cfg, meeting, args.folder, Timer(), args.llm)
    else:
        summary = Path(args.folder, "summary.md")
        if summary.exists():
            text = summary.read_text(encoding="utf-8")
            summary.write_text(speakers.replace_names(text, renames), encoding="utf-8")
    output.save_meeting(meeting, args.folder)
    print("Updated transcript.md, summary.md and speakers.html")


def cmd_summarize(cfg, args):
    meeting = output.load_meeting(args.folder)
    timer = Timer()
    run_summary(cfg, meeting, args.folder, timer, args.llm)
    output.save_meeting(meeting, args.folder)
    timer.report()


def cmd_compare(cfg, args):
    """Summarize the same meeting with several models and show the results side by side."""
    meeting = output.load_meeting(args.folder)
    models = args.models or cfg["summarize"]["compare_models"]
    installed = summarize.installed_models(cfg)
    lang = "sv" if meeting["language"] == "sv" else "en"
    results = []
    for model in models:
        if model not in installed and model + ":latest" not in installed:
            print(f"\nSkipping {model}: not installed (ollama pull {model})")
            continue
        print(f"\n[{model}]")
        text, stats = summarize.summarize(cfg, meeting, model)
        print(f"    {stats['seconds']} s, {stats['tokens_per_s']} tok/s, GPU {stats['gpu_percent']}%, VRAM {stats['vram_mb']} MB")
        filename = f"summary_{slugify(model)}.md"
        output.write_summary_md(meeting, text, model, cfg["prompts"][f"consent_note_{lang}"],
                                args.folder, filename)
        results.append((model, text, stats))
        summarize.unload(cfg, model)

    columns = "".join(
        f"<div class='col'><h2>{html.escape(m)}</h2>"
        f"<p class='stats'>{s['seconds']} s, {s['tokens_per_s']} tok/s, GPU {s['gpu_percent']}%, VRAM {s['vram_mb']} MB,"
        f" {s['chunks']} chunk(s)</p><pre>{html.escape(t)}</pre></div>"
        for m, t, s in results)
    page = ("<!doctype html><meta charset='utf-8'><title>Compare</title><style>"
            "body{font-family:sans-serif;margin:16px}.row{display:flex;gap:16px}"
            ".col{flex:1;min-width:0;border:1px solid #ccc;padding:8px}"
            "pre{white-space:pre-wrap;font-family:inherit}.stats{color:#666}</style>"
            f"<h1>{html.escape(meeting['name'])}</h1><div class='row'>{columns}</div>")
    out = Path(args.folder, "compare.html")
    out.write_text(page, encoding="utf-8")
    print(f"\nOpen {out}")


def apply_overrides(cfg, overrides):
    """Apply "section.key=value" overrides. The value is read as TOML (numbers,
    true/false, "quoted strings", [lists]); anything else is taken as a string."""
    import tomllib

    for item in overrides:
        key, sep, value = item.partition("=")
        section, dot, name = key.strip().partition(".")
        if not sep or not dot or section not in cfg:
            raise SystemExit(f"--set expects SECTION.KEY=VALUE with a section from config.toml, got: {item}")
        try:
            cfg[section][name] = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            cfg[section][name] = value


def main():
    parser = argparse.ArgumentParser(description="Local meeting transcription and summary")
    parser.add_argument("--config", help="Path to config.toml")
    parser.add_argument("--set", action="append", default=[], metavar="SECTION.KEY=VALUE",
                        help="Override a config value for this run, e.g. --set summarize.num_ctx=16384 "
                             "or --set transcribe.model_sv=models/whisper/kb-whisper-medium-q5_0.bin (repeatable)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_pipeline_args(p):
        p.add_argument("--name", help="Meeting name")
        p.add_argument("--lang", choices=["sv", "en", "auto"])
        p.add_argument("--speakers", type=int,
                       help="Number of speakers, if known (in a recording: not counting you)")
        p.add_argument("--threshold", type=float, help="Speaker clustering threshold (higher = fewer speakers)")
        p.add_argument("--diarizer", choices=["sherpa", "none"])
        p.add_argument("--llm", help="Ollama model for the summary")
        p.add_argument("--no-summary", action="store_true")

    p = sub.add_parser("process", help="Transcribe, diarize and summarize an audio file or recording folder")
    p.add_argument("audio", help="Audio/video file, or a folder made by the record command")
    add_pipeline_args(p)
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("record", help="Record mic + system audio, then process when you press Ctrl+C")
    add_pipeline_args(p)
    p.add_argument("--mic-only", action="store_true",
                   help="In-person meeting: record only the microphone and diarize it")
    p.add_argument("--no-process", action="store_true", help="Only record, process later")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("evaluate", help="Measure accuracy against a reference transcript")
    p.add_argument("folder", help="A processed meeting folder")
    p.add_argument("reference", help="Reference JSON: [{start, end, speaker, text}, ...]")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("remember", help="Save a speaker's voice, to name them automatically in later meetings")
    p.add_argument("folder")
    p.add_argument("speaker", type=int, help="Speaker number (Talare 2 -> 2)")
    p.add_argument("--name", help="Name (default: the name given with rename)")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("voices", help="List the saved voices, or delete one")
    p.add_argument("--delete", metavar="NAME", help="Forget this person's voice")
    p.set_defaults(func=cmd_voices)

    p = sub.add_parser("search", help="Search the transcripts and minutes of all meetings")
    p.add_argument("query", help='Words that must all be in the same sentence; "quotes" for a phrase')
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("app", help="Open the LokalProtokoll window")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("devices", help="List microphones and output devices for recording")
    p.set_defaults(func=cmd_devices)

    p = sub.add_parser("rediarize", help="Redo speaker diarization for a meeting folder")
    p.add_argument("folder")
    p.add_argument("--speakers", type=int)
    p.add_argument("--threshold", type=float, help="Speaker clustering threshold (higher = fewer speakers)")
    p.add_argument("--diarizer", choices=["sherpa", "none"])
    p.add_argument("--summarize", action="store_true", help="Also redo the summary")
    p.add_argument("--llm")
    p.set_defaults(func=cmd_rediarize)

    p = sub.add_parser("rename", help="Listen to each speaker and give them a name")
    p.add_argument("folder")
    p.add_argument("names", nargs="*", help='Optional, skips the questions: 1="Anna" 2="Erik"')
    p.add_argument("--summarize", action="store_true",
                   help="Redo the summary with the names (otherwise names are just replaced)")
    p.add_argument("--llm")
    p.set_defaults(func=cmd_rename)

    p = sub.add_parser("summarize", help="Redo the summary for a meeting folder")
    p.add_argument("folder")
    p.add_argument("--llm")
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("compare", help="Compare summaries from several Ollama models")
    p.add_argument("folder")
    p.add_argument("--models", nargs="+")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args()
    cfg = load_config(args.config)
    apply_overrides(cfg, args.set)
    args.func(cfg, args)


if __name__ == "__main__":
    main()
