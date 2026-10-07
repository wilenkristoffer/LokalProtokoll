# Models: what they do, how well they work, what they need

LokalProtokoll uses four kinds of models. All of them run locally. This page describes each one, gives the
accuracy and resource use **measured with the benchmark in this project**, and helps you choose models for
your own computer.

| Step | Model (default) | Runs on | Chosen in `config.toml` |
|---|---|---|---|
| Speech to text, Swedish | KB-Whisper large (q5_0) | GPU (Vulkan), or CPU | `transcribe.model_sv` |
| Speech to text, English, language detection | Whisper large-v3-turbo (q5_0) | GPU (Vulkan), or CPU | `transcribe.model_en`, `model_detect` |
| Finding speech (voice activity detection) | Silero VAD v6.2 | CPU | `transcribe.vad_model` |
| Who spoke when (speaker diarization) | pyannote segmentation 3.0 + TitaNet small voice model, via sherpa-onnx | CPU | `diarize.*` |
| Minutes (summary) | gemma4:12b via Ollama | GPU (ROCm/Vulkan), or CPU | `summarize.model` |

## Speech to text

### KB-Whisper (Swedish)

Made by KBLab at the National Library of Sweden. It is OpenAI's Whisper, trained further on more than 50,000
hours of Swedish speech. It is the reason Swedish transcripts are good: on KBLab's own tests it makes roughly
half as many errors as OpenAI's largest model.

| Size | File (q5_0) | Word errors, FLEURS / Common Voice / NST (KBLab) | OpenAI Whisper, same tests |
|---|---|---|---|
| large | 1.08 GB | 5.4% / 4.1% / 5.2% | large-v3: 7.8% / 9.5% / 11.3% |
| medium | 0.54 GB | 6.6% / 5.4% / 5.8% | medium: 12.1% / 15.8% / 17.1% |
| small | 0.18 GB | 7.3% / 6.4% / 6.6% | small: 20.6% / 26.4% / 26.4% |

These numbers are from the model card (read speech). Real meetings are harder; see the measured results below.

- **Variants**: KBLab also trains "strict" (more word-for-word) and "subtitle" (more condensed) versions. For
  large, the whisper.cpp file of "strict" is identical to "standard" (checked), so there is nothing to choose
  there. Other sizes: `python setup_models.py --kb-size medium`.
- **q5_0** means the weights are compressed to 5 bits. It is about three times smaller than the full model and
  much faster, with almost the same accuracy.
- **Swedish only.** Forced to Swedish, it translates English speech into Swedish instead of writing it down in
  English. That is why English meetings use stock Whisper (below).
- Source: https://huggingface.co/KBLab/kb-whisper-large (license on the model card).

### Whisper large-v3-turbo (English, and language detection)

OpenAI's multilingual Whisper with a smaller, faster decoder (809M parameters). It is used for English
meetings and to detect the language at the start of a meeting. File (q5_0): 0.55 GB. MIT license.
Source: https://huggingface.co/ggerganov/whisper.cpp

### How transcription runs

Whisper gets a short starting text per language (`transcribe.prompt_sv`, `prompt_en`) written with normal
punctuation. Without it, casual conversation sometimes came out with no punctuation or capitals at all; the
whole conversation then became one long "sentence", and all of it went to one speaker. With the prompt the
text is punctuated again, and WER was slightly better in English and unchanged in Swedish. As a second
safeguard, a segment longer than 15 seconds is split per word where the speaker changes.

whisper.cpp runs these models, built with the Vulkan backend so it works on AMD, Intel and NVIDIA GPUs.
Without a GPU it falls back to the CPU; large models are then slow (about real time or slower), and medium or
small are more practical.

## Finding speech: Silero VAD

A tiny model (0.9 MB, MIT license) that finds the parts of the audio that contain speech, so Whisper does not
invent text in silence. `vad_min_silence_ms` (1000) and `vad_speech_pad_ms` (400) control how the speech is
cut into pieces. whisper.cpp's own defaults (100 / 30) cut too tightly: in a test, Whisper then skipped 12
seconds of speech after a sound effect.

## Who spoke when: speaker diarization

Runs on the CPU with sherpa-onnx, in two steps:

1. **Segmentation** (pyannote segmentation 3.0, 6 MB, MIT): finds where one voice stops and another starts.
2. **Voice model** ("embedding model"): turns each piece of speech into a voice fingerprint. Pieces with
   similar fingerprints are grouped into one speaker.

