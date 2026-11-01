DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dashulya_app') THEN
        CREATE ROLE dashulya_app LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='dashulya_history_reader') THEN
        CREATE ROLE dashulya_history_reader NOLOGIN;
    END IF;
END $$;

ALTER ROLE dashulya_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
ALTER ROLE dashulya_history_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
GRANT dashulya_history_reader TO dashulya_app;

DO $$ BEGIN
    EXECUTE format('GRANT dashulya_history_reader TO %I', current_user);
    EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM PUBLIC', current_schema());
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC, dashulya_history_reader', current_schema());
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO dashulya_app', current_schema());
    EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO dashulya_app', current_schema());
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO dashulya_app', current_database());
END $$;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM dashulya_app;

REVOKE EXECUTE ON FUNCTION pg_catalog.set_config(TEXT, TEXT, BOOLEAN) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION pg_catalog.set_config(TEXT, TEXT, BOOLEAN) TO dashulya_app;
