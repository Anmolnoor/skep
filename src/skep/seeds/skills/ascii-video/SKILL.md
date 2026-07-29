---
name: ascii-video
description: render video or animations as ASCII in the terminal or as text-frame files
---

# ASCII video

Tools: dispatch_run, get_run, read_file

1. Everything runs as a script-caste dispatch; opencv-python +
   pillow live in the run workspace venv.
2. Pipeline: sample frames with OpenCV (2–10 fps is plenty), resize
   small (80×40-ish, halve height for character aspect), luminance →
   ramp ` .:-=+*#%@` per frame.
3. Artifacts, pick per ask: a `.txt` flipbook (frames joined by a
   separator), a self-contained player script (clear + print + sleep
   loop), or an animated GIF re-rendering the text frames via pillow
   when the target is chat/web.
4. Verify: frame count > 0, constant line dimensions across frames,
   and the artifact file nonzero — then land it as a run output.
5. Long videos: state the frame budget first (duration × fps) and
   clip to a scene; a 2-minute video at 10fps is 1200 frames of text
   nobody wants.
