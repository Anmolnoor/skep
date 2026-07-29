---
name: audiocraft-audio-generation
description: text-to-music and sound effects with Meta's AudioCraft (MusicGen)
---

# AudioCraft — audio generation

Tools: dispatch_run, get_run, allow_fetch_domain, read_file

MusicGen text-to-music as a script-caste dispatch. Heavy deps
(torch, audiocraft) live in the RUN workspace venv — skep's own
environment stays lean, always.

1. Dispatch: venv + `pip install audiocraft`, then a short script —
   `MusicGen.get_pretrained("facebook/musicgen-small")`,
   `.set_generation_params(duration=<s>)`, `.generate([prompt])`,
   write wav via `audio_write`. Weights download from the granted
   huggingface.co lane on first run (~2GB for small; say so first).
2. Start with `musicgen-small` and short durations (8–15s) — prove
   the prompt works before burning minutes on `-large`. CPU works;
   it's just slow — state the expected wait.
3. The wav lands in the workspace and reaches the operator as a run
   artifact; verify by checking the file exists with nonzero duration
   (soundfile/ffprobe in the run), not by "the script finished".
4. Prompt like a brief: genre, tempo, mood, instrumentation ("lo-fi
   hip hop, 80 bpm, warm Rhodes, vinyl crackle").
