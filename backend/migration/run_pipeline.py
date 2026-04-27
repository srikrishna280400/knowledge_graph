# run_pipeline.py

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from dotenv import load_dotenv, dotenv_values
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.pool import NullPool
from .config import GRAPH_STORE, PRIMARY, get_db_path, make_timestamped_batch_path
from .db_factory import build_sqlite_url, get_primary_mirror_engine
from .pipeline_state import (
    create_run,
    ensure_pipeline_tables,
    mark_run_done,
    mark_step_done,
    mark_step_failed,
    mark_step_start,
    register_artifact,
)

BASE_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = BASE_DIR / ".env"


def load_local_env() -> dict[str, str]:
    parsed: dict[str, str] = {}
    if ENV_PATH.exists():
        raw = dotenv_values(ENV_PATH)
        for k, v in raw.items():
            if k and v is not None:
                parsed[k] = str(v).strip()
        load_dotenv(dotenv_path=ENV_PATH, override=True, encoding="utf-8")
        for k, v in parsed.items():
            os.environ[k] = v
    return parsed


LOCAL_ENV = load_local_env()

RUN_LIMIT = 2

PIPELINE_REDDIT_CRAWL_LIMIT = 1
PIPELINE_LINKEDIN_CRAWL_LIMIT = 0
PIPELINE_X_CRAWL_LIMIT = 0
PIPELINE_GENERIC_CRAWL_LIMIT = 1

RETRYABLE_STATUSES = ("pending_crawl", "crawl_failed", "extraction_failed")
POST_CRAWL_STATUSES = ("crawled", "crawled_wayback", "title_only")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--profile-id", default="", help="app.profiles.id")
    p.add_argument("--batch-out", default="", help="Batch JSONL path to create")
    p.add_argument("--db", default="", help="Graph ingest target")
    p.add_argument("--pipeline-db-url", default="", help="Postgres URL override")
    p.add_argument("--storage-bucket", default="pipeline-intermediate")
    p.add_argument("--storage-prefix", default="runs")
    p.add_argument("--run-limit", type=int, default=RUN_LIMIT)
    p.add_argument("--reddit-limit", type=int, default=None)
    p.add_argument("--linkedin-limit", type=int, default=None)
    p.add_argument("--x-limit", type=int, default=None)
    p.add_argument("--generic-limit", type=int, default=None)
    return p.parse_args()


def resolve_path(value: str, base_dir: Path) -> Path:
    p = Path(value).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def get_pipeline_db_url(cli_value: str) -> str:
    candidates = [
        (cli_value or "").strip(),
        (os.getenv("PIPELINE_DATABASE_URL") or "").strip(),
        (os.getenv("DATABASE_URL") or "").strip(),
    ]
    for value in candidates:
        if value:
            return value
    raise RuntimeError(
        "Missing pipeline Postgres URL. Set --pipeline-db-url or PIPELINE_DATABASE_URL "
        "or DATABASE_URL"
    )


def get_pipeline_engine(db_url: str):
    return create_engine(
        db_url,
        future=True,
        pool_pre_ping=True,
    )


def get_supabase_storage_config() -> tuple[str, str]:
    supabase_url = (os.getenv("SUPABASE_URL") or "").strip()
    service_role_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()

    missing = []
    if not supabase_url.startswith("https://"):
        raise RuntimeError(
            f"SUPABASE_URL must be your https Supabase project URL, got: {supabase_url!r}"
            )
        
    if "postgresql://" in supabase_url or "postgres://" in supabase_url:
        raise RuntimeError("SUPABASE_URL is pointing to a database URL, not the Supabase HTTP URL")
    
    if not service_role_key:
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    
    if missing:
        raise RuntimeError(
            f"Missing required Storage env var(s): {', '.join(missing)}"
        )

    return supabase_url.rstrip("/"), service_role_key


