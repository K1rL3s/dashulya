"""Tune pg_search on physical per-chat indexes with a frozen validation split."""

# ruff: file-ignore[import-private-name] -- Share production tokenization/query rules without changing runtime interfaces.

import argparse
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import Connection, sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from dashulya.domain.memory import OriginalMessage
from dashulya.memory._unicode61 import tokens
from dashulya.memory.hybrid import _STOP_WORDS, query_terms
from dashulya.memory.store import original

from .corpus import Case, Document, distribution, documents, expand, write_json

ConnectionType = Connection[dict[str, Any]]
FIELDS = ("flat", "body", "parents", "authors")
SEED = "chat-isolated-bm25-v1"


@dataclass(frozen=True)
class Question:
    case: Case
    group: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Profile:
    model: str = "flat"
    k1: float = 1.2
    b: float = 0.75
    parents: float = 1.0
    authors: float = 1.0

    @property
    def label(self) -> str:
        return f"{self.model}_k{self.k1}_b{self.b}_p{self.parents}_a{self.authors}"


@dataclass(frozen=True)
class Prepared:
    number: int
    document: Document
    fields: dict[str, str]


def split_questions(questions: list[Question]) -> tuple[list[Question], list[Question]]:
    """Keep explicit episodes and all shared annotated evidence in one component."""
    roots = list(range(len(questions)))

    def root(number: int) -> int:
        while roots[number] != number:
            roots[number] = roots[roots[number]]
            number = roots[number]
        return number

    seen: dict[str, int] = {}
    for number, question in enumerate(questions):
        for key in ("group:" + question.group, *question.evidence_ids):
            if key in seen:
                roots[root(number)] = root(seen[key])
            else:
                seen[key] = number
    components: dict[int, list[Question]] = {}
    for number, question in enumerate(questions):
        components.setdefault(root(number), []).append(question)
    groups = sorted(
        components.values(),
        key=lambda group: hashlib.sha256(
            (SEED + ":" + ",".join(sorted(q.case.case_id for q in group))).encode()
        ).hexdigest(),
    )
    if len(groups) < 2:
        msg = "Tuning needs at least two independent evidence/episode components."
        raise ValueError(msg)
    boundary = max(1, min(len(groups) - 1, round(len(groups) * 0.65)))
    return (
        [question for group in groups[:boundary] for question in group],
        [question for group in groups[boundary:] for question in group],
    )


def prepare(originals: list[OriginalMessage], scope: Case) -> list[Prepared]:
    source = {f"{m.collection}/{m.message_id}": m for m in originals}
    result = []
    for number, document in enumerate(documents(originals, scope), 1):
        anchor = source[document.sources[-1]]
        raw = {
            "body": anchor.text,
            "parents": "\n\n".join(source[key].text for key in document.sources[:-1]),
            "authors": " ".join(dict.fromkeys([anchor.author, anchor.username or ""])),
        }
        fields = {name: " ".join(tokens(text)) for name, text in raw.items()}
        fields["flat"] = " ".join(document.terms)
        result.append(Prepared(number, document, fields))
    return result


def vocabulary(rows: list[Prepared]) -> dict[str, list[str]]:
    return {
        field: sorted({term for row in rows for term in row.fields[field].split()})
        for field in FIELDS
    }


