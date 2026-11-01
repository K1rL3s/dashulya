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

## Diagnose a history-summary conversation

`python -m evals.suites.memory.scenario` replays JSONL scenarios through the
production `Dialogue`, reply-agent factory, memory tools, formatter and PostgreSQL.
Each row supplies `case_id`, `session`, `query`, `chat_id`, `user_id`,
`target_chat_id`, timezone-aware `sent_at`, `after` and exclusive `before`.
Optional `author_id` describes the expected recall filter. Rows sharing a session
retain private agent context; distinct sessions start fresh. Group requests use
the normal latest-20-plus-ancestors context. Requests and simulated deliveries
are recorded only in the disposable evaluation schema.

Use `EVAL_SOURCE_DATABASE_URL` for the source, or the application's `DATABASE_URL`.
The source is opened read-only at repeatable read. Canonical rows after the first
request cutoff and rows edited at/after it are excluded. Existing lexical and
vector caches are copied unchanged, including unused entries; authorization and
current content keys still determine candidates. Preparation does not re-embed
history. `EVAL_DATABASE_URL` must be a separate disposable database. This copies
potentially large caches; preparation time is reported separately.

Start with a fully local pipeline probe:

```sh
uv run --locked python -m evals.suites.memory.scenario --scripted \
  --cases evals/datasets/YOUR_DATASET/cases.jsonl \
  --out evals/runs/history-pipeline
```

`--scripted` uses predetermined calls and synthetic query vectors. It creates no
OpenAI client and makes no model, embedding, Telegram or web API calls. Its timing
measures local integration, SQL and context assembly; it does not measure model
latency, token usage, retrieval relevance or answer quality. It can still expose
context accumulation and scope/filter regressions. Unknown token usage remains
unscored instead of being fabricated as zero.

For a separately authorized live run, omit `--scripted` and supply `--model` (or
`OPENAI_MODEL`) and `OPENAI_API_KEY`. This sends prompts, questions and retrieved
original fragments to the configured OpenAI model, and new query text to the
embedding API. Owner-private scope can retrieve every saved chat, including the
owner's private records. System/tool prompts and retrieved text are recorded in
ignored run artifacts. Do not publish those artifacts. There is no semantic judge
or human gold in this diagnostic mode; citation identity is not entailment.

`report.md` summarizes timings, context growth, checks and per-search stages.
Each case JSON stores every native AG2 model input, system prompt, tool schema,
call arguments/results, token usage when available, SQL/search logs correlated
to tool-call IDs and embedding timings. Saved JSON redacts configured credentials.
`summary.json` holds objective checks; `snapshot.json` records preparation counts
and timing, including ANALYZE after bulk loading for representative query plans.
SQL columnar tables are expanded only for scoring; model contexts retain the
exact compact output. Seen-source counts include explicit canonical IDs or
original URLs, and do not infer identity from unqualified numeric IDs.
Case files are saved as each case finishes. Development limits are
12 model calls, 24 tool calls, 50000 input tokens per model call and Dialogue's
90-second generation deadline; limits are diagnostics, not release SLAs.

The trace checks cover execution, tool errors, SQL batch shape, hidden chat
authority, bounded author/date recall, retrieved-source scope, known/exposed
citations and context-token budgets. They do not establish historical completeness
or factual/semantic correctness. Telegram routing, delivery, reactions, web tools
and image downloads are outside this text replay; integration tests cover those
boundaries separately. Tests for this runner use synthetic data and scripted AG2
responses against an isolated PostgreSQL, without private datasets or model APIs.
