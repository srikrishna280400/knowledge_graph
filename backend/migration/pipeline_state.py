# pipeline_state.py

import uuid
from sqlalchemy import text

PIPELINE_SCHEMA = "pipeline"


def _is_sqlite(engine) -> bool:
    return engine.dialect.name == "sqlite"


def _table(engine, name: str) -> str:
    return name if _is_sqlite(engine) else f"{PIPELINE_SCHEMA}.{name}"


def ensure_pipeline_tables(engine) -> None:
    runs_table = _table(engine, "runs")
    steps_table = _table(engine, "steps")
    artifacts_table = _table(engine, "artifacts")

    with engine.begin() as conn:
        if not _is_sqlite(engine):
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {PIPELINE_SCHEMA}"))

        conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {runs_table} (
            run_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            batch_file TEXT NOT NULL,
            groq_output_file TEXT,
            normalized_file TEXT,
            graph_db TEXT,
            status TEXT NOT NULL,
            current_step TEXT,
            error_text TEXT,
            started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMPTZ
        )
        """))

        conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {steps_table} (
            run_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            status TEXT NOT NULL,
            local_path TEXT,
            storage_bucket TEXT,
            storage_key TEXT,
            error_text TEXT,
            started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMPTZ,
            PRIMARY KEY (run_id, step_name)
        )
        """))

        conn.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {artifacts_table} (
            artifact_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            step_name TEXT NOT NULL,
            artifact_kind TEXT NOT NULL,
            file_name TEXT NOT NULL,
            local_path TEXT,
            storage_bucket TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            content_type TEXT,
            size_bytes BIGINT,
            sha256 TEXT,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """))


def create_run(
    engine,
    *,
    profile_id: str,
    batch_file: str,
    graph_db: str,
) -> str:
    run_id = str(uuid.uuid4())
    runs_table = _table(engine, "runs")

    with engine.begin() as conn:
        conn.execute(text(f"""
        INSERT INTO {runs_table} (
            run_id,
            profile_id,
            batch_file,
            graph_db,
            status,
            current_step
        )
        VALUES (
            :run_id,
            :profile_id,
            :batch_file,
            :graph_db,
            'running',
            'init'
        )
        """), {
            "run_id": run_id,
            "profile_id": profile_id,
            "batch_file": batch_file,
            "graph_db": graph_db,
        })

    return run_id


def mark_step_start(engine, run_id: str, profile_id: str, step_name: str) -> None:
    runs_table = _table(engine, "runs")
    steps_table = _table(engine, "steps")

    with engine.begin() as conn:
        conn.execute(text(f"""
        INSERT INTO {steps_table} (
            run_id,
            profile_id,
            step_name,
            status,
            started_at,
            finished_at,
            error_text,
            local_path,
            storage_bucket,
            storage_key
        )
        VALUES (
            :run_id,
            :profile_id,
            :step_name,
            'running',
            CURRENT_TIMESTAMP,
            NULL,
            NULL,
            NULL,
            NULL,
            NULL
        )
        ON CONFLICT (run_id, step_name) DO UPDATE SET
            profile_id = EXCLUDED.profile_id,
            status = 'running',
            started_at = CURRENT_TIMESTAMP,
            finished_at = NULL,
            error_text = NULL,
            local_path = NULL,
            storage_bucket = NULL,
            storage_key = NULL
        """), {
            "run_id": run_id,
            "profile_id": profile_id,
            "step_name": step_name,
        })

        conn.execute(text(f"""
        UPDATE {runs_table}
        SET status = 'running',
            current_step = :step_name,
            error_text = NULL
        WHERE run_id = :run_id
        """), {
            "run_id": run_id,
            "step_name": step_name,
        })


def mark_step_done(
    engine,
    run_id: str,
    step_name: str,
    *,
    local_path: str | None = None,
    storage_bucket: str | None = None,
    storage_key: str | None = None,
) -> None:
    steps_table = _table(engine, "steps")

    with engine.begin() as conn:
        conn.execute(text(f"""
        UPDATE {steps_table}
        SET status = 'done',
            local_path = :local_path,
            storage_bucket = :storage_bucket,
            storage_key = :storage_key,
            finished_at = CURRENT_TIMESTAMP
        WHERE run_id = :run_id
          AND step_name = :step_name
        """), {
            "run_id": run_id,
            "step_name": step_name,
            "local_path": local_path,
            "storage_bucket": storage_bucket,
            "storage_key": storage_key,
        })


def register_artifact(
    engine,
    *,
    run_id: str,
    profile_id: str,
    step_name: str,
    artifact_kind: str,
    file_name: str,
    local_path: str,
    storage_bucket: str,
    storage_key: str,
    content_type: str | None,
    size_bytes: int | None,
    sha256: str | None,
) -> str:
    artifact_id = str(uuid.uuid4())
    artifacts_table = _table(engine, "artifacts")

    with engine.begin() as conn:
        conn.execute(text(f"""
        INSERT INTO {artifacts_table} (
            artifact_id,
            run_id,
            profile_id,
            step_name,
            artifact_kind,
            file_name,
            local_path,
            storage_bucket,
            storage_key,
            content_type,
            size_bytes,
            sha256
        )
        VALUES (
            :artifact_id,
            :run_id,
            :profile_id,
            :step_name,
            :artifact_kind,
            :file_name,
            :local_path,
            :storage_bucket,
            :storage_key,
            :content_type,
            :size_bytes,
            :sha256
        )
        """), {
            "artifact_id": artifact_id,
            "run_id": run_id,
            "profile_id": profile_id,
            "step_name": step_name,
            "artifact_kind": artifact_kind,
            "file_name": file_name,
            "local_path": local_path,
            "storage_bucket": storage_bucket,
            "storage_key": storage_key,
            "content_type": content_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
        })

    return artifact_id


def mark_step_failed(
    engine,
    run_id: str,
    step_name: str,
    error_text: str,
) -> None:
    runs_table = _table(engine, "runs")
    steps_table = _table(engine, "steps")
    msg = (error_text or "")[:4000]

    with engine.begin() as conn:
        conn.execute(text(f"""
        UPDATE {steps_table}
        SET status = 'failed',
            error_text = :error_text,
            finished_at = CURRENT_TIMESTAMP
        WHERE run_id = :run_id
          AND step_name = :step_name
        """), {
            "run_id": run_id,
            "step_name": step_name,
            "error_text": msg,
        })

        conn.execute(text(f"""
        UPDATE {runs_table}
        SET status = 'failed',
            current_step = :step_name,
            error_text = :error_text,
            finished_at = CURRENT_TIMESTAMP
        WHERE run_id = :run_id
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
    runs_table = _table(engine, "runs")

    with engine.begin() as conn:
        conn.execute(text(f"""
        UPDATE {runs_table}
        SET status = 'done',
            current_step = 'done',
            groq_output_file = :groq_output_file,
            normalized_file = :normalized_file,
            finished_at = CURRENT_TIMESTAMP
        WHERE run_id = :run_id
        """), {
            "run_id": run_id,
            "groq_output_file": groq_output_file,
            "normalized_file": normalized_file,
        })