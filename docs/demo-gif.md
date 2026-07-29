# Demo GIF

The launch page needs a short terminal recording that shows the first-run
approval loop and the second run using the remembered template grant.

## Record

From the repo root:

```sh
scripts/record-demo-gif.sh
```

The script creates a disposable demo repo under `/tmp/skep-demo-recording`, uses
`scripts/demo_worker.py` as the worker, and exercises the real CLI:

1. `skep run ... "add a health endpoint" --execution-mode workspace`
2. Inline shell approval appears
3. Press `b` for approve + remember
4. `skep review <task_id>`
5. `skep review <task_id> --approve`
6. Second run matches the learned template and shows the pre-granted shell command

The checked-in launch GIF lives at:

```text
docs/assets/skep-demo.gif
```

It is a short generated terminal animation that mirrors the intended launch
story. To re-record the demo against the real CLI, install `asciinema` and run
the script above. With `asciinema`, the script writes:

```text
docs/assets/skep-demo.cast
```

Without `asciinema`, the script prints setup guidance. From an interactive
terminal it can still run the demo session without recording.

If [`agg`](https://github.com/asciinema/agg) is also installed, it replaces:

```text
docs/assets/skep-demo.gif
```

Keep the final GIF under 30 seconds before publishing it on the landing page.
