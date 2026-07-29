---
name: teams-meeting-pipeline
description: turn a meeting transcript file into minutes, decisions, and action items
---

# Meeting transcript pipeline

Tools: read_file, search_files, dispatch_run, get_run, add_task

Adapted from the Hermes Teams pipeline: the Teams-specific capture
(joining calls, pulling recordings) is honestly OUT OF SCOPE — this
skill starts from a transcript file the operator already has (vtt,
txt, or an export).

1. Locate the transcript (`search_files` / ask for the path) and skim
   its shape: speaker labels? timestamps? Strip vtt cue noise before
   summarizing.
2. Dispatch a document-caste run: minutes with sections Decisions,
   Action items (owner + due when stated), Open questions, and a
   ≤10-line summary. `Must include:` names two facts you spotted in
   the transcript so the summary is verifiably grounded.
3. Offer to turn action items into tasks (`add_task`) — one per item,
   owner in the title; only on confirm.
4. Long transcript? Chunk by agenda topic and summarize per chunk,
   then merge — never silently truncate the tail of the meeting.
