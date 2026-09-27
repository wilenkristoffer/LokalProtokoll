# LokalProtokoll

Local meeting transcriber and summarizer. It takes an audio file and produces a
transcript with speakers and timestamps, plus minutes (summary, decisions, action
items, open questions). Everything runs on your own PC: no cloud, no time limits.

```
audio file -> ffmpeg (16 kHz mono wav)
           -> whisper.cpp + Vulkan (KB-Whisper for Swedish, Whisper turbo for English)
           -> sherpa-onnx speaker diarization (TitaNet voice model)
           -> merge speakers with text by timestamp
           -> Ollama (local LLM) -> summary.md
```

## Setup on a clean Windows machine

**The quick way:** run the installer from the project folder. It installs what is missing (Python, ffmpeg, Ollama,
the build tools and Vulkan SDK for whisper.cpp, the models) and skips what is already there, so it is safe to run
again:

```powershell
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Without a usable GPU, or to skip building whisper.cpp, use `setup.ps1 -CpuOnly` (a ready-made, slower CPU version).
The steps below are what the installer does, if you want to do them by hand.

Run all commands in PowerShell from the project folder.

### 1. Python 3.11 or newer

Install from https://www.python.org/downloads/ (tick "Add python.exe to PATH"). Then:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If `Activate.ps1` is blocked, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### 2. ffmpeg

```powershell
winget install Gyan.FFmpeg
```

Open a new PowerShell window afterwards and check that `ffmpeg -version` works.

### 3. whisper.cpp with Vulkan (AMD GPU)

There is no prebuilt Windows Vulkan binary, so you build it yourself (it takes about 5-10 minutes).
You need:

- Git: `winget install Git.Git`
- Visual Studio Build Tools with "Desktop development with C++" (this includes CMake):
  `winget install Microsoft.VisualStudio.2022.BuildTools`, then pick that workload in the installer
- Vulkan SDK: `winget install KhronosGroup.VulkanSDK`

Open a **new** PowerShell window (so it sees `VULKAN_SDK`) and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_whispercpp.ps1
```

This produces `tools\whisper.cpp\build\bin\Release\whisper-cli.exe`, which is the path set in `config.toml`.
When you transcribe, the output should include a line like
`ggml_vulkan: 0 = AMD Radeon RX 7800 XT`. If that line is missing, Whisper is running on the CPU.

### 4. Models

```powershell
python setup_models.py
```

