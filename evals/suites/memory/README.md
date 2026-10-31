# Memory evaluations

This suite grades historical recall through the production reply-agent factory,
`search_memory`, `chat_history`, formatter and hybrid PostgreSQL memory. It uses
AG2 Evals from the existing locked `ag2` dependency; no separate eval package is
needed. Preparation does not contact models. Only `run` calls the embedding,
answering and judging APIs.

## Prepare and review without model calls

Run commands from the repository root after the normal `uv sync` setup. Obtain
the local dataset separately on a fresh clone and read its provenance README.
The default package is `evals/datasets/ostrikov/v2/`; it references immutable v1
originals rather than copying them. All dataset content remains ignored.

```sh
uv run --locked python -m evals.suites.memory validate
uv run --locked python -m evals.suites.memory export \
  --out evals/runs/memory-review
```

The output directory must be new and under `evals/runs/`. `queries.jsonl` is
answerer-only input. `review.jsonl` contains evaluator-only facts, forbidden
claims, support alternatives, full reference originals, cutoff, category and
blank human labels. `plan.json` records the selection and dataset hash. The
dataset's own `questions.md` is the readable list of questions and expected
findings. Neither export nor validation creates an index or uses an API key.

Use repeated `--case CASE_ID` options to prepare a subset. Validation checks
manifest hashes, query/gold separation, exact source quotations and support
availability before each cutoff. Count references are recomputed from all
matching originals, including empty-text records. The input contract is the
annotated v2 format; unannotated v1 requests are not interchangeable with it.

Each factual requirement has OR alternatives of AND support sets. Expected
answers are semantic references. Forbidden claims describe unsupported
conclusions, including wrong attribution, fabricated details, obsolete facts
presented as current, and opinions presented as consensus. Negative cases mean
insufficient evidence in authorized saved coverage, not universal nonexistence.

## Run later, when explicitly requested

These commands are instructions for a future run. Exported plans have status
`prepared_not_run` and contain no measured model-quality result.

Set `OPENAI_API_KEY` in the environment or local `.env`; existing environment
variables win. No Telegram token, Exa key or Telegram connection is needed.
Supply the answering and judging models explicitly, or set `OPENAI_MODEL` and
`OPENAI_EVAL_JUDGE_MODEL`. Set `EVAL_DATABASE_URL` to a separate, disposable
pgvector-enabled PostgreSQL database. The suite rejects the application database
and any database containing `public.messages`.

```sh
uv run --locked python -m evals.suites.memory run \
  --model "$OPENAI_MODEL" \
  --judge-model "$OPENAI_EVAL_JUDGE_MODEL" \
  --out evals/runs/memory-first-run
```

One new schema is built per distinct historical cutoff. Only that chat's
canonical messages strictly before `(send time, message ID)` are imported;
neither later replies nor annotation-selected evidence enters the index.
Original text and identities are preserved. Linked channel posts and external
pages are not added. Saved scrape text has no edit-version history, so replay
cannot establish which edited text existed at an earlier date.

The source dump has no verified numeric group identity. The suite uses one
unbound archive in an evaluation owner's private scope and never invents a live
Telegram chat binding. Physical prefix isolation additionally enforces the
dataset's single-chat scope. This measures memory/tool use, not group routing,
activation, multi-chat authorization, Telegram delivery or media interpretation.
Existing integration tests cover access enforcement separately.

Preparation uses production BM25 and `text-embedding-3-small` at 512 dimensions.
It builds fresh vectors and can be costly even for a small case selection; it
does not reuse or mutate maintained application indices. Schemas are removed in
`finally` after their cases finish. A process killed without cleanup may leave an
`eval_memory_*` schema in the disposable database. Source snapshots and `var/`
are never modified. There is no background indexing retry that hides a partial
preparation failure.

Cases run sequentially with a fresh stream and a 90-second generation deadline.
The agent chooses its memory calls. Only original historical context included
in input-only queries is supplied; authored questions receive no gold-selected
context. The same agent is reused for cases with the same cutoff. Web and
reaction tools are omitted because the subject is saved chat memory.

## Scores and artifacts

| Signal | Meaning |
| --- | --- |
| `execution` | A nonempty structured answer completed without an agent exception. |
| `retrieval_scope` | Returned source identities belong to the case's authorized prefix. |
| `citation_identity` | Original-message citations exist in that prefix and were exposed to the answerer; answerable cases require a citation. |
| `annotated_support_coverage` | Fraction of requirements with a complete annotated AND set among seen sources. Diagnostic, not a pass gate: support annotation is nonexhaustive. Omitted for counts and requirements without support. |
| `canonical_count` | Counts use a successful canonical `chat_history(count)` result with the exact author/date/text predicate and expected integer. |
| `semantic_quality` | Provisional judge: all required facts, attribution, forbidden conclusions, grounding, citation entailment and coverage qualification. |
| `factual_answer` / `abstention` | Separate semantic outcomes for answerable and insufficient-evidence cases. |
| `faithfulness` | Provisional judgment of additional claims and citation support against actually observed originals. |

Identity checks do not establish that a cited text entails a claim. The semantic
judge checks that separately, including extra factual assertions. A count answer
also needs the correct integer and saved-coverage qualification; exact counts
are not measured by top-k recall. An empty search result is never proof that
something was not discussed. Judge failures or malformed verdicts are unscored
and cannot pass; missing traces remain in the denominator as errors.

`ag2-results.json` contains typed traces, tool arguments/results, answers,
evaluator references and feedback in AG2's persisted format. `summary.json`
reports separate category and answerability slices, including counts and
abstention. A case passes only when all applicable objective checks and the
semantic verdict pass. `runtime.json` records model names, installed AG2 version
and judge prompt hash; `plan.json` records the source-manifest hash. Subject-agent
token usage in AG2 results excludes embedding and judge costs. Outputs contain
private chat text and remain ignored.

## Review before relying on scores

The supplied annotations are source-checked development drafts, not human gold
or a held-out benchmark. Review the question/reference pairs, then label future
answers independently in `review.jsonl`. Preserve `split_group` when splitting
shared sources and episodes. Calibrate judge decisions against domain-expert
pass/fail labels on separate development and held-out data before using them
for release decisions. No calibrated threshold or achieved accuracy is asserted.

The supplied [evals-skills recommendations](https://github.com/ai-evals-course/evals-skills)
inform this scaffold: [separate retrieval and generation assessment](https://github.com/ai-evals-course/evals-skills/blob/main/skills/evaluate-rag/SKILL.md),
[check objective failures in code](https://github.com/ai-evals-course/evals-skills/blob/main/skills/write-code-eval/SKILL.md)
and [calibrate evaluators against human judgments](https://github.com/ai-evals-course/evals-skills/blob/main/skills/validate-evaluator/SKILL.md).
This initial behavior contract does not claim to replace error discovery on
observed agent traces.

The adapter calls `agent.ask` with trusted session dependencies, records public
typed stream events and grades them with `Trace`, `InMemoryTraceSource` and
`evaluate_traces`. In locked AG2 1.2.1, `run_agent` cannot forward Dashulya's
per-request `MemoryAccess`. The public trace-grading API preserves that boundary;
see [AG2 evaluation runs](https://docs.ag2.ai/docs/user-guide/evaluation/runs/).

Infrastructure tests use temporary synthetic messages and scripted `TestConfig`
responses, independently of private datasets, PostgreSQL and model services:

```sh
just test-local
```