def create_table(
    connection: ConnectionType,
    name: str,
    collection: str,
    rows: list[Prepared],
    profile: Profile,
) -> dict[str, Any]:
    """Each table physically accepts exactly one chat, including its statistics."""
    started = perf_counter()
    table = sql.Identifier(name)
    with connection.transaction():
        connection.execute(
            sql.SQL(
                "CREATE TABLE {} (id bigint PRIMARY KEY,collection text NOT NULL "
                "CHECK(collection={}),flat text NOT NULL,body text NOT NULL,"
                "parents text NOT NULL,authors text NOT NULL,author_id bigint,"
                "sent_at timestamptz NOT NULL,edited_at timestamptz)"
            ).format(table, sql.Literal(collection))
        )
        with connection.cursor().copy(
            sql.SQL("COPY {} FROM STDIN").format(table)
        ) as writer:
            for row in rows:
                assert row.document.collection == collection
                writer.write_row((
                    row.number,
                    collection,
                    *(row.fields[field] for field in FIELDS),
                    row.document.author_id,
                    row.document.sent_at,
                    row.document.edited_at,
                ))
        selected = (
            ("flat",) if profile.model == "flat" else ("body", "parents", "authors")
        )
        fields: list[sql.Composable] = [sql.Identifier("id")]
        for field in selected:
            options = [
                "lowercase=false",
                "pnorms=true",
                f"k1={profile.k1}",
                f"b={profile.b}",
            ]
            if profile.model == "split_stem" and field != "authors":
                options += ["stemmer=russian", "stopwords_language=russian"]
            fields.append(
                sql.SQL("({}::pdb.whitespace({}))").format(
                    sql.Identifier(field),
                    sql.SQL(",").join(sql.Literal(option) for option in options),
                )
            )
        fields += [
            sql.Identifier(field) for field in ("author_id", "sent_at", "edited_at")
        ]
        connection.execute(
            sql.SQL("CREATE INDEX ON {} USING paradedb ({}) WITH(key_field='id')").format(
                table, sql.SQL(",").join(fields)
            )
        )
        connection.execute(sql.SQL("ANALYZE {}").format(table))
        size = connection.execute(
            "SELECT pg_indexes_size(%s::regclass) AS index_bytes", (name,)
        ).fetchone()
        assert size is not None
    return {"build_ms": (perf_counter() - started) * 1000, "rows": len(rows), **size}


def build_query(
    table: str,
    case: Case,
    profile: Profile,
    words: dict[str, list[str]],
    limit: int = 160,
) -> tuple[sql.Composed, list[Any]] | None:
    prefixes = [word for prefix in query_terms(case.query) for word in tokens(prefix)]
    fullwords = list(
        dict.fromkeys(
            word
            for word in tokens(case.query)
            if len(word) >= 2 and word not in _STOP_WORDS
        )
    )[:48]
    fields = (
        [("flat", 1.0)]
        if profile.model == "flat"
        else [("body", 1.0), ("parents", profile.parents), ("authors", profile.authors)]
    )
    clauses = []
    parameters: list[Any] = []
    for field, weight in fields:
        if weight == 0:
            continue
        if profile.model == "split_stem" and field != "authors":
            if not fullwords:
                continue
            clauses.append(
                sql.SQL("({} ||| %s::pdb.boost({}))").format(
                    sql.Identifier(field), sql.Literal(weight)
                )
            )
            parameters.append(" ".join(fullwords))
        else:
            expanded = expand(prefixes, words[field])
            if not expanded:
                continue
            clauses.append(
                sql.SQL(
                    "({} @@@ pdb.match_disjunction(%s::text[])::pdb.boost({}))"
                ).format(sql.Identifier(field), sql.Literal(weight))
            )
            parameters.append(expanded)
    if not clauses:
        return None
    query = sql.SQL("SELECT id,pdb.score(id) AS score FROM {} WHERE ({})").format(
        sql.Identifier(table), sql.SQL(" OR ").join(clauses)
    )
    query += sql.SQL(" AND sent_at<%s AND (edited_at IS NULL OR edited_at<%s)")
    parameters += [case.cutoff, case.cutoff]
    for field, operator, value in (
        ("sent_at", ">=", case.after),
        ("sent_at", "<", case.before),
        ("author_id", "=", case.author_id),
    ):
        if value is not None:
            query += sql.SQL(" AND {} {} %s").format(
                sql.Identifier(field), sql.SQL(operator)
            )
            parameters.append(value)
    return query + sql.SQL(" ORDER BY pdb.score(id) DESC,id LIMIT {}").format(
        sql.Literal(limit)
    ), parameters


