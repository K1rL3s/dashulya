# Retire legacy storage adapters and reference tests

Use PostgreSQL for maintained runtime, maintenance, evaluation storage and storage
tests. Remove the retired SQLite/OpenViking adapters, migration/export CLI,
dependencies and SQLite reference comparisons instead of maintaining a second
storage implementation after the PostgreSQL cutover.

Keep the existing Unicode 6.1 tokenization data and ranking behavior, covered by
fixed tokenizer contracts and PostgreSQL retrieval tests. Historical decisions,
source snapshots, backups and already persisted PostgreSQL records remain intact;
removing the adapters does not delete or rewrite user data.
