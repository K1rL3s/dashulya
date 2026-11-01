# Use chat-isolated ParadeDB snapshots for lexical recall

Replace [ADR 0005](0005-unify-memory-in-postgresql.md)'s hand-written SQL BM25
with ParadeDB `pg_search` 0.26.4, using the tested flat reply blocks with
`k1=2.0` and `b=0.75`. Native retrieval substantially reduced measured query and
build costs; development relevance reviews found many ties, several SQL wins
and some native wins, so this decision accepts different ranking rather than
claiming equivalent relevance. Canonical originals, cached pgvector embeddings,
trusted memory access, source diversity and original-message evidence remain.

Build a separate physical native table/index for each chat's exact authorized
block corpus. Corpus identity includes every rendered original and its saved
reply parents: a later edit, missing parent, deletion, historical cutoff or
primary filter produces a different snapshot. This preserves authorized corpus
statistics and prevents a historical child's index from retaining a later
parent's text. A single current chat index with anchor-time filters cannot
provide that property. Author/date eligibility still applies before LIMIT.

Retain eight recently used snapshots in a bounded durable database cache;
active snapshots may temporarily exceed that bound. Transaction advisory locks
coordinate reuse, construction and eviction across processes. Building and
registering an index are atomic, so cancellation leaves no partial relation.
The runtime owns only its separate derived schema, with no CREATE grant on the
canonical schema; model-facing history readers cannot access that cache.
New snapshots require index construction and changed blocks require tokenization;
unchanged blocks reuse normalized text from resident snapshots. Warm-query
benchmarks therefore do not describe the first request for a new corpus.

Preserve Unicode61 normalization and the existing Russian query heuristic.
Expand prefixes against the exact snapshot's uncapped vocabulary and use native
exact-term BM25. This intentionally replaces pooled-prefix SQL scoring. In
owner-private search, merge per-chat ordinal ranks with RRF before the existing
lexical/dense fusion: raw BM25 scores from independent indexes are incomparable.
No shared global lexical statistics or SQL-scoring fallback remain at runtime.

Compose adds pg_search to PostgreSQL 16 while retaining the existing pgvector
0.8.7 binaries and SQL files. The additive migration installs the pinned extension
in its required `paradedb` schema and creates the cache metadata. A follow-up
migration drops the retired SQL posting tables; the former scorer and comparison
runners are removed. Originals and embeddings are not rebuilt or deleted.