Voice models that can be compared (`python setup_models.py --compare`):

| Voice model | File | Trained on |
|---|---|---|
| TitaNet small (NVIDIA NeMo) - default | 40 MB | English speaker data |
| CAM++ (3D-Speaker) | 30 MB | VoxCeleb (English, many accents) |
| ResNet34 (WeSpeaker) | 27 MB | VoxCeleb |
| ERes2Net base (3D-Speaker) - the first default | 40 MB | 3D-Speaker (Chinese) |

Voice fingerprints depend little on the language, so models trained on other languages can still work well
for Swedish. The benchmark shows which one separates speakers best.

3. **Voice check** (`diarize.refine_margin`, 0.2): the segmentation step sometimes treats a quick reply by
   someone else as the same voice ("Yeah, I only have one eye." / "Oh, my God, I'm sorry." / "Me too." all
   as one person). Afterwards, every voice turn gets its own fingerprint and is compared with each speaker's
   average voice; a turn that sounds clearly more like another speaker is moved there. On the test recordings
   this made nothing worse: cpWER went from 24.2% to 23.9% and 38.2% to 38.1% on AMI, and from 49.6% to 4.8%
   on the synthetic sample. It adds about 3 seconds per 25 minutes of audio.

pyannote, the best-known alternative, was tested and is not used (see "Tested and not used" below).

### Recognizing saved voices

"Remember voice" stores a person's TitaNet fingerprint (the average over their longest segments, up to 90 s). In
later meetings every speaker with at least 10 s of speech is compared with the saved voices.

Measured on the test recordings (a voice saved from the first half of a recording, compared with the second
half; 11 people):

| | Similarity |
|---|---|
| Same person, 25 s of speech or more | 0.84-0.95 |
| Same person, only 16 s of speech | 0.47 |
| Different people | mostly below 0.45; highest 0.72 (with the 16-second person) |

So a speaker is named only if the best voice scores **at least 0.75** and beats the next best voice by **at least
0.10**, and a voice can only be saved from **at least 20 s** of speech. In a test across two separate meetings
(the two halves of AMI ES2004a, processed separately), 3 of 4 people were recognized, all with the right name; the
fourth, with only 30 s of speech, was left unnamed. Recordings on other days or with other microphones usually
score somewhat lower, so a missing name is more likely than a wrong one. The limits are `MATCH_SCORE`,
`MATCH_MARGIN` and `MIN_SAVE_S` in `lokalprotokoll/voices.py`.

**Tip:** telling it the number of speakers (`--speakers`, or "Others" in the app) helps much more than any
model choice. Guessing the number is the weakest part of all diarization systems.

## Minutes: the summary model (LLM)

Runs in Ollama. The model has to fit in GPU memory **together with its context** (`num_ctx`): the transcript
of a 1-hour meeting is about 12,000-15,000 tokens, and a 32,768-token context adds several GB on top of the
model itself.

| Model | Download | Measured on the RX 7800 XT | Notes |
|---|---|---|---|
| **gemma4:12b** (default) | 7.6 GB | 50 tokens/s; about 12.9 GB VRAM at num_ctx 32768 | The most complete Swedish minutes in the comparison below |
| gemma3:12b | 8.1 GB | 49 tokens/s; about 11.2 GB VRAM | Good Swedish (EuroEval), a little less complete |
| qwen3:14b | 9.3 GB | 30 tokens/s; fills a 16 GB card | Missed all action items; twice as slow |
| qwen2.5:14b | 9.0 GB | 23 tokens/s; fills a 16 GB card | Some language errors and a wrong year; the slowest |

A summary of a 25-minute meeting took about 25 seconds with gemma4:12b on this GPU. Summary quality cannot be measured
automatically like transcription can: use `python lp.py compare <meeting>` and read the results side by side.
`lp.py compare` also prints each model's speed and GPU memory.

## Measured results

Measured on 2026-09-27 with `python tests/benchmark.py --variants tests/variants.toml` on:
AMD Ryzen 5 5600X (6 cores / 12 threads), 32 GB RAM, AMD Radeon RX 7800 XT (16 GB, Vulkan), Windows 11.
The full tables are in `tests/results/report.md`. The speaker count was given (`--speakers`).

