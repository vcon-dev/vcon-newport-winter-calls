# Newport Winter Calls

A synthetic corpus of 2,000 resident phone calls to the City of Newport,
Rhode Island, from 2025-12-01 to 2026-03-15, in
[vCon](https://datatracker.ietf.org/wg/vcon/about/) format. Each file in
`vcons/` is one call between a resident and a city clerk, as a per-turn text
transcript. The corpus has planted findings that a careful reader, human or
model, can be tested against.

> **Synthetic data, MIT licensed.** The corpus and every person in it are
> synthetic and illustrative. Names are fictional. Newport street names are
> real but house numbers are invented. Card numbers are published test
> numbers. Phone numbers use the fictional 555-01xx range. No City of Newport
> staff or residents are depicted, and the city did not produce or endorse
> this corpus. See [`LICENSE`](LICENSE).

It was built to support the "Thinking in vCon" talks (Small Ball and Long
Ball).

## Corpus at a glance

| | |
|---|---|
| Calls | 2,000 |
| Date range | 2025-12-01 to 2026-03-15 (America/New_York) |
| Call length | 1 to 12 minutes |
| Departments | public-works, utilities, finance, city-clerk, zoning-and-inspections, recreation, canvassing |
| Format | vCon `0.4.0` (`draft-ietf-vcon-vcon-core-04`) |
| Content | per-turn text dialogs, no audio |

Topic volumes, tones, and every planted finding are recorded in
`config/corpus.yaml` and `truth/`, not here. When testing a reader, human or
model, give it `vcons/` only. Everything outside that folder contains the
answers.

## Repo layout

```
config/         # corpus.yaml (frozen design constants), prompts/, one hand-written transcript
docs/           # pipeline.md, the file formats the scripts share
scripts/        # plan_corpus.py, generate.py, build_vcons.py, lint.py, verify.py
truth/          # manifest.json, decoys.json, verification.json
vcons/          # the deliverable, one vCon JSON file per call
build/          # gitignored scratch: specs, raw transcripts, verifier answers
```

Filenames follow `vcons/<YYYY-MM-DD>-<HHMM>-<department>-<first 8 hex of uuid>.vcon.json`.

## What's in each vCon

Each file is a self-contained vCon with `vcon: "0.4.0"`, built to
`vcon-core-04`.

- **Header.** `uuid`, `created_at` (the call end), and a `subject` of the form
  "Inbound call, Public Works". The subject names the department, never the
  topic.
- **`parties[]`.** The caller (`role: "resident"`) and the clerk
  (`role: "clerk"`), both with `validation: "synthetic"`. No phone numbers and
  no civic address.
- **`dialog[]`.** One `type: "text"` entry per turn, with `start`, `duration`,
  `parties`, `originator`, and a plain-text `body`. Turn durations sum to the
  call duration. Transcripts may contain noise markers such as `[inaudible]`
  and `[line breaks up]`.
- **`attachments[]`.** Three entries, each tied to party 0 and dialog 0:
  - `tags`: the department, and nothing else.
  - `call_detail`: direction, start, end, duration in seconds, and line
    channel.
  - `lawful_basis`: `legitimate_interests`, with all purposes granted at the
    call start and a proof mechanism that says the corpus is synthetic and has
    no real data subject.
- **`extensions: ["lawful_basis"]`.**

There is no `analysis` array. The corpus is meant to be read by a model.
Pre-labelled files, with topic, sentiment, or summary already attached, would
make that trivial. Anything a reader concludes, it concludes from the
dialog.

## Ground truth

`truth/manifest.json` lists every planted finding with its count and the vCon
UUIDs that carry it. `truth/decoys.json` lists the near misses.
`truth/verification.json` records the result of the blind verification pass.
The answers are not repeated here, because the corpus is a test.

The planted findings are of these kinds:

- a geographic complaint cluster
- repeat incidents at the same address
- escalation language
- payment card data read aloud
- a question asked again and again
- a pattern in when callers ask to pay

Each has decoys that look similar and are not the finding: a related word in a
harmless context, a partial number, a complaint just outside the cluster, a
neighbor with a near-identical question. Telling them apart takes a careful
read of the dialog.

Do not give the manifest to the system under test.

## Storms

Six fictional rain and wind events fall inside the window, and flooding calls
rise after them. The dates are illustrative. They are not the weather record,
and should not be compared with it.

## Pipeline

```
config/corpus.yaml
   | scripts/plan_corpus.py     -> build/specs.jsonl, truth/manifest.json, truth/decoys.json
   v
build/specs.jsonl
   | scripts/generate.py        (Claude Message Batches, claude-sonnet-5-5)
   v
build/transcripts/<uuid>.json
   | scripts/audit_cluster.py   (cluster calls checked against household fact sheets, claude-opus-5-5)
   | scripts/trim_tics.py       (deterministic trim of one overused clerk opener)
   | scripts/build_vcons.py     -> vcons/*.vcon.json
   v
   | scripts/lint.py            (deterministic checks over vcons/ and the manifest)
   | scripts/verify.py          (blind Claude read of every vCon, compared to the manifest)
```

Every planted count is derived from `config/corpus.yaml` by `plan_corpus.py`,
which writes the manifest. The generator writes each transcript from a call
spec, so the planted facts are put there on purpose. The verifier sees only
the department tag, the start time, and the transcript. It answers a fixed
questionnaire, and its answers are recomputed into findings and diffed against
the manifest by UUID.

The 40 generated calls in the planted drainage cluster were written with
`claude-opus-5-5` instead of Sonnet, because they share households and a dated
history across calls. Each household has a fact sheet in its call specs, and
`audit_cluster.py` checks every cluster call against it. Calls with a
contradiction are regenerated until none remain.

When the two disagree, fix the corpus, never the manifest. `verify.py
--mismatches-out` writes the UUIDs to regenerate. File formats are in
[`docs/pipeline.md`](docs/pipeline.md).

## Regenerating

Requires Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and an
`ANTHROPIC_API_KEY`.

```bash
uv sync
uv run python scripts/plan_corpus.py
uv run python scripts/generate.py --sample 40 --no-batch   # pilot
uv run python scripts/generate.py                          # full run, Message Batches
uv run python scripts/build_vcons.py
uv run python scripts/lint.py
uv run python scripts/verify.py
uv run python scripts/verify.py --compare-only             # no API calls
```

`generate.py` is resumable and skips transcripts that exist. Use `--only` or
`--only-file` to regenerate named UUIDs, `--resume` to collect an unfinished
batch, and `--dry-run` to print one rendered request. The seed is in
`config/corpus.yaml`. Changing a count there changes the manifest, so
regenerate the corpus after any edit.

## Quality caveat

The transcripts are written by a language model from call specs. They read
like calls, but they are not recordings, and they lack the disfluency,
overlap, and recognition errors of real speech-to-text. Regional detail is
limited to street names. The corpus tests whether a reader finds planted
patterns, not how a reader handles real audio. Do not use it to draw
conclusions about the City of Newport or its residents.

## License

[MIT](LICENSE). Copyright (c) 2026 Thomas Howe.
