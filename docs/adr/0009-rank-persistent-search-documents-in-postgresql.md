# Rank persistent search documents in PostgreSQL

Replace [ADR 0008](0008-use-chat-isolated-paradedb.md)'s per-request snapshots
with incrementally maintained, physically separate chat indexes. Loading and
reconstructing every authorized original before native ranking took 30–55 seconds
on large chats. The target is a three-second complete recall tool call after
background preparation, including the query embedding and final evidence reads.

BM25 statistics now describe each indexed chat, independently of author/date
filters. Those filters and the trusted request cutoff still restrict candidates
before LIMIT. Accept this ranking change instead of building a new physical
index for every request. Preserve Unicode61 normalization, native prefix queries,
`k1=2.0`, `b=0.75`, and ordinal RRF between independent chat indexes.

The database queues changed originals and their two-level reply dependents.
Background preparation reads bounded batches, maintains reply-aware documents,
reuses cached vectors and updates per-chat HNSW indexes. A block may have a
shorter version when a parent's later edit makes its current text unavailable
at a historical cutoff. Version eligibility is checked before ranking limits;
queued stale documents cannot participate. A bounded SQL lexical delta search
keeps pending revisions discoverable without exporting bodies to Python. Bulk
initialization and ANN construction occur before runtime readiness.

Native BM25 and dense searches return bounded ID rankings and source IDs, never
original bodies. Fuse ranks and suppress duplicate sources first, then load
originals only for the final eight hits and their bounded supporting context.
Keep compact coverage metadata alongside the documents so counting does not
scan rows containing vectors. Force custom prepared plans for ranking; a generic
plan can lose ParadeDB's score-producing custom scan. Semantic recall remains
available even when stop words leave no lexical terms.
Exact history/count tools remain independent. ANN changes dense recall; evaluate
its recall against exact search under the same filters and measure complete tool
latency, including uncached query embeddings, instead of reporting index-only
timings. Cancellation joins sibling searches and shutdown drains active readers.