The default setup was measured last, with all current settings (the punctuation prompt and the voice
check). The other variants were measured together, earlier the same day, without those two: compare them
with each other, and with the default's earlier numbers given in brackets.

### Speech to text, Swedish (Riksdag debate, 25.5 min)

| Model | WER | cpWER | Transcription speed | VRAM | Whole run (with speakers) |
|---|---|---|---|---|---|
| **KB-Whisper large** (default) | **24.0%** (23.9%) | **23.5%** (23.5%) | 8.6x | 1.8 GB | 5.0x |
| KB-Whisper medium | 27.6% | 28.1% | 12.4x | 1.1 GB | 6.6x |
| KB-Whisper small | 27.6% | 27.4% | 19.2x | 0.24 GB | 8.2x |
| Whisper large-v3-turbo | 30.2% | 30.4% | 33.8x | 0.77 GB | 9.9x |

The reference is the official protocol. It is edited: spoken language is tidied up, and the chair's words are
missing. So all WER values are higher than the real error rate. Use them to **compare** models: KB-Whisper
large makes the fewest errors, and medium and small are clearly faster at a small cost. Stock Whisper is the
weakest on Swedish, as KBLab's own numbers also show. "Speed" means minutes of audio per minute: at 8.7x, the
transcription of a 1-hour meeting takes about 7 minutes.

### Speech to text, English (AMI meetings, 17.5 + 14 min, word-for-word reference)

Whisper large-v3-turbo: **WER 21.5%** (22.1% without the punctuation prompt), about 0.8 GB VRAM. AMI is hard: casual
speech, people talking over each other, and a reference that includes every repeated and broken-off word.

### Speaker detection (all samples)

| Voice model | cpWER sv | DER sv | cpWER en | DER en | Speed | CPU load |
|---|---|---|---|---|---|---|
| **TitaNet small** (default, with the voice check) | **23.5%** | **7.6%** | **29.3%** | **37.5%** | 12-15x | 49% |
| TitaNet small, without the voice check (same run as the rows below) | 23.5% | 7.7% | 31.1% | 40.0% | 14-18x | 49% |
| CAM++ | 64.7% | 41.2% | 77.6% | 76.3% | 17-19x | 49% |
| WeSpeaker ResNet34 | 73.6% | 35.4% | 49.2% | 54.0% | 10-13x | 50% |
| ERes2Net (the first default) | 103.9% | 54.0% | 47.0% | 49.1% | 8-9x | 49% |

Speaker detection runs on the CPU (6 threads, about half of this CPU) and the whole run needs about 1 GB of
RAM. The first default voice model put an entire Swedish debate on one speaker. TitaNet small is better on
almost every sample and faster, so it is now the default. (The one exception is the synthetic sample with two
very similar computer voices, which says little about real meetings.)

Two things matter more than the voice model:

- **Give the number of speakers, and count everyone who speaks.** In the Riksdag debate, counting the chair
  (who only hands out the floor) as a fourth speaker took cpWER from 59% to 23.5%.
- **Automatic counting ("Auto") is unreliable.** With the threshold used at first (0.5), it found up to 48
  speakers in a 4-person meeting. `tests/tune_threshold.py` tried 0.5 to 1.2 on the real recordings:

  | Threshold | Speakers found (real: 4, 4, 4) | cpWER (average) |
  |---|---|---|
  | 0.5 | 48, 45, 9 | 85.7% |
  | 0.9 | 14, 18, 4 | 31.9% |
  | **1.0 (default)** | 9, 13, 4 | 30.1% |
  | 1.1 | 5, 9, 3 | 28.0% |
  | 1.2 | 1, 4, 2 | 69.7% |

  (AMI ES2004a, AMI IS1009a, Riksdag.) 1.1 scored best, but 1.2 already merges people, so 1.0 keeps a margin.
  Too many speakers can be fixed afterwards: give two of them the same name when naming speakers. People who
  were merged into one speaker cannot be split without processing again. A one-person screen recording
  gave 1 speaker at 1.0 (14 with the first voice model). With the number of speakers given, the same
  recordings reach cpWER 24.7%, 38.8% and 23.5%.
