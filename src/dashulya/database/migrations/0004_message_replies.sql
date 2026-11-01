-- Thread expansion and per-original reply counts share these lookup keys.
CREATE INDEX IF NOT EXISTS messages_replies ON messages(collection,reply_to_id,topic_id)
    WHERE reply_to_id IS NOT NULL;