def search(
    connection: ConnectionType,
    table: str,
    case: Case,
    profile: Profile,
    words: dict[str, list[str]],
) -> list[dict[str, Any]]:
    query = build_query(table, case, profile, words)
    return connection.execute(*query).fetchall() if query is not None else []


def support_metrics(case: Case, sources: Sequence[tuple[str, ...]]) -> dict[str, Any]:
    """Score complete OR-of-AND evidence sets; parent originals remain evidence."""
    seen: set[str] = set()
    first_ranks: dict[int, int] = {}
    for rank, block in enumerate(sources[:32], 1):
        seen.update(block)
        for number, alternatives in enumerate(case.requirements):
            if number not in first_ranks and any(
                set(group) <= seen for group in alternatives
            ):
                first_ranks[number] = rank
    return {
        "requirements": len(case.requirements),
        "covered_8": sum(rank <= 8 for rank in first_ranks.values()),
        "covered_32": len(first_ranks),
        "reciprocal_rank_sum": sum(1 / rank for rank in first_ranks.values()),
    }


def evaluate(
    connection: ConnectionType,
    table: str,
    profile: Profile,
    questions: list[Question],
    rows: list[Prepared],
    words: dict[str, list[str]],
    repeats: int,
) -> dict[str, Any]:
    lookup = {row.number: row.document for row in rows}
    results: list[dict[str, Any]] = []
    durations: list[float] = []
    for question in questions:
        case = question.case
        samples = []
        ranking: list[int] | None = None
        error = None
        try:  # ruff: ignore[too-many-statements-in-try-clause] -- Catch database failures across the complete measured query loop.
            for sample in range(repeats + 1):
                started = perf_counter()
                found = search(connection, table, case, profile, words)
                elapsed = (perf_counter() - started) * 1000
                numbers = [row["id"] for row in found]
                assert len(numbers) == len(set(numbers))
                assert all(number in lookup for number in numbers), "Out-of-chat result"
                if ranking is not None:
                    assert ranking == numbers, "Unstable ranking on an unchanged index"
                ranking = numbers
                if sample:
                    samples.append(elapsed)
        except psycopg.Error as exception:
            error = type(exception).__name__
        metric = (
            support_metrics(case, [lookup[number].sources for number in ranking])
            if error is None and ranking is not None
            else None
        )
        durations += samples
        results.append({
            "case_id": case.case_id,
            "support": metric,
            "error": error,
            "samples_ms": samples,
            "top_8": [lookup[number].sources[-1] for number in (ranking or [])[:8]],
        })
    metrics = [row["support"] for row in results if row["support"] is not None]
    return {
        "profile": asdict(profile),
        "label": profile.label,
        "cases": results,
        "errors": sum(row["error"] is not None for row in results),
        "requirements": sum(len(q.case.requirements) for q in questions),
        **{
            name: sum(metric[name] for metric in metrics)
            for name in ("covered_8", "covered_32", "reciprocal_rank_sum")
        },
        "latency": distribution(durations),
    }


def selection_key(result: dict[str, Any]) -> tuple[float, ...]:
    profile = Profile(**result["profile"])
    return (
        -float(result["errors"]),
        float(result["covered_8"]),
        float(result["reciprocal_rank_sum"]),
        float(result["covered_32"]),
        float(profile.model == "flat"),
        -abs(profile.k1 - 1.2) - abs(profile.b - 0.75),
    )