- **Same voice check (after "Auto", added 2026-10-07).** Real online meetings with 2-4 other people still got
  7-22 speakers at 1.0. Almost all extra speakers had a few seconds of speech (coughs, laughs, crosstalk),
  and some people were split in two. Comparing the speakers' average voices (TitaNet, cosine): two different
  people were at most 0.44 alike (benchmarks and real meetings), one person split in two 0.73-0.82. So after
  clustering, speakers at least 0.6 alike are joined (`diarize.merge_similar`), and the turns of speakers with
  less than 8 s of speech in total go to the speaker they sound most like (`diarize.min_speaker_s`):

  | Auto, threshold 1.0 | Speakers found (real: 4, 4, 4) | cpWER (AMI ES2004a, IS1009a, Riksdag) |
  |---|---|---|
  | Before | 9, 12, 4 | 27%, 36%, 24% |
  | **With the same voice check** | **4, 4, 4** | **24%, 29%, 24%** |

  Ten real online meetings went from 4-22 speakers to 2-5; in the meeting where one person left after a minute, that
  person is still found. Merging alone (min 0 s) or absorbing alone (no merging) both left too many speakers.

### VAD settings

| | WER sv | WER en | cpWER en |
|---|---|---|---|
| New (1000 ms / 400 ms, default; same run, before the prompt and voice check) | 23.9% | 22.1% | 31.1% |
| whisper.cpp defaults (100 ms / 30 ms) | 24.1% | 23.0% | 31.9% |

The new settings are slightly better, and they fix the case where Whisper skipped 12 seconds of speech.

### Summary models (25-minute Swedish meeting, num_ctx 32768)

Measured on 2026-09-27: the same 25-minute Swedish debate (Riksdag HD108), the same prompt, one after another.
The minutes are saved in `tests/results/llm_compare_2026-09-27/` (open `compare.html` to read them side by side).

| Model | Time | Speed | VRAM reported by Ollama | VRAM measured (LibreHardwareMonitor) | RAM while loaded |
|---|---|---|---|---|---|
| **gemma4:12b** (default) | 28 s | 50 tokens/s | 8.0 GB | +12.9 GB (the card was full) | +9.6 GB |
| gemma3:12b | 26 s | 49 tokens/s | 7.7 GB | +11.2 GB | +7.8 GB |
| qwen3:14b | 53 s | 30 tokens/s | 13.7 GB | +12.9 GB (the card was full) | +9.9 GB |
| qwen2.5:14b | 62 s | 23 tokens/s | 14.4 GB | +12.9 GB (the card was full) | +10.1 GB |

How the minutes compared (read by hand):

- **gemma4:12b**: the most complete. It named all three debaters and their positions, turned the three dates in
  the government's planning process into action items, and listed three distinct open questions.
- **gemma3:12b**: correct and clear, but it mentioned the other members only as "several members" and had two
  action items and two open questions.
- **qwen3:14b**: a good summary, but no action items at all, and two of its open questions said the same thing.
- **qwen2.5:14b**: some language errors ("understrydde", "stationeringslägen"), a probably wrong year, and one claim
  that did not quite match the debate.

The memory includes the context. **Ollama's own number is too low**, because it leaves out working buffers: plan
with the measured number. Three models stopped at +12.9 GB because the 16 GB card was then full (the desktop
already used 2.6 GB). They still ran fully on the GPU, but if another program uses a lot of GPU memory at the same
time, part of the model can spill over to the CPU and the summary gets slower.
While the model is loaded, Windows also shows about 7 GB more RAM in use. LokalProtokoll unloads the model as
soon as the minutes are written (`summarize.unload_after`); without that, Ollama keeps it loaded for 5 minutes.

### Resources in short (this computer)

| Step | Runs on | Memory | Time for a 1-hour meeting |
|---|---|---|---|
| Transcription, KB-Whisper large | GPU | 1.8 GB VRAM, 0.6 GB RAM | about 7 min |
| Speaker detection, TitaNet small | CPU (about half) | about 1 GB RAM | about 4 min |
| Summary, gemma4:12b | GPU | up to the whole free VRAM (12.9 GB here) and about 10 GB RAM, freed afterwards | about 1 min |
| **Total** | | the summary uses the most VRAM; the steps run one after another | **about 12 min** |

The same KB-Whisper large model on the **CPU only** (whisper.cpp without Vulkan) ran at about 2x real time on
this CPU: about 30 minutes for a 1-hour meeting.

## Everyday use: recording with other programs open

