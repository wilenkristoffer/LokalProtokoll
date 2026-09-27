# Roadmap

## Status

- [x] Step 1: file-based pipeline (transcribe, diarize, summarize)
- [x] Voice samples per speaker + `rename`
- [x] Step 2: recording of mic + system audio as separate tracks (`record`)
- [ ] Step 2: test on a real online meeting with 2-4 other participants (see below)
- [x] Step 3: desktop app (`lp.py app`), hidden from screen sharing by default
- [ ] Step 4: live transcription (maybe never)
- [x] Benchmark with test recordings (`tests/`), results and model guide in `docs/MODELS.md`
- [x] Corrections in the app (speakers, text, name, minutes), installer (`setup.ps1`), git
- [x] Search across all meetings, and saved voices that are recognized in later meetings

## Testing still needed (Step 2)

- A real online meeting with 2-4 other people, 15+ minutes, recorded with `record --speakers N`.
  - Does diarization separate the real voices on the system track?
  - Speed and num_ctx on a long meeting.
  - Swedish summary quality on a real meeting: does gemma4:12b (chosen on a Riksdag debate) also do well there?
- Automatic speaker count is unreliable on short recordings; check how it behaves on long ones.

## Later

- **Voice recognition on real meetings**: check the recognition limits (docs/MODELS.md) with voices saved
  on one day and recognized on another, with different microphones.

- **Benchmark on other hardware**: the numbers in docs/MODELS.md are from one computer (RX 7800 XT). Not
  measured yet: GPUs with 4-8 GB, CPU-only speed for KB-Whisper small/medium.
- **Automatic speaker count**: still off by a few speakers on long meetings (threshold 1.0). Could use a
  different clustering method; measure with `tests/tune_threshold.py`.

- **Mixed-language meetings**: the language is detected once per meeting. In a Swedish meeting, English speech
  is transcribed by KB-Whisper as a Swedish *translation* (seen with an English YouTube clip). Possible fix:
  detect the language per stretch of speech and send English parts to the English model.

- **Vocabulary prompt**: a list of names and terms Whisper often gets wrong (company names, colleagues). They
  can already be added to the starting prompt (`transcribe.prompt_sv` / `prompt_en` in config.toml); a
  separate list, or a field in the app, would make it easier.
- **Loudness normalization**: even out the volume of the tracks for listening, so a quiet mic does not disappear
  in the mix (`audio_16k.wav`).

## Tested and decided against

- **pyannote for speaker detection** (2026-09-27): not clearly better than sherpa-onnx + TitaNet (better on one of
  four test recordings, slightly worse on the Swedish one), about 8 times slower on the CPU, a few GB of PyTorch,
  a Hugging Face account for every user, and telemetry on by default. Removed again. Details and numbers in
  docs/MODELS.md, "Tested and not used".
- **KB-Whisper large "strict"**: its whisper.cpp file is identical to the standard one.
- **Other voice models** (ERes2Net, CAM++, WeSpeaker ResNet34): all clearly worse than TitaNet small in the benchmark.

