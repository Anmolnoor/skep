---
name: songwriting-and-ai-music
description: write lyrics and song structure; AI generation via the operator's Suno account cards per call
---

# Songwriting & AI music

Tools: read_file, dispatch_run, allow_fetch_domain, read_url

The songwriting half ports fully; the generation half is API-keyed
egress and cards per call — honestly split.

1. Songwriting: title/concept → structure (verse/chorus/bridge map)
   → lyrics. Concrete images over abstractions ("coffee ring on your
   note" beats "memories of you"); chorus earns repetition by meaning
   more each time; scan syllables against the intended rhythm by
   reading aloud.
2. Style brief for generation: genre, tempo/bpm, mood, vocal type,
   production references — 2 lines max; generators weight the front.
3. Suno (or any hosted generator) uses the operator's OWN account and
   API key (env, never chat). Each generation call is a paid outbound
   call: it rides `read_url`/the granted domain the operator confirms,
   one card per call showing the exact lyrics + style prompt being
   sent. Never batch-fire generations without asking.
4. Local alternative with no account: the audiocraft skill (MusicGen)
   — offer it when the ask is instrumental/background music.
