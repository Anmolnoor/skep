---
name: segment-anything-model
description: zero-shot image segmentation with Meta's SAM in a run workspace
---

# Segment Anything (SAM)

Tools: dispatch_run, get_run, allow_fetch_domain, read_file

Zero-shot segmentation as a script-caste dispatch; torch + SAM live in
the run workspace venv, never in skep's tree.

1. Weights come from the granted domain the seed names:
   `allow_fetch_domain dl.fbaipublicfiles.com` — `sam_vit_b` (~375MB)
   for speed, `sam_vit_h` (~2.4GB) for quality; state the size before
   downloading.
2. Dispatch: venv + `pip install segment-anything opencv-python`,
   then either `SamAutomaticMaskGenerator` (segment everything) or
   `SamPredictor` with point/box prompts (segment THIS thing —
   prefer it when the user names a target).
3. Artifacts: mask PNGs plus an overlay image so the operator can eyeball
   the result; verify by mask count > 0 and the overlay file existing.
4. CPU inference runs a minute-plus per image — fine for a few
   images; batch jobs should say the expected wall time first.
5. For "cut out the object" asks, apply the mask to produce a
   transparent PNG in the same run.
