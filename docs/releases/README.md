# Releases

How a skep release actually happens, and what is still parked.

## Local gates (run before any tag)

```sh
./scripts/local-release-gates.sh
```

That single script runs the full ladder: lint + types + unit + smoke,
reliability, the Linux sandbox smoke, the hygiene scan, the docs link check,
`uv build` + `twine check`, the wheel install smoke, and the container image
smoke. The scorecard must stay `Overall: PASS`.

## Tag → CI → GitHub release

Tag `v<version>` (the version in `pyproject.toml` — see
[`../release-checklist.md`](../release-checklist.md) for the full external
checklist) and push it. `.github/workflows/release.yml` re-runs the release
gate, builds the wheel/sdist, twine-checks them, smoke-installs the wheel, and
cuts the GitHub release with artifacts attached. `ci.yml` publishes the GHCR
image on the same tag.

## PyPI publish — PARKED (operator action to un-park)

The `pypa/gh-action-pypi-publish` step in `release.yml` is deliberately
commented out until PyPI trusted publishing is configured. To un-park:

1. On pypi.org: create the `skep` project (or claim the name), then under
   *Publishing* add a **trusted publisher**: repository `Anmolnoor/skep`,
   workflow `release.yml`, environment blank.
2. Uncomment the publish step in `.github/workflows/release.yml`.
3. Tag the release. `uvx skep` and `uv tool install skep` work from then on.

No API token needs to exist anywhere; trusted publishing is OIDC.

## Go/no-go: what remains is one decision

Everything below the operator's go/no-go is scripted (v37-F1). In order:

1. **Rehearse on TestPyPI.** On test.pypi.org, add a trusted publisher
   (repository `Anmolnoor/skep`, workflow `release.yml`, environment blank),
   then run the `Release` workflow manually (Actions → Release → Run
   workflow). The `testpypi-rehearsal` job publishes the current build to
   test.pypi.org over the exact OIDC path the real publish will use. It is
   manual-only and can never reach real PyPI or cut a GitHub release.
2. **Set up real PyPI.** On pypi.org, create/claim the `skep` project and add
   the same trusted publisher.
3. **Un-park the publish step.** Uncomment `pypa/gh-action-pypi-publish` in
   `.github/workflows/release.yml`.
4. **Tag.** `git tag -a v1.0.1 -m "skep 1.0.1" && git push origin v1.0.1` —
   the gate re-runs, the wheel publishes, the GitHub release and GHCR image
   cut automatically.
5. **Flip the repository public.** GitHub → Settings → Danger Zone.
6. **Mirror the demo repo.** `scripts/mirror-demo-repo.sh <public-skep-demo-url> --push`
   (dry-run first: `scripts/mirror-demo-repo.sh --dry-run`).
7. **Deploy the docs.** Publish `docs/` to the public host named in
   [`../release-checklist.md`](../release-checklist.md).
8. **Re-record the demo GIF.** `scripts/record-demo-gif.sh`, then commit
   `docs/assets/skep-demo.gif`.

## Also parked, deliberately

- **`agent-task-contract` package split** (v18 step 5): the contract is
  vendored at `src/skep/worker_contract/`; splitting it into an external
  PyPI package touches four `SUPPORTED_CONTRACT_RANGE` sites and needs a new
  public repo — a separate session with the operator.
- **Demo GIF re-record**: `scripts/record-demo-gif.sh` is the reproducible
  pipeline (asciinema + agg); the committed GIF is a faithful placeholder.
- **Public demo repo mirror**: `examples/skep-demo` mirrors to a public
  `skep-demo` repo at release time (external checklist step 5).
