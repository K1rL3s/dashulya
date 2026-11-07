ALTER TABLE messages ADD COLUMN IF NOT EXISTS trigger_message_id BIGINT;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid='messages'::regclass AND conname='messages_reply_trigger'
    ) THEN
        ALTER TABLE messages ADD CONSTRAINT messages_reply_trigger CHECK (
            trigger_message_id IS NULL OR (
                chat_id < 0 AND trigger_message_id > 0 AND trigger_message_id < message_id
            )
        );
    END IF;
END $$;