This downloads about 1.7 GB into `models\`:

| File | What it is |
|---|---|
| `kb-whisper-large-q5_0.bin` | KB-Whisper large (KBLab), for Swedish speech |
| `ggml-large-v3-turbo-q5_0.bin` | OpenAI Whisper turbo, for English and language detection |
| `ggml-silero-v6.2.0.bin` | Voice activity detection (skips silence) |
| `sherpa-onnx-pyannote-segmentation-3-0/` and `3dspeaker_..._16k.onnx` | Speaker diarization |

To try a smaller or more verbatim Swedish model:
`python setup_models.py --kb-size medium` or `--kb-variant strict`.
The script then prints the line to change in `config.toml`.

### 5. Ollama

Install it from https://ollama.com/download. The RX 7800 XT is supported on Windows (ROCm, with Vulkan as a fallback).
Keep your Adrenalin driver up to date. Then pull a model:

```powershell
ollama pull gemma4:12b
```

## The app

A small window for everyday use: record, follow the processing, and open past meetings.

```powershell
python lp.py app
```

You can also double-click `LokalProtokoll.pyw`. To get a desktop and Start menu shortcut, run this once:
`powershell -ExecutionPolicy Bypass -File scripts\create_shortcut.ps1`

Top right:

- **Hidden** (on by default): the window is excluded from screen sharing and screenshots. You see it; Teams, Zoom
  and screenshots do not. Click it to switch to "Visible". This needs Windows 10 version 2004 or newer.
- **Pin**: keeps the window on top of other windows.
- **Tuck away**: hides the window in the system tray. The window's close button does the same. Click the tray icon
  to bring it back; right-click it to start/stop a recording or to quit. The icon is a neutral "notes" icon and
  does not change while recording, so the taskbar and tray do not show that a meeting is being recorded.

The rest of the window:

- **Meeting name (optional)**: leave it empty and the meeting gets a short title made from its minutes, e.g.
  "Q4 budget and launch date" (it is called "Mote" until then). The same happens for imported files and the
  command line when no `--name` is given. The prompt is `prompts\title_sv.txt` / `title_en.txt`.
- **Record**: type a meeting name and choose how many *other* people are in the meeting ("Auto" if unsure). Press
  Record (or Ctrl+R), and press Stop when you are done. The meeting is then processed automatically, with the
  progress shown in the window. "In the room" records only the microphone, for in-person meetings.
- **Meetings**: click a meeting to open its minutes in a panel on the right side of the window. The panel has three
  tabs:
  - **Minutes**: the summary.
  - **Transcript**: the full transcript, with each speaker in their own color.
  - **Speakers**: play each speaker's voice sample and type a name. "Rewrite minutes with the names" makes a new
    summary with the names.

  Copy puts the text on the clipboard. The panel closes with the X button or Esc.
- **The `...` menu** on a meeting: view minutes or transcript, name speakers, rewrite the minutes, rename the
  meeting, redo the speakers with a new count, find and replace a word in the whole meeting, open the folder, or
  delete the meeting.
- **Correcting the transcript**: click a sentence in the Transcript tab to give it (or the whole paragraph) to
  another speaker or a new one, or to fix the text. Afterwards the Minutes tab shows "Speakers or text changed
  after these minutes were written" with a button to rewrite them. The minutes can also be edited by hand
  ("Edit" in the Minutes tab).
- **Import file**: processes an existing recording, using the name and speaker count from the fields above.
- **Search all meetings** (the box above the list): finds every sentence in the transcripts and every line in the
  minutes that contains all the search words, or a "quoted phrase". Click a result to open the transcript at that
  sentence. From the command line: `python lp.py search "budget"`.
- **Remember voice** (Speakers tab): tick it for a person you have named, and they are named automatically in
  later meetings. It needs at least 20 seconds of that person's speech; saving the voice again from another
  meeting makes it more reliable. A speaker is only named when the match is clear (see docs/MODELS.md); otherwise
  they keep "Talare N". "Saved voices" lists the voices and deletes them. From the command line:
  `python lp.py remember <meeting folder> 2 --name "Anna"`, `python lp.py voices`, `python lp.py voices --delete "Anna"`.

  **A saved voice is biometric personal data (GDPR).** Tell the person before you save their voice, and delete it
  when it is no longer needed. Voices are stored only in the `voices` folder of the project, never in git.

The app runs the same `lp.py` commands as below, so everything works the same from the command line.

## Usage

Activate the venv first (`.\.venv\Scripts\Activate.ps1`). Then:

```powershell
python lp.py process "C:\path\to\recording.m4a" --name "Styrelsemote"
```

Useful flags:

- `--speakers 4`: the number of speakers, if you know it. This gives much better speaker separation. Count
  everyone who says anything, including a chair who only hands out the floor: in the benchmark, counting the
  chair of a debate cut the speaker errors by more than half.
- `--lang sv` or `--lang en`: skips the automatic language detection.
- `--llm qwen3:14b`: uses a different summary model.
- `--diarizer none`: turns off speaker diarization.
- `--no-summary`: skips the summary step.

The output goes to `meetings\<date>_<time>_<name>\`:

| File | Content |
|---|---|
| `transcript.md` | Transcript with speakers and timestamps |
| `summary.md` | Summary, decisions, action items, open questions |
| `meeting.json` | All segments with speaker, plus metadata and timings (used for re-processing) |
| `speakers.html`, `speakers\` | A voice sample per speaker, with play buttons (see "Naming the speakers") |
| `whisper.json`, `diarization.json` | Raw output from each stage |
| `whisper.log` | Full whisper.cpp output (look here for GPU info and errors) |
| `audio_16k.wav` | The converted audio (about 115 MB per hour; you can delete it when you are done) |

The time taken by each stage is printed at the end, along with its speed compared to real time.

### Recording a meeting

```powershell
python lp.py record --name "Styrelsemote" --speakers 3
```

This records two separate tracks:

- **mic**: your microphone. In an online meeting this is you, so it is not diarized. You are labeled "Jag" (or "Me"
  in English); set `my_name` in `config.toml` to use your name.
- **system**: everything your PC plays (WASAPI loopback), i.e. the other participants. Only this track is split into
  "Talare 1, 2, ...". `--speakers` is the number of *other* people (not counting you).

A level meter shows that both tracks are receiving sound. Press **Ctrl+C** to stop. The recording is then processed
automatically.

Other options:

- `--mic-only`: for an in-person meeting where everyone is in the room. This records only the microphone and diarizes
  everyone, like a normal file.
- `--no-process`: only records. Process it later with `python lp.py process meetings\<folder>`.
- All `process` flags work too (`--lang`, `--llm`, `--no-summary`, ...).

Devices: the Windows default microphone and speakers are used. To see the devices or pick others:

```powershell
python lp.py devices
```

Then set `mic_device` / `speaker_device` in `config.toml` under `[record]` to part of a device name.

**Use a headset if you can.** With speakers, your microphone also picks up the other participants. The echo filter
(`echo_filter` in `config.toml`) removes most of this by dropping mic segments whose words are also in the system audio
at the same time. Single words can still slip through, and timestamps are less exact.

The full-quality `mic.wav` / `system.wav` files (about 350 MB per hour each) are deleted after processing. The 16 kHz
copies are kept: `mic_16k.wav`, `system_16k.wav`, and `audio_16k.wav` (both tracks mixed, for listening). To keep the
originals, set `delete_raw_audio = false`.

### Naming the speakers

After diarization, every speaker gets a short voice sample (6-12 s from their longest segments) in `speakers\`, and
`speakers.html` shows a play button per speaker together with what they said in the sample. To replace "Talare 1" with
real names:

```powershell
python lp.py rename meetings\2026-09-26_1400_Styrelsemote
```

This plays each speaker's sample and asks for a name. Press Enter to keep the current name, or type `p` to hear the
sample again. You can also skip the questions:

```powershell
python lp.py rename meetings\2026-09-26_1400_Styrelsemote 1="Anna Svensson" 2="Erik"
```

- `transcript.md` and `speakers.html` are rewritten with the names.
- In `summary.md`, the names are replaced as text, which is instant.
- Add `--summarize` to write a new summary with the names instead. This usually gives better action items, because the
  model knows who is who.
- The names are saved in `meeting.json`.
- If you run `rediarize`, the speaker numbers can change, so the names are removed and you rename again.

### Re-processing a meeting

```powershell
# Wrong number of speakers? Redo diarization (and the summary):
python lp.py rediarize meetings\2026-09-26_1400_Styrelsemote --speakers 3 --summarize

