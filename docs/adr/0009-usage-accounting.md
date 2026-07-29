# ADR 0009 — Usage accounting (G8, v2)

Date: 2026-06-11 · Status: accepted

## Context

The result envelope reserved a `Usage` block at v0.1 (G8) but never populated it
— v1 left it `None`. The north-star workloads (U1's nightly fleet) need cost to
be answerable: "what did this task / day / repo cost in provider calls and
tokens?" That answer has to come from the worker, which is the only party that
sees provider responses.

## Decision

The worker wraps its provider in a metering layer that counts every `complete`
call and sums the per-response token usage already parsed by the provider
adapters. At finalize it writes the totals into the reserved `result.usage`
(`provider_calls`, `input_tokens`, `output_tokens`). This is the reserved field
being filled — **zero contract change.**

The supervisor records `result.usage` into a `task_usage` row at ingest and
surfaces it: a per-run cell in `status` (`<calls>c/<tokens>`), a full line in
`review`, and an aggregate footer (`usage_totals`) that sums across runs — the
"answerable cost" G8 asked for.

`cost_usd` stays `None`: turning tokens into dollars needs a per-model price map,
which is supervisor-side configuration and belongs with v2+'s cost-accounting
work (the field is wired end-to-end and ready for it). The mock provider reports
no tokens, so gate runs record `provider_calls` with `None` tokens — honest, and
enough to prove the pipeline.

## Consequences

- Cost is answerable per task and in aggregate today, from real evidence the
  worker metered — not an estimate.
- Metering wraps *all* provider calls (planning and preflight review), so the
  call count is complete, not a planning-only approximation.
- `cost_usd` and per-day/per-repo rollups are a thin layer on top (a price map +
  a `GROUP BY`); the data they need is now captured. Recorded as the next step,
  not silently implied.
