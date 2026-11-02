CREATE TABLE IF NOT EXISTS search_pending (
    collection TEXT NOT NULL,
    message_id BIGINT NOT NULL,
    version BIGINT GENERATED ALWAYS AS IDENTITY,
    PRIMARY KEY(collection,message_id)
);

INSERT INTO search_pending(collection,message_id)
    SELECT collection,message_id FROM messages ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION queue_search_blocks() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE source messages; BEGIN
    IF TG_OP='UPDATE' AND NEW.revision=OLD.revision THEN RETURN NEW; END IF;
    IF TG_OP='DELETE' THEN source:=OLD; ELSE source:=NEW; END IF;
    INSERT INTO search_pending(collection,message_id)
        SELECT source.collection,source.message_id
        UNION
        SELECT collection,message_id FROM messages
            WHERE collection=source.collection AND reply_to_id=source.message_id
        UNION
        SELECT c.collection,c.message_id FROM messages c JOIN messages p
            ON c.collection=p.collection AND c.reply_to_id=p.message_id
            WHERE p.collection=source.collection AND p.reply_to_id=source.message_id
    ON CONFLICT(collection,message_id) DO UPDATE SET version=DEFAULT;
    IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
END $$;

DROP TRIGGER IF EXISTS queue_search_blocks ON messages;
CREATE TRIGGER queue_search_blocks AFTER INSERT OR UPDATE OR DELETE ON messages
    FOR EACH ROW EXECUTE FUNCTION queue_search_blocks();

GRANT SELECT,INSERT,UPDATE,DELETE ON search_pending TO dashulya_app;
GRANT USAGE,SELECT ON SEQUENCE search_pending_version_seq TO dashulya_app;
REVOKE ALL ON search_pending FROM PUBLIC,dashulya_history_reader;

CREATE INDEX IF NOT EXISTS messages_recall_coverage
    ON messages(chat_id,sent_at,message_id) INCLUDE(collection,provenance,edited_at);
