import uuid
from sqlalchemy import text


def ensure_pipeline_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT PRIMARY KEY,
            batch_file TEXT NOT NULL,
            groq_output_file TEXT,
            normalized_file TEXT,
            graph_db TEXT,
            vault_path TEXT,
            root_folder TEXT,
            status TEXT NOT NULL,
            current_step TEXT,
            error_text TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT
        )
        """))

        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pipeline_steps (
            run_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL,
            artifact_path TEXT,
            error_text TEXT,
            started_at TEXT DEFAULT (datetime('now')),
            finished_at TEXT,
            PRIMARY KEY (run_id, step_name)
        )
        """))


def create_run(
    engine,
    *,
    batch_file: str,
    graph_db: str,
    vault_path: str,
    root_folder: str,
) -> str:
    run_id = str(uuid.uuid4())

    with engine.begin() as conn:
        conn.execute(text("""
        INSERT INTO pipeline_runs (
            run_id,
            batch_file,
            graph_db,
            vault_path,
            root_folder,
            status,
            current_step
        )
        VALUES (
            :run_id,
            :batch_file,
            :graph_db,
            :vault_path,
            :root_folder,
            'running',
            'init'
        )
        """), {
            "run_id": run_id,
            "batch_file": batch_file,
            "graph_db": graph_db,
            "vault_path": vault_path,
            "root_folder": root_folder,
        })

    return run_id


def mark_step_start(engine, run_id: str, step_name: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        INSERT INTO pipeline_steps (
            run_id,
            step_name,
            status,
            started_at,
            finished_at,
            error_text,
            artifact_path
        )
        VALUES (
            :run_id,
            :step_name,
            'running',
            datetime('now'),
            NULL,
            NULL,
            NULL
        )
        ON CONFLICT(run_id, step_name) DO UPDATE SET
            status='running',
            started_at=datetime('now'),
            finished_at=NULL,
            error_text=NULL,
            artifact_path=NULL
        """), {
            "run_id": run_id,
            "step_name": step_name,
        })

        conn.execute(text("""
        UPDATE pipeline_runs
        SET status='running',
            current_step=:step_name,
            error_text=NULL
        WHERE run_id=:run_id
        """), {
            "run_id": run_id,
            "step_name": step_name,
        })


def mark_step_done(
    engine,
    run_id: str,
    step_name: str,
    artifact_path: str | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        UPDATE pipeline_steps
        SET status='done',
            artifact_path=:artifact_path,
            finished_at=datetime('now')
        WHERE run_id=:run_id
          AND step_name=:step_name
        """), {
            "run_id": run_id,
            "step_name": step_name,
            "artifact_path": artifact_path,
        })


def mark_step_failed(
    engine,
    run_id: str,
    step_name: str,
    error_text: str,
) -> None:
    msg = (error_text or "")[:4000]

    with engine.begin() as conn:
        conn.execute(text("""
        UPDATE pipeline_steps
        SET status='failed',
            error_text=:error_text,
            finished_at=datetime('now')
        WHERE run_id=:run_id
          AND step_name=:step_name
        """), {
            "run_id": run_id,
            "step_name": step_name,
            "error_text": msg,
        })

        conn.execute(text("""
        UPDATE pipeline_runs
        SET status='failed',
            current_step=:step_name,
            error_text=:error_text,
            finished_at=datetime('now')
        WHERE run_id=:run_id
        """), {
            "run_id": run_id,
            "step_name": step_name,
            "error_text": msg,
        })


def mark_run_done(
    engine,
    run_id: str,
    *,
    groq_output_file: str,
    normalized_file: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
        UPDATE pipeline_runs
        SET status='done',
            current_step='done',
            groq_output_file=:groq_output_file,
            normalized_file=:normalized_file,
            finished_at=datetime('now')
        WHERE run_id=:run_id
        """), {
            "run_id": run_id,
            "groq_output_file": groq_output_file,
            "normalized_file": normalized_file,
        })
