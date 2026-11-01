# ParadeDB tuning

Tune native `pg_search` 0.26.4 profiles in a marked, disposable database named
`bm25_eval`. Read the local corpus README before use. Copy canonical originals
with a consistent read-only snapshot and preserve the source. The runner requires
`bm25_benchmark_guard` and never accepts the application database.

It makes no model, embedding or Telegram calls. Questions, source requirements
and original-message cutoffs are evaluator inputs; annotations never enter
indexed text. Generated indices and results belong under ignored `evals/runs/`.

## Procedure

`python -m evals.suites.bm25` uses one physical table/index and vocabulary
per chat, with a database CHECK constraint rejecting another collection. A
separate synthetic chat is updated and filled with competing terms; the probe
requires the target chat's scores and ordering to remain exactly unchanged.
It does not rely on filtering a global index or on segment partitioning.

```sh
BM25_DATABASE_URL=postgresql://USER@localhost:PORT/bm25_eval \
  uv run --locked python -m evals.suites.bm25 \
  --cases evals/runs/YOUR_RUN/cases.jsonl \
  --out evals/runs/YOUR_RUN/results --repeats 3
```

Input cases use the existing contract plus `split_group` and `evidence_ids`.
Every case must have positive retrieval requirements and the same single
collection and exclusive historical cutoff. Originals come from the marked
disposable database, read in a read-only transaction. Counts and unanswerable
questions are inappropriate for this support-coverage metric. Archives retain
their unbound collection identities; no Telegram numeric binding is invented.

The split is deterministic: explicit episodes and shared annotated sources form
connected components, assigned approximately 65% to tuning and 35% to validation.
Only tuning results choose parameters. `selected.json` is written before any
validation query. Both partitions remain development data; this does not create
new human-reviewed or never-seen gold. The source corpus is available to both
partitions as ordinary retrieval input, while annotations never enter the index.

The first stage tests `k1` in 0.6/1.2/2.0 and `b` in 0/0.4/0.75 with the previous
flat block. The second stage tests default/best-flat parameters on separate
body/parents/author fields, with parent weights 0/0.25/0.5/1 and author weights
0/1/2. Body weight stays 1. Zero weight removes the query arm. Both prefix
expansion and native Russian stemming/stopword variants are tested. Native
stemmed fields use analyzed query strings; exact arrays bypass analysis.

Selection maximizes covered requirements at eight, then reciprocal support rank
through 32 and coverage at 32. Simpler/default settings break ties. Failed query
profiles cannot win. Baseline and the frozen winner alone run validation. The
reports retain per-question evidence coverage, errors, warm latency and index
build costs. All temporary schemas are removed in `finally`.

Each historical index contains only its authorized chat prefix. Author/date
filters narrow candidates. This offline runner tests native profiles, not the
runtime snapshot cache, dense fusion or the reply agent. Runtime access and
maintenance behavior are covered by the integration suite and ADR 0008.