Measured with `python tests/measure_live.py` and LibreHardwareMonitor, with VS Code and other programs open as
usual. The system values are for the whole computer; "app" is LokalProtokoll itself (window, recorder,
whisper, lp.py and Ollama).

| Phase | CPU, whole computer | CPU, app | RAM, whole computer | GPU load | VRAM used |
|---|---|---|---|---|---|
| Before starting the app | 5.5% | - | 9.0 GB | 2% | 1.39 GB |
| App open, idle | 5.2% | 0.2% | 9.0 GB | 2% | +0.05 GB |
| **Recording (mic + system audio)** | **5.0%** | **0.5%** | **9.0 GB (+0)** | **2.5%** | **+0** |
| Processing a 1-minute recording (27 s) | 16% on average, 55% at most | 6%, 51% at most | +7.3 GB at most | 23%, 96% at most | +11.4 GB at most |
| 15 s after processing | 3.7% | 0.1% | 8.9 GB | 1% | back to 1.40 GB |

- **Recording is light**: half a percent of the CPU, no GPU, and about 200 MB of RAM for the whole app. It
  does not slow down a meeting, screen sharing or VS Code.
- **The heavy part is processing after the meeting**, mostly the summary: for about 20 seconds per meeting it
  uses about 11 GB of VRAM and 7 GB of RAM, and then frees them. Transcription and speaker detection use far
  less (about 0.7-1.8 GB VRAM and 1 GB RAM).
- **Disk**: while recording, the full-quality tracks take about 0.35 GB per hour each (mic and system). After
  processing they are replaced by 16 kHz copies, about 0.35 GB per hour for all three files together.
- If you process a meeting while you work on something heavy on the GPU (games, video editing), the summary
  can be slower. Processing can also be started later from the app ("Process now").

## Which models for which computer

The steps run one after another, so the graphics card only needs to hold the largest single model: the summary
model with its context. LokalProtokoll chooses the models with a **device profile** (`[device] profile` in
`config.toml`, Profile in the app); "auto" picks it from the graphics card's memory (`lokalprotokoll/hardware.py`).

| Device profile | Graphics card | Speech to text | Summary |
|---|---|---|---|
| desktop | 12 GB VRAM or more | KB-Whisper large, Whisper turbo | gemma4:12b, num_ctx 32768 |
| laptop | 8-12 GB | KB-Whisper large, Whisper turbo | gemma4:e4b, num_ctx 16384 |
| small | 4-8 GB | KB-Whisper large, Whisper turbo | gemma4:e2b, num_ctx 16384 |
| cpu | none, or integrated graphics | KB-Whisper small, Whisper small | gemma4:e4b on the processor, num_ctx 16384 |

Measured on 2026-10-07 (RX 7800 XT, Ryzen 5 5600X with 6 threads, 32 GB RAM):

**Summary models**, the same 20-minute Swedish meeting (about 4,300 tokens) and prompt, minutes plus the check
pass. VRAM is the increase in Windows' "GPU Adapter Memory, Dedicated Usage" counter while it ran; Ollama's own
`/api/ps` number is far too low for the gemma4 "e" models (it said 0.3 GB).

| Model | VRAM | On the GPU | On the processor only (`num_gpu = 0`) | The minutes (read by hand) |
|---|---|---|---|---|
| gemma4:12b | 9.2 GB (32k context), 9.0 GB (16k) | 46 s, 50 tokens/s | not tried | the most accurate and concise |
| gemma4:e4b | 5.6 GB | 38 s, 105 tokens/s | 5.7 min, 11.5 tokens/s | the right facts and deadlines, but twice as long and some clumsy Swedish |
| gemma4:e2b | 3.9 GB | 35 s, 122 tokens/s | 3.9 min, 21 tokens/s | the main points, but more often the wrong speaker |
| gemma3:4b | | 36 s, 114 tokens/s | 5.2 min, 12 tokens/s | **made things up**: a deadline and a speaker that were not in the meeting. Not used. |

**Speech to text on the processor only** (whisper.cpp `-ng`), 5 minutes of continuous speech (Riksdag):

| Model | On the GPU | On the processor only |
|---|---|---|
| KB-Whisper small | 16.5x real time | 4.7x |
| KB-Whisper medium | 10.2x | 1.9x |
| KB-Whisper large | 7.0x | 1.0x |
| Whisper large-v3-turbo | 7.0x | 1.1x |

Meetings have pauses that are skipped, so they go faster than continuous speech.