def run_cmd(cmd: list[str]) -> None:
    print("\n>", " ".join(cmd))
    subprocess.run(cmd, check=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def guess_content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def upload_file_to_supabase_storage(
    local_path: Path,
    *,
    bucket: str,
    object_key: str,
) -> None:
    supabase_url, service_role_key = get_supabase_storage_config()
    object_url = (
        f"{supabase_url}/storage/v1/object/"
        f"{quote(bucket, safe='')}/{quote(object_key, safe='/')}"
    )

    req = Request(
        object_url,
        data=local_path.read_bytes(),
        method="POST",
        headers={
            "Authorization": f"Bearer {service_role_key}",
            "apikey": service_role_key,
            "x-upsert": "true",
            "Content-Type": guess_content_type(local_path),
        },
    )

    try:
        with urlopen(req) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"Storage upload failed: HTTP {resp.status}")
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Storage upload failed for {local_path.name}: HTTP {e.code} {body}"
        ) from e


def persist_intermediate_artifact(
    *,
    engine,
    run_id: str,
    profile_id: str,
    step_name: str,
    artifact_kind: str,
    path: Path,
    bucket: str,
    storage_prefix: str,
) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Expected artifact file missing: {path}")

    storage_key = (
        f"{storage_prefix.strip('/')}/{profile_id}/{run_id}/{step_name}/{path.name}"
    )

    upload_file_to_supabase_storage(
        path,
        bucket=bucket,
        object_key=storage_key,
    )

    register_artifact(
        engine,
        run_id=run_id,
        profile_id=profile_id,
        step_name=step_name,
        artifact_kind=artifact_kind,
        file_name=path.name,
        local_path=str(path),
        storage_bucket=bucket,
        storage_key=storage_key,
        content_type=guess_content_type(path),
        size_bytes=path.stat().st_size,
        sha256=sha256_file(path),
    )

    return storage_key


def get_primary_sqlite_engine():
    db_path = get_db_path(PRIMARY).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run_pipeline] local sqlite = {db_path}")
    return create_engine(
        build_sqlite_url(db_path),
        future=True,
        pool_pre_ping=True,
        poolclass=NullPool,
    )


def refresh_primary_sqlite_engine(engine):
    try:
        engine.dispose()
    except Exception:
        pass
    return get_primary_sqlite_engine()


def jsonl_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def jsonl_ids(path: Path) -> list[str]:
    return [str(r.get("id")) for r in jsonl_rows(path) if r.get("id")]


def groq_success_ids(path: Path) -> list[str]:
    good: list[str] = []
    for r in jsonl_rows(path):
        sid = r.get("id")
        if sid and r.get("http_status") and int(r["http_status"]) < 400 and r.get("parsed") is not None:
            good.append(str(sid))
    return good


def delete_file_if_exists(path: Path) -> None:
    if path.exists():
        path.unlink()


def _expanding_text(sql: str, param_name: str):
    return text(sql).bindparams(bindparam(param_name, expanding=True))


