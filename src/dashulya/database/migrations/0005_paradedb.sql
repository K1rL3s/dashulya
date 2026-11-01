CREATE EXTENSION IF NOT EXISTS pg_search VERSION '0.26.4';

DO $$ BEGIN
    IF (SELECT extversion FROM pg_extension WHERE extname='pg_search') <> '0.26.4' THEN
        RAISE EXCEPTION 'Dashulya requires pg_search 0.26.4';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS lexical_cache_config (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
    namespace NAME NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS lexical_snapshots (
    snapshot_key TEXT PRIMARY KEY,
    table_name NAME NOT NULL UNIQUE,
    collection TEXT NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

DO $$ DECLARE namespace TEXT := 'dashulya_bm25_' || md5(current_schema()); BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION dashulya_app', namespace);
    INSERT INTO lexical_cache_config(namespace) VALUES(namespace)
        ON CONFLICT(singleton) DO UPDATE SET namespace=EXCLUDED.namespace;
    EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC, dashulya_history_reader', namespace);
    EXECUTE format('REVOKE ALL ON %I.lexical_cache_config FROM PUBLIC, dashulya_history_reader, dashulya_app', current_schema());
    EXECUTE format('REVOKE ALL ON %I.lexical_snapshots FROM PUBLIC, dashulya_history_reader', current_schema());
    EXECUTE format('GRANT SELECT ON %I.lexical_cache_config TO dashulya_app', current_schema());
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I.lexical_snapshots TO dashulya_app', current_schema());
END $$;