**The whole "cpu" profile**, everything on the processor, a 20-minute meeting: 13 minutes (transcription 3.4 min,
speaker detection 1 min, minutes 8.7 min). So about 40 minutes per meeting hour on this 6-core desktop processor;
laptop processors are usually slower.

On any computer:

- Speaker detection always runs on the processor. A 6-core CPU handles it at about 14x real time.
- Give the number of speakers when you know it.
- Run `python tests/benchmark.py --variants tests/variants.toml` on the new computer: `report.md` then shows
  the real numbers for that hardware.

## Tested and not used: pyannote

pyannote (`pyannote/speaker-diarization-community-1`, pyannote.audio 4.0.7 with PyTorch 2.14, on the CPU) was tested on
2026-09-27 as a replacement for sherpa-onnx + TitaNet, because a few short replies in chaotic YouTube clips went to the
wrong person. It was then removed again. **Do not try it again unless something important changes** (for example a
GPU version for AMD on Windows, or a clearly better model).

Same transcripts, same speaker counts, only the speaker detection changed:

| Recording | sherpa-onnx + TitaNet (default): cpWER / DER | pyannote: cpWER / DER |
|---|---|---|
| Synthetic, 2 voices | 4.8% / 8.6% | 4.8% / 8.6% |
| AMI IS1009a, 4 people, English | 38.1% / 38.2% | **28.0% / 28.3%** |
| AMI ES2004a, 4 people, English | 23.9% / 38.3% | 23.3% / 38.4% |
| Riksdag debate, 4 voices, **Swedish** | **23.5% / 7.6%** | 24.4% / 8.5% |

Why it was not kept:

- **Not clearly better.** Clearly better on one of four recordings (English), about the same on two, and slightly
  **worse on the Swedish** one. On two YouTube clips it was mixed: better at spreading a chaotic clip with many
  interruptions over 6 voices, worse at a quick reply that the default gets right.
- **About 8 times slower**: 1.8x real time on a Ryzen 5 5600X, so about 33 minutes per meeting hour, against about
  4 minutes for sherpa-onnx. PyTorch has no GPU support for AMD cards on Windows, so it runs on the CPU.
- **Heavy**: about 90 extra Python packages and a few GB, including PyTorch.
- **Account needed**: the model is gated on Hugging Face, so every user needs an account, must accept the terms and
  create a token.
- **Telemetry**: pyannote.audio 4 sends usage metrics (audio length, number of speakers, a session id) to
  `otel.pyannote.ai` by default. It can be switched off with `PYANNOTE_METRICS_ENABLED=false`, but it is one more
  thing that could let data leave the computer.

## Measuring on your own computer

```powershell
pip install -r requirements-dev.txt
python setup_models.py --compare                        # the alternative models (about 1.9 GB)
python tests/fetch_samples.py                           # test recordings with correct transcripts (about 120 MB)
python tests/benchmark.py --variants tests/variants.toml
```

The results go to `tests/results/report.md` (with your hardware), and every run is added to
`tests/results/history.csv`. Add your own setups to `tests/variants.toml`.

Test recordings used:

| Sample | Language | What | Reference |
|---|---|---|---|
| ami_es2004a, ami_is1009a | English | Project meetings, 4 people, 17.5 and 14 min (AMI corpus, CC BY 4.0) | Word for word, including "um" and overlapping speech |
| riksdag_hd108 | Swedish | Parliament debate, 3 people taking turns plus the chair, 25.5 min (Riksdagen open data) | The official protocol: lightly edited, so WER looks higher than the true error rate. Timed per speech. |
| synthetic_en | English | Two Windows speech voices, 70 s | Exact |

What the numbers mean:

- **WER** (word error rate): words that are wrong, missing or extra, divided by the number of words in the
  reference. 10% means about 1 word in 10 is wrong. Different spellings of numbers ("20 000" vs "tjugotusen")
  also count as errors.
- **cpWER**: the same, but each word must also be credited to the right speaker.
- **DER** (diarization error rate): the share of speaking time that is given to the wrong speaker, missed, or
  marked as speech where nobody spoke.
- **Speed**: minutes of audio processed per minute (transcription and speaker detection, without the summary).
  10x means a 1-hour meeting takes 6 minutes.
- **VRAM**: extra GPU memory used during transcription. **RAM**: peak memory of the whole run.