# Redo only the summary, for example after editing a prompt:
python lp.py summarize meetings\2026-09-26_1400_Styrelsemote --llm qwen3:14b
```

### Comparing summary models

```powershell
ollama pull gemma3:12b
ollama pull qwen3:14b
python lp.py compare meetings\2026-09-26_1400_Styrelsemote
```

This writes one `summary_<model>.md` per model, plus a `compare.html` that shows them side by side with speed and
GPU share for each. The model list is `compare_models` in `config.toml`; override it with
`--models a b c`.

## Configuration

All settings live in `config.toml`: model files, language, threads, diarization, Ollama model, `num_ctx`, and prompt files.
The prompts are plain UTF-8 text files in `prompts\`. You can edit them freely. Placeholders:
`{transcript}`, `{meeting_name}`, `{date}`.

### Context length (important)

Ollama's default context on a 16 GB card is only 4096 tokens. Anything longer is silently cut off.
`num_ctx = 32768` in `config.toml` handles about 2 hours of speech in one request.

- If a transcript is longer than that, it is split into chunks. Each chunk is summarized to notes first (`prompts\chunk_*.txt`),
  and then the notes are summarized.
- The tool warns you if the model used the whole context window.
- The `GPU %` value in the output shows how much of the model fits in VRAM. If it is below 100, the model runs
  partly on the CPU and is slower. In that case, lower `num_ctx` (for example to 16384) or use a smaller model.

## Speaker detection

Speaker detection uses **sherpa-onnx** with the TitaNet small voice model. It runs on the CPU, needs no account and
no PyTorch. pyannote, the other well-known option, was tested and not used: it was not clearly better (slightly
worse on Swedish) and about 8 times slower on the CPU. See "Tested and not used" in [docs/MODELS.md](docs/MODELS.md).

Speakers are labeled "Talare 1", "Talare 2" (or "Speaker 1", "Speaker 2" in English meetings), numbered in order of first
appearance.

## Models, accuracy and benchmark

[docs/MODELS.md](docs/MODELS.md) describes every model, how accurate it is, how much GPU memory, RAM and CPU
it needs, and which models suit which computer. The numbers come from the benchmark in `tests/`, which
processes test recordings that have correct transcripts and measures the errors:

```powershell
pip install -r requirements-dev.txt
python tests/fetch_samples.py                            # test recordings (about 120 MB)
python tests/benchmark.py                                # current settings
python setup_models.py --compare                         # alternative models (about 1.9 GB)
python tests/benchmark.py --variants tests/variants.toml # compare setups
```

Results: `tests/results/report.md` (with the hardware it ran on) and `tests/results/history.csv` (every run).

To see what the app uses in everyday conditions (recording with your other programs open), run
`python tests/measure_live.py` while LibreHardwareMonitor runs with its web server on (Options > Remote Web
Server > Run). It takes about 3 minutes and writes `tests/results/live_usage.md`.
To check a single meeting against your own reference transcript: `python lp.py evaluate <meeting folder>
<reference.json>`. Any config value can be changed for one run with `--set`, e.g.
`python lp.py --set summarize.num_ctx=16384 summarize <meeting folder>`.

## Consent

Tell everyone in the meeting that you are recording, and why. Under GDPR, you are responsible for this. `summary.md` ends
with a consent line you can fill in; the text is set in `config.toml` under `[prompts]`.
