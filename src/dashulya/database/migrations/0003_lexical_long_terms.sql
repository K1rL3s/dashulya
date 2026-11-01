-- Short prefixes use lexical_short_prefix. Keep the rare oversized tokens
-- searchable without scanning every ordinary posting or exceeding B-tree limits.
CREATE INDEX IF NOT EXISTS lexical_long_terms ON lexical_terms(document_key,term_key)
    WHERE octet_length(term)>=2000;
