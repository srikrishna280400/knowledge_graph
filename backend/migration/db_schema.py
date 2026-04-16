from sqlalchemy import text

def ensure_extension_batch_runs_table(session) -> None:
    session.execute(text("""
    CREATE TABLE IF NOT EXISTS extension_batch_runs (
        id TEXT PRIMARY KEY,
        profile_id TEXT,
        source_key TEXT NOT NULL,
        label TEXT,
        input_format TEXT NOT NULL,
        total_items INTEGER DEFAULT 0,
        new_saved_items INTEGER DEFAULT 0,
        existing_saved_items INTEGER DEFAULT 0,
        invalid_items INTEGER DEFAULT 0,
        status TEXT NOT NULL,
        error_text TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT
    )
    """))
    
def ensure_import_schema(session) -> None:
    ensure_extension_batch_runs_table(session)
    session.commit()