def isolation_probe(
    connection: ConnectionType,
    table: str,
    scope: Case,
    words: dict[str, list[str]],
) -> dict[str, Any]:
    """Mutating an unrelated physical index must not change target scores."""
    preferred = [
        word for word in ("redis", "claude", "агент") if expand([word], words["flat"])
    ]
    alternatives = [
        word
        for word in words["flat"]
        if word.isalpha() and len(word) >= 4 and word not in _STOP_WORDS
    ]
    queries = list(dict.fromkeys([*preferred, *alternatives]))[:3]
    assert queries, "Isolation probe needs searchable terms in the target chat"
    probes = [
        Case("isolation", query, scope.cutoff, scope.collections) for query in queries
    ]
    before = [search(connection, table, case, Profile(), words) for case in probes]
    assert all(before), "Isolation probe must exercise nonempty target results"
    competing_text = " ".join(queries) + " "
    connection.execute(
        "CREATE TABLE unrelated_chat(id bigint PRIMARY KEY,flat text NOT NULL)"
    )
    connection.execute("INSERT INTO unrelated_chat VALUES (1,'initial unrelated text')")
    connection.execute(
        "CREATE INDEX ON unrelated_chat USING paradedb(id,flat) WITH(key_field='id')"
    )
    connection.execute("UPDATE unrelated_chat SET flat=repeat(%s,100)", (competing_text,))
    connection.execute(
        "INSERT INTO unrelated_chat SELECT n,repeat(%s,100) "
        "FROM generate_series(2,1001) AS n",
        (competing_text,),
    )
    connection.execute("VACUUM ANALYZE unrelated_chat")
    after = [search(connection, table, case, Profile(), words) for case in probes]
    assert before == after, "Another chat changed target BM25 scores or ordering"
    rejected = False
    try:
        connection.execute(
            sql.SQL(
                "INSERT INTO {} SELECT id+1000000,'synthetic:foreign',flat,body,"
                "parents,authors,author_id,sent_at,edited_at FROM {} LIMIT 1"
            ).format(sql.Identifier(table), sql.Identifier(table))
        )
    except psycopg.errors.CheckViolation:
        rejected = True
    assert rejected, "Chat table accepted a foreign collection"
    connection.execute("DROP TABLE unrelated_chat")
    digest = hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest()
    return {
        "independent_index_mutation": True,
        "foreign_rows_written": 1001,
        "target_scores_and_order_unchanged": before == after,
        "foreign_insert_rejected": rejected,
        "target_result_counts": [len(found) for found in before],
        "target_before_after_sha256": digest,
    }