def fetch_saved_items_snapshot(engine) -> dict[str, dict]:
    table = "saved_items" if engine.dialect.name == "sqlite" else "app.saved_items"
    stmt = _expanding_text(
        f"""
        SELECT id, canonical_url, title, extracted_text, status, summary
        FROM {table}
        WHERE status IN :statuses
        """,
        "statuses",
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt, {"statuses": list(RETRYABLE_STATUSES)}).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def fetch_saved_items_by_ids(engine, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    table = "saved_items" if engine.dialect.name == "sqlite" else "app.saved_items"
    stmt = _expanding_text(
        f"""
        SELECT id, canonical_url, title, extracted_text, status, summary
        FROM {table}
        WHERE id IN :ids
        """,
        "ids",
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt, {"ids": ids}).mappings().all()
    return {r["id"]: dict(r) for r in rows}


def detect_crawl_touched_ids(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    touched: list[str] = []
    for sid, old in before.items():
        new = after.get(sid)
        if not new:
            continue
        if any(
            new.get(k) != old.get(k)
            for k in ("canonical_url", "title", "extracted_text", "status", "summary")
        ):
            touched.append(sid)
    return touched


def restore_saved_items(engine, snapshot: dict[str, dict], ids: list[str]) -> None:
    if not ids:
        return
    table = "saved_items" if engine.dialect.name == "sqlite" else "app.saved_items"
    with engine.begin() as conn:
        for sid in ids:
            row = snapshot.get(sid)
            if not row:
                continue
            conn.execute(
                text(
                    f"""
                    UPDATE {table}
                    SET canonical_url=:canonical_url,
                        title=:title,
                        extracted_text=:extracted_text,
                        status=:status,
                        summary=:summary
                    WHERE id=:id
                    """
                ),
                row,
            )


def fetch_llm_batched_ids(engine, batch_file: str) -> set[str]:
    table = "llm_batched" if engine.dialect.name == "sqlite" else "app.llm_batched"
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT saved_item_id FROM {table} WHERE batch_file = :bf"),
            {"bf": batch_file},
        ).fetchall()
    return {r[0] for r in rows}


def delete_llm_batched_ids(engine, ids: list[str]) -> None:
    if not ids:
        return
    table = "llm_batched" if engine.dialect.name == "sqlite" else "app.llm_batched"
    stmt = _expanding_text(
        f"DELETE FROM {table} WHERE saved_item_id IN :ids",
        "ids",
    )
    with engine.begin() as conn:
        conn.execute(stmt, {"ids": ids})


def fetch_llm_output_ids(engine, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    table = "llm_outputs" if engine.dialect.name == "sqlite" else "public.llm_outputs"
    stmt = _expanding_text(
        f"SELECT saved_item_id FROM {table} WHERE saved_item_id IN :ids",
        "ids",
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt, {"ids": ids}).fetchall()
    return {r[0] for r in rows}


def fetch_llm_processed_ids(engine, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    table = "llm_processed" if engine.dialect.name == "sqlite" else "public.llm_processed"
    stmt = _expanding_text(
        f"SELECT saved_item_id FROM {table} WHERE saved_item_id IN :ids",
        "ids",
    )
    with engine.begin() as conn:
        rows = conn.execute(stmt, {"ids": ids}).fetchall()
    return {r[0] for r in rows}


def delete_llm_output_ids(engine, ids: list[str]) -> None:
    if not ids:
        return
    outputs = "llm_outputs" if engine.dialect.name == "sqlite" else "public.llm_outputs"
    processed = "llm_processed" if engine.dialect.name == "sqlite" else "public.llm_processed"
    stmt_processed = _expanding_text(
        f"DELETE FROM {processed} WHERE saved_item_id IN :ids",
        "ids",
    )
    stmt_outputs = _expanding_text(
        f"DELETE FROM {outputs} WHERE saved_item_id IN :ids",
        "ids",
    )
    with engine.begin() as conn:
        conn.execute(stmt_processed, {"ids": ids})
        conn.execute(stmt_outputs, {"ids": ids})


def normalized_manifest(path: Path) -> dict:
    rows = jsonl_rows(path)
    source_ids: list[str] = []
    concept_ids: set[str] = set()

    for obj in rows:
        sn = obj.get("source_node") or {}
        sid = sn.get("id")
        if sid:
            source_ids.append(str(sid))
        for cn in (obj.get("concept_nodes") or []):
            cid = cn.get("id")
            if cid:
                concept_ids.add(str(cid))

    return {
        "source_ids": source_ids,
        "concept_ids": sorted(concept_ids),
        "batch_id": path.stem,
    }


def fetch_graph_presence_sqlite(db_path: Path, source_ids: list[str], concept_ids: list[str]) -> dict:
    if not db_path.exists():
        return {
            "source_nodes": set(),
            "concept_nodes": set(),
            "source_embeddings": set(),
            "concept_embeddings": set(),
            "concept_stats": set(),
        }

    con = sqlite3.connect(db_path)
    try:
        out: dict[str, set[str]] = {}
        if source_ids:
            q = ",".join("?" * len(source_ids))
            out["source_nodes"] = {
                r[0] for r in con.execute(
                    f"SELECT node_id FROM source_nodes WHERE node_id IN ({q})",
                    source_ids,
                )
            }
            out["source_embeddings"] = {
                r[0] for r in con.execute(
                    f"SELECT node_id FROM source_embeddings WHERE node_id IN ({q})",
                    source_ids,
                )
            }
        else:
            out["source_nodes"] = set()
            out["source_embeddings"] = set()

        if concept_ids:
            q = ",".join("?" * len(concept_ids))
            out["concept_nodes"] = {
                r[0] for r in con.execute(
                    f"SELECT node_id FROM concept_nodes WHERE node_id IN ({q})",
                    concept_ids,
                )
            }
            out["concept_embeddings"] = {
                r[0] for r in con.execute(
                    f"SELECT node_id FROM concept_embeddings WHERE node_id IN ({q})",
                    concept_ids,
                )
            }
            out["concept_stats"] = {
                r[0] for r in con.execute(
                    f"SELECT concept_node_id FROM concept_stats WHERE concept_node_id IN ({q})",
                    concept_ids,
                )
            }
        else:
            out["concept_nodes"] = set()
            out["concept_embeddings"] = set()
            out["concept_stats"] = set()

        return out
    finally:
        con.close()


def fetch_graph_batch_counts_sqlite(db_path: Path, batch_id: str) -> dict:
    if not db_path.exists():
        return {
            "source_payloads": 0,
            "source_concept_edges": 0,
            "concept_edges": 0,
            "source_edges": 0,
        }

    con = sqlite3.connect(db_path)
    try:
        return {
            "source_payloads": con.execute(
                "SELECT COUNT(*) FROM source_payloads WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0],
            "source_concept_edges": con.execute(
                "SELECT COUNT(*) FROM source_concept_edges WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0],
            "concept_edges": con.execute(
                "SELECT COUNT(*) FROM concept_edges WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0],
            "source_edges": con.execute(
                "SELECT COUNT(*) FROM source_edges WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()[0],
        }
    finally:
        con.close()


def fetch_graph_presence_pg(pg_engine, schema: str, source_ids: list[str], concept_ids: list[str]) -> dict:
    out = {
        "source_nodes": set(),
        "concept_nodes": set(),
        "source_embeddings": set(),
        "concept_embeddings": set(),
        "concept_stats": set(),
    }
    if pg_engine is None:
        return out

    with pg_engine.begin() as conn:
        if source_ids:
            stmt = _expanding_text(
                f"SELECT node_id FROM {schema}.source_nodes WHERE node_id IN :ids",
                "ids",
            )
            out["source_nodes"] = {r[0] for r in conn.execute(stmt, {"ids": source_ids})}

            stmt = _expanding_text(
                f"SELECT node_id FROM {schema}.source_embeddings WHERE node_id IN :ids",
                "ids",
            )
            out["source_embeddings"] = {r[0] for r in conn.execute(stmt, {"ids": source_ids})}

        if concept_ids:
            stmt = _expanding_text(
                f"SELECT node_id FROM {schema}.concept_nodes WHERE node_id IN :ids",
                "ids",
            )
            out["concept_nodes"] = {r[0] for r in conn.execute(stmt, {"ids": concept_ids})}

            stmt = _expanding_text(
                f"SELECT node_id FROM {schema}.concept_embeddings WHERE node_id IN :ids",
                "ids",
            )
            out["concept_embeddings"] = {r[0] for r in conn.execute(stmt, {"ids": concept_ids})}

            stmt = _expanding_text(
                f"SELECT concept_node_id FROM {schema}.concept_stats WHERE concept_node_id IN :ids",
                "ids",
            )
            out["concept_stats"] = {r[0] for r in conn.execute(stmt, {"ids": concept_ids})}

    return out


def fetch_graph_batch_counts_pg(pg_engine, schema: str, batch_id: str) -> dict:
    with pg_engine.begin() as conn:
        return {
            "source_payloads": conn.execute(
                text(f"SELECT COUNT(*) FROM {schema}.source_payloads WHERE batch_id=:b"),
                {"b": batch_id},
            ).scalar_one(),
            "source_concept_edges": conn.execute(
                text(f"SELECT COUNT(*) FROM {schema}.source_concept_edges WHERE batch_id=:b"),
                {"b": batch_id},
            ).scalar_one(),
            "concept_edges": conn.execute(
                text(f"SELECT COUNT(*) FROM {schema}.concept_edges WHERE batch_id=:b"),
                {"b": batch_id},
            ).scalar_one(),
            "source_edges": conn.execute(
                text(f"SELECT COUNT(*) FROM {schema}.source_edges WHERE batch_id=:b"),
                {"b": batch_id},
            ).scalar_one(),
        }

def get_embedding_model(model_name: str):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(model_name)

def rollback_graph_sqlite_from_backup(target_db: Path, backup_db: Path) -> None:
    if backup_db.exists():
        shutil.copy2(backup_db, target_db)
    elif target_db.exists():
        target_db.unlink()


def rollback_graph_pg(pg_engine, schema: str, batch_id: str, source_ids: list[str], concept_ids: list[str], before: dict) -> None:
    if pg_engine is None:
        return

    with pg_engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {schema}.source_payloads WHERE batch_id=:b"), {"b": batch_id})
        conn.execute(text(f"DELETE FROM {schema}.source_concept_edges WHERE batch_id=:b"), {"b": batch_id})
        conn.execute(text(f"DELETE FROM {schema}.concept_edges WHERE batch_id=:b"), {"b": batch_id})
        conn.execute(text(f"DELETE FROM {schema}.source_edges WHERE batch_id=:b"), {"b": batch_id})

        new_source_nodes = [x for x in source_ids if x not in before["source_nodes"]]
        new_concept_nodes = [x for x in concept_ids if x not in before["concept_nodes"]]
        new_source_emb = [x for x in source_ids if x not in before["source_embeddings"]]
        new_concept_emb = [x for x in concept_ids if x not in before["concept_embeddings"]]
        new_concept_stats = [x for x in concept_ids if x not in before["concept_stats"]]

        if new_source_emb:
            stmt = _expanding_text(
                f"DELETE FROM {schema}.source_embeddings WHERE node_id IN :ids",
                "ids",
            )
            conn.execute(stmt, {"ids": new_source_emb})

        if new_concept_emb:
            stmt = _expanding_text(
                f"DELETE FROM {schema}.concept_embeddings WHERE node_id IN :ids",
                "ids",
            )
            conn.execute(stmt, {"ids": new_concept_emb})

        if new_concept_stats:
            stmt = _expanding_text(
                f"DELETE FROM {schema}.concept_stats WHERE concept_node_id IN :ids",
                "ids",
            )
            conn.execute(stmt, {"ids": new_concept_stats})

        if new_source_nodes:
            stmt = _expanding_text(
                f"DELETE FROM {schema}.source_nodes WHERE node_id IN :ids",
                "ids",
            )
            conn.execute(stmt, {"ids": new_source_nodes})

        if new_concept_nodes:
            stmt = _expanding_text(
                f"DELETE FROM {schema}.concept_nodes WHERE node_id IN :ids",
                "ids",
            )
            conn.execute(stmt, {"ids": new_concept_nodes})

def resolved_cap(cli_value: int | None, default_cap: int) -> int:
    return cli_value if cli_value is not None else default_cap


def resolve_single_profile_id(engine) -> str:
    table = "profiles" if engine.dialect.name == "sqlite" else "app.profiles"
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT id FROM {table} ORDER BY created_at ASC LIMIT 2")
        ).fetchall()
    if not rows:
        raise RuntimeError(f"No profiles found in {table}")
    if len(rows) != 1:
        raise RuntimeError(
            f"Expected exactly one profile in {table}, found {len(rows)}"
        )
    return str(rows[0][0])


def main() -> None:
    args = parse_args()
    
    pipeline_reddit_limit = resolved_cap(args.reddit_limit, PIPELINE_REDDIT_CRAWL_LIMIT)
    pipeline_linkedin_limit = resolved_cap(args.linkedin_limit, PIPELINE_LINKEDIN_CRAWL_LIMIT)
    pipeline_x_limit = resolved_cap(args.x_limit, PIPELINE_X_CRAWL_LIMIT)
    pipeline_generic_limit = resolved_cap(args.generic_limit, PIPELINE_GENERIC_CRAWL_LIMIT)
    
    pipeline_batch_limit = (
        pipeline_reddit_limit
        + pipeline_linkedin_limit
        + pipeline_x_limit
        + pipeline_generic_limit
        )
    
    if pipeline_batch_limit <= 0:
        raise ValueError("pipeline_batch_limit must be > 0")

    run_limit = args.run_limit
    if run_limit <= 0:
        raise ValueError("--run-limit must be > 0")
    
    project_root = Path(__file__).resolve().parents[1]

    profile_id = args.profile_id.strip() or resolve_single_profile_id(get_primary_sqlite_engine())

    batch_out_value = (args.batch_out or "").strip()
    if batch_out_value:
        batch_out = resolve_path(batch_out_value, project_root)
    else:
        batch_out = make_timestamped_batch_path(PRIMARY)

    db_value = (args.db or "").strip()
    if db_value:
        graph_db_target = resolve_path(db_value, project_root)
    else:
        graph_db_target = get_db_path(GRAPH_STORE)

    graph_db_target.parent.mkdir(parents=True, exist_ok=True)
    batch_out.parent.mkdir(parents=True, exist_ok=True)

    groq_out = batch_out.with_name(f"{batch_out.stem}_outputs.jsonl")
    groq_incomplete_out = batch_out.with_name(f"{batch_out.stem}_outputs_incomplete.jsonl")
    groq_csv_out = batch_out.with_name(f"{batch_out.stem}_outputs.csv")
    normalized_out = batch_out.with_name(f"{batch_out.stem}_outputs.normalized.jsonl")

    pipeline_db_url = get_pipeline_db_url(args.pipeline_db_url)
    engine = get_pipeline_engine(pipeline_db_url)

    app_local_engine = get_primary_sqlite_engine()
    mirror_engine = get_primary_mirror_engine()
    app_mirror_engine = mirror_engine
    graph_pg_engine = mirror_engine
    graph_pg_schema = (os.getenv("GRAPH_PG_SCHEMA") or "graph").strip() or "graph"

    if app_mirror_engine is None:
        raise RuntimeError("Supabase mirror DB is required for runner acceptance checks")
    if graph_pg_engine is None:
        raise RuntimeError("Supabase graph mirror DB is required for ingest acceptance checks")

    ensure_pipeline_tables(engine)

    run_id = create_run(
        engine,
        profile_id=profile_id,
        batch_file=str(batch_out),
        graph_db=str(graph_db_target),
    )

    current_step = "init"

    try:
        current_step = "crawl_all"
        mark_step_start(engine, run_id, profile_id, current_step)

        crawl_before_local = fetch_saved_items_snapshot(app_local_engine)
        crawl_before_mirror = fetch_saved_items_snapshot(app_mirror_engine)

        pipeline_reddit_limit = resolved_cap(args.reddit_limit, PIPELINE_REDDIT_CRAWL_LIMIT)
        pipeline_linkedin_limit = resolved_cap(args.linkedin_limit, PIPELINE_LINKEDIN_CRAWL_LIMIT)
        pipeline_x_limit = resolved_cap(args.x_limit, PIPELINE_X_CRAWL_LIMIT)
        pipeline_generic_limit = resolved_cap(args.generic_limit, PIPELINE_GENERIC_CRAWL_LIMIT)

        run_cmd([
            sys.executable, "-m", "app.crawl_all",
            "--limit", str(run_limit),
            "--reddit-limit", str(pipeline_reddit_limit),
            "--linkedin-limit", str(pipeline_linkedin_limit),
            "--x-limit", str(pipeline_x_limit),
            "--generic-limit", str(pipeline_generic_limit),
        ])
        app_local_engine = refresh_primary_sqlite_engine(app_local_engine)

        crawl_after_local = fetch_saved_items_by_ids(
            app_local_engine,
            list(crawl_before_local.keys()),
        )
        touched_ids = detect_crawl_touched_ids(crawl_before_local, crawl_after_local)

        crawl_after_mirror = fetch_saved_items_by_ids(app_mirror_engine, touched_ids)
        bad = [
            sid for sid in touched_ids
            if crawl_after_mirror.get(sid, {}).get("status") not in POST_CRAWL_STATUSES
        ]

        if bad:
            restore_saved_items(app_local_engine, crawl_before_local, touched_ids)
            restore_saved_items(app_mirror_engine, crawl_before_mirror, touched_ids)
            raise RuntimeError(
                f"crawl verification failed in mirror; reverted {len(touched_ids)} rows"
            )

        mark_step_done(engine, run_id, current_step)

        current_step = "make_batch_all"
        mark_step_start(engine, run_id, profile_id, current_step)

        run_cmd([
            sys.executable, "-m", "app.make_batch_all",
            "--out", str(batch_out),
            "--limit", str(pipeline_batch_limit),
        ])
        app_local_engine = refresh_primary_sqlite_engine(app_local_engine)

        batch_ids = jsonl_ids(batch_out)
        if not batch_ids:
            raise RuntimeError("make_batch_all produced no batch ids")

        mirror_batched = fetch_llm_batched_ids(app_mirror_engine, str(batch_out))

        try:
            if set(batch_ids) - mirror_batched:
                raise RuntimeError("missing mirror llm_batched rows after make_batch_all")

            batch_storage_key = persist_intermediate_artifact(
                engine=engine,
                run_id=run_id,
                profile_id=profile_id,
                step_name=current_step,
                artifact_kind="batch_jsonl",
                path=batch_out,
                bucket=args.storage_bucket,
                storage_prefix=args.storage_prefix,
            )
        except Exception:
            delete_llm_batched_ids(app_local_engine, batch_ids)
            delete_llm_batched_ids(app_mirror_engine, batch_ids)
            delete_file_if_exists(batch_out)
            raise

        mark_step_done(
            engine,
            run_id,
            current_step,
            local_path=str(batch_out),
            storage_bucket=args.storage_bucket,
            storage_key=batch_storage_key,
        )

        current_step = "run_groq_all"
        mark_step_start(engine, run_id, profile_id, current_step)

        run_cmd([
            sys.executable, "-m", "app.run_groq_all",
            "--in", str(batch_out),
        ])
        app_local_engine = refresh_primary_sqlite_engine(app_local_engine)

        groq_ids = jsonl_ids(groq_out)
        groq_good_ids = groq_success_ids(groq_out)

        try:
            mirror_outputs = fetch_llm_output_ids(app_mirror_engine, groq_ids)
            mirror_processed = fetch_llm_processed_ids(app_mirror_engine, groq_good_ids)

            if set(groq_ids) - mirror_outputs:
                raise RuntimeError("missing mirror llm_outputs rows after run_groq_all")
            if set(groq_good_ids) - mirror_processed:
                raise RuntimeError("missing mirror llm_processed rows after run_groq_all")

            groq_storage_key = persist_intermediate_artifact(
                engine=engine,
                run_id=run_id,
                profile_id=profile_id,
                step_name=current_step,
                artifact_kind="groq_output_jsonl",
                path=groq_out,
                bucket=args.storage_bucket,
                storage_prefix=args.storage_prefix,
            )
        except Exception:
            delete_llm_output_ids(app_local_engine, groq_ids)
            delete_llm_output_ids(app_mirror_engine, groq_ids)
            delete_file_if_exists(groq_out)
            delete_file_if_exists(groq_incomplete_out)
            delete_file_if_exists(groq_csv_out)
            raise

        mark_step_done(
            engine,
            run_id,
            current_step,
            local_path=str(groq_out),
            storage_bucket=args.storage_bucket,
            storage_key=groq_storage_key,
        )

        current_step = "normalize_groq_output"
        mark_step_start(engine, run_id, profile_id, current_step)

        run_cmd([
            sys.executable, "-m", "app.normalize_groq_output",
            "--in", str(groq_out),
        ])

        try:
            normalized_storage_key = persist_intermediate_artifact(
                engine=engine,
                run_id=run_id,
                profile_id=profile_id,
                step_name=current_step,
                artifact_kind="normalized_jsonl",
                path=normalized_out,
                bucket=args.storage_bucket,
                storage_prefix=args.storage_prefix,
            )
        except Exception:
            delete_file_if_exists(normalized_out)
            raise

        mark_step_done(
            engine,
            run_id,
            current_step,
            local_path=str(normalized_out),
            storage_bucket=args.storage_bucket,
            storage_key=normalized_storage_key,
        )

        current_step = "graph_ingest_incremental"
        mark_step_start(engine, run_id, profile_id, current_step)

        manifest = normalized_manifest(normalized_out)
        batch_id = manifest["batch_id"]
        source_ids = manifest["source_ids"]
        concept_ids = manifest["concept_ids"]

        graph_backup = graph_db_target.with_suffix(graph_db_target.suffix + ".bak")
        if graph_db_target.exists():
            shutil.copy2(graph_db_target, graph_backup)

        graph_before_pg = fetch_graph_presence_pg(
            graph_pg_engine,
            graph_pg_schema,
            source_ids,
            concept_ids,
        )

        run_cmd([
            sys.executable, "-m", "app.graph_ingest_incremental",
            "--in", str(normalized_out),
            "--db", str(graph_db_target),
        ])

        try:
            pg_counts = fetch_graph_batch_counts_pg(
                graph_pg_engine,
                graph_pg_schema,
                batch_id,
            )
            pg_presence = fetch_graph_presence_pg(
                graph_pg_engine,
                graph_pg_schema,
                source_ids,
                concept_ids,
            )

            if pg_counts["source_payloads"] < len(source_ids):
                raise RuntimeError(
                    "mirror graph ingest verification failed: source_payloads shortfall"
                )
            if set(source_ids) - pg_presence["source_nodes"]:
                raise RuntimeError(
                    "mirror graph ingest verification failed: missing source_nodes"
                )
            if concept_ids and (set(concept_ids) - pg_presence["concept_nodes"]):
                raise RuntimeError(
                    "mirror graph ingest verification failed: missing concept_nodes"
                )

        except Exception:
            rollback_graph_sqlite_from_backup(graph_db_target, graph_backup)
            rollback_graph_pg(
                graph_pg_engine,
                graph_pg_schema,
                batch_id,
                source_ids,
                concept_ids,
                graph_before_pg,
            )
            raise
        finally:
            if graph_backup.exists():
                graph_backup.unlink()

        mark_step_done(
            engine,
            run_id,
            current_step,
            local_path=str(graph_db_target),
        )

        mark_run_done(
            engine,
            run_id,
            groq_output_file=str(groq_out),
            normalized_file=str(normalized_out),
        )

        print("\nDONE")
        print(f"run_id={run_id}")
        print(f"profile_id={profile_id}")
        print(f"Batch file: {batch_out}")
        print(f"Groq output: {groq_out}")
        print(f"Normalized: {normalized_out}")
        print(f"Graph target: {graph_db_target}")

    except Exception as e:
        mark_step_failed(engine, run_id, current_step, repr(e))
        raise

if __name__ == "__main__":
    main()