def markdown(
    tuning: list[dict[str, Any]], validation: dict[str, Any], plan: dict[str, Any]
) -> str:
    best = sorted(tuning, key=selection_key, reverse=True)[:12]
    lines = [
        "# Chat-isolated ParadeDB tuning",
        "",
        "One physical table/index and vocabulary per chat and trusted historical cutoff.",
        "Other chats cannot affect corpus statistics. Date/author filters only narrow results.",
        "This is an offline experiment; no runtime ranking or access policy was changed.",
        "",
        f"Tuning questions: {len(plan['tuning'])}; validation questions: {len(plan['validation'])}.",
        "Explicit episodes and shared annotated originals remain in one split component.",
        "The selected profile was frozen before validation. Both splits are development data,",
        "with nonexhaustive, source-checked draft annotations; they are not human-reviewed held-out gold.",
        "No model, embedding, Telegram, answer generation or semantic judging calls were made.",
        "Original questions were used directly as search queries, without agent reformulation.",
        "",
        "## Validation",
        "",
        "| Profile | Covered requirements at 8 | Covered at 32 | p50 ms | p95 ms | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for label, result in validation.items():
        latency = result["latency"]
        lines.append(
            f"| {label}: {result['label']} | {result['covered_8']}/{result['requirements']} | "
            f"{result['covered_32']}/{result['requirements']} | {latency.get('p50_ms')} | "
            f"{latency.get('p95_ms')} | {result['errors']} |"
        )
    lines += [
        "",
        "## Best tuning profiles (validation did not choose these)",
        "",
        "| Profile | Covered at 8 | Covered at 32 | p50 ms |",
        "| --- | ---: | ---: | ---: |",
    ]
    for result in best:
        lines.append(
            f"| {result['label']} | {result['covered_8']}/{result['requirements']} | "
            f"{result['covered_32']}/{result['requirements']} | {result['latency'].get('p50_ms')} |"
        )
    lines += [
        "",
        "Coverage counts complete OR-of-AND original-message support sets in returned blocks,",
        "including their authorized parents. It is not answer accuracy or exhaustive relevance.",
        "A failed query is an explicit error and has no quality score.",
        "Latency includes prefix expansion from a resident per-chat vocabulary and warm SQL retrieval.",
        "Snapshot loading, blocks, tokenization, vocabulary and index builds are separate setup costs.",
        "",
        "The flat control preserves the previous metadata-rich block. Split variants score body,",
        "parent text and author names separately, excluding technical metadata from lexical text.",
        "Russian-stem variants use native string analysis for body/parents and exact expanded tokens",
        "for authors. A zero field weight removes its query arm, including parent/author-only matches.",
        "",
        "This run fixes the historical corpus at the questions' cutoff. A permanent per-chat index",
        "would still need a decision about statistics under later date/author filters and historical replay.",
        "Owner-private fusion across independent chat indexes is outside this experiment.",
        "",
    ]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> None:  # ruff: ignore[too-many-locals,too-many-statements] -- Ordered experimental phases keep selection before validation explicit.
    url = os.environ.get("BM25_DATABASE_URL", "")
    if conninfo_to_dict(url).get("dbname") != "bm25_eval":
        msg = "Use the marked, disposable bm25_eval database."
        raise ValueError(msg)
    out = args.out.resolve()
    if not out.is_relative_to(Path("evals/runs").resolve()) or out.exists():
        msg = "Choose a new output directory under evals/runs."
        raise ValueError(msg)
    case_bytes = args.cases.read_bytes()
    raw = [json.loads(line) for line in case_bytes.splitlines() if line]
    questions = [
        Question(Case.load(row), row["split_group"], tuple(row["evidence_ids"]))
        for row in raw
    ]
    if len({q.case.case_id for q in questions}) != len(questions):
        msg = "Duplicate question ID."
        raise ValueError(msg)
    if not questions or any(not q.case.requirements for q in questions):
        msg = "Select annotated positive retrieval questions."
        raise ValueError(msg)
    scope = questions[0].case
    if len(scope.collections) != 1 or any(
        q.case.collections != scope.collections or q.case.cutoff != scope.cutoff
        for q in questions
    ):
        msg = "Each tuning run requires one chat and one trusted historical cutoff."
        raise ValueError(msg)
    train, holdout = split_questions(questions)
    plan = {
        "seed": SEED,
        "cases_sha256": hashlib.sha256(case_bytes).hexdigest(),
        "collection": scope.collections[0],
        "cutoff": scope.cutoff.isoformat(),
        "tuning": [q.case.case_id for q in train],
        "validation": [q.case.case_id for q in holdout],
        "selection": "coverage at 8, reciprocal support rank through 32, coverage at 32; simpler/default parameters break ties",
        "parameter_grid": {"k1": [0.6, 1.2, 2.0], "b": [0.0, 0.4, 0.75]},
        "weight_grid": {"parents": [0.0, 0.25, 0.5, 1.0], "authors": [0.0, 1.0, 2.0]},
    }
    out.mkdir(parents=True)
    write_json(out / "plan.json", plan)
    schema = "bm25_tune_" + uuid4().hex
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
        environment = connection.execute(
            "SELECT version() AS postgres,"
            "(SELECT extversion FROM pg_extension WHERE extname='pg_search') AS pg_search,"
            "to_regclass('bm25_benchmark_guard') AS guard,"
            "current_setting('shared_buffers') AS shared_buffers,"
            "current_setting('max_parallel_workers_per_gather') AS parallel_workers"
        ).fetchone()
        if (
            not environment
            or not environment["guard"]
            or environment["pg_search"] != "0.26.4"
        ):
            msg = "A marked pg_search 0.26.4 evaluation database is required."
            raise ValueError(msg)
        connection.execute("SET statement_timeout=20000")
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            source = connection.execute(
                "SELECT * FROM public.messages WHERE collection=%s", scope.collections
            ).fetchall()
            originals = [original(row) for row in source]
        prepared_at = perf_counter()
        corpus_scope = Case("corpus", "", scope.cutoff, scope.collections)
        rows = prepare(originals, corpus_scope)
        if not rows:
            msg = "The isolated snapshot does not contain this chat."
            raise ValueError(msg)
        words = vocabulary(rows)
        environment.update(
            rows=len(rows),
            preparation_ms=(perf_counter() - prepared_at) * 1000,
            repeats=args.repeats,
            provider_calls=0,
            started_at=datetime.now(UTC).isoformat(),
        )
        write_json(out / "environment.json", environment)
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET search_path TO {},public").format(sql.Identifier(schema))
        )
        results: list[dict[str, Any]] = []
        counter = 0

        def experiment(
            profiles: list[Profile], selected: list[Question], phase: str
        ) -> list[dict[str, Any]]:
            nonlocal counter
            table = f"chat_{counter}"
            counter += 1
            index = create_table(
                connection, table, scope.collections[0], rows, profiles[0]
            )
            if profiles[0] == Profile() and phase == "tuning":
                write_json(
                    out / "isolation.json",
                    isolation_probe(connection, table, scope, words),
                )
            batch = []
            for profile in profiles:
                result = evaluate(
                    connection, table, profile, selected, rows, words, args.repeats
                )
                result.update(phase=phase, index=index)
                batch.append(result)
                write_json(out / f"{phase}-{profile.label}.json", result)
                print(  # ruff: ignore[print] -- CLI progress omits private query/source text.
                    phase,
                    profile.label,
                    f"{result['covered_8']}/{result['requirements']}",
                    result["latency"],
                    flush=True,
                )
            connection.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
            return batch

        try:
            defaults = Profile()
            numeric = [
                defaults,
                *[
                    Profile(k1=k1, b=b)
                    for k1 in (0.6, 1.2, 2.0)
                    for b in (0.0, 0.4, 0.75)
                    if (k1, b) != (1.2, 0.75)
                ],
            ]
            for profile in numeric:
                results += experiment([profile], train, "tuning")
                write_json(out / "tuning.json", results)
            best_flat = Profile(**max(results, key=selection_key)["profile"])
            parameter_pairs = sorted({(1.2, 0.75), (best_flat.k1, best_flat.b)})
            for model in ("split_prefix", "split_stem"):
                for k1, b in parameter_pairs:
                    profiles = [
                        Profile(model, k1, b, parent, author)
                        for parent in (0.0, 0.25, 0.5, 1.0)
                        for author in (0.0, 1.0, 2.0)
                    ]
                    results += experiment(profiles, train, "tuning")
                    write_json(out / "tuning.json", results)
            valid = [result for result in results if result["errors"] == 0]
            if not valid:
                msg = "No tuning profile completed every question."
                raise RuntimeError(msg)
            selected_result = max(valid, key=selection_key)
            selected_profile = Profile(**selected_result["profile"])
            write_json(
                out / "selected.json",
                {
                    "profile": asdict(selected_profile),
                    "tuning_covered_8": selected_result["covered_8"],
                    "selected_before_validation_at": datetime.now(UTC).isoformat(),
                },
            )
            validation = {
                "baseline": experiment([defaults], holdout, "validation_baseline")[0],
                "selected": experiment(
                    [selected_profile], holdout, "validation_selected"
                )[0],
            }
            write_json(out / "validation.json", validation)
            (out / "report.md").write_text(
                markdown(results, validation, plan), encoding="utf-8"
            )
        finally:
            connection.execute("SET search_path TO public")
            connection.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    run(args)


if __name__ == "__main__":
    main()
