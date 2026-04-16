import argparse
import subprocess
import sys
from pathlib import Path
from .config import PRIMARY
from .db_factory import get_engine

from .pipeline_state import (
    ensure_pipeline_tables,
    create_run,
    mark_step_start,
    mark_step_done,
    mark_step_failed,
    mark_run_done,
)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--batch-out", required=True, help="Batch JSONL path to create")
    p.add_argument("--db", required=True, help="Graph store sqlite path")
    p.add_argument("--vault", required=True, help="Obsidian vault path")
    p.add_argument("--root", default=".", help="Folder inside vault for generated output")
    return p.parse_args()


def run_cmd(cmd: list[str]) -> None:
    print("\n>", " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> None:
    args = parse_args()

    project_root = Path(__file__).resolve().parents[1]

    batch_out = Path(args.batch_out)
    if not batch_out.is_absolute():
        batch_out = project_root / batch_out

    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = project_root / db_path

    vault_path = Path(args.vault)
    root_folder = args.root

    groq_out = batch_out.with_name(f"{batch_out.stem}_outputs.jsonl")
    normalized_out = batch_out.with_name(f"{batch_out.stem}_outputs.normalized.jsonl")

    batch_out.parent.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine(PRIMARY)
    ensure_pipeline_tables(engine)

    run_id = create_run(
        engine,
        batch_file=str(batch_out),
        graph_db=str(db_path),
        vault_path=str(vault_path),
        root_folder=str(root_folder),
    )

    current_step = "init"

    try:
        current_step = "make_batch_all"
        mark_step_start(engine, run_id, current_step)
        run_cmd([
            sys.executable, "-m", "app.make_batch_all",
            "--out", str(batch_out),
        ])
        mark_step_done(engine, run_id, current_step, str(batch_out))

        current_step = "run_groq_all"
        mark_step_start(engine, run_id, current_step)
        run_cmd([
            sys.executable, "-m", "app.run_groq_all",
            "--in", str(batch_out),
        ])
        mark_step_done(engine, run_id, current_step, str(groq_out))

        current_step = "normalize_groq_output"
        mark_step_start(engine, run_id, current_step)
        run_cmd([
            sys.executable, "-m", "app.normalize_groq_output",
            "--in", str(groq_out),
        ])
        mark_step_done(engine, run_id, current_step, str(normalized_out))

        current_step = "graph_ingest_incremental"
        mark_step_start(engine, run_id, current_step)
        run_cmd([
            sys.executable, "-m", "app.graph_ingest_incremental",
            "--in", str(normalized_out),
            "--db", str(db_path),
        ])
        mark_step_done(engine, run_id, current_step, str(db_path))

        current_step = "generate_visual_boards"
        mark_step_start(engine, run_id, current_step)
        run_cmd([
            sys.executable, "-m", "vis_pipeline.generate_visual_boards",
            "--db", str(db_path),
            "--vault", str(vault_path),
            "--root", str(root_folder),
        ])
        mark_step_done(engine, run_id, current_step, str(vault_path))

        mark_run_done(
            engine,
            run_id,
            groq_output_file=str(groq_out),
            normalized_file=str(normalized_out),
        )

        print("\nDONE")
        print(f"run_id={run_id}")
        print(f"Batch file: {batch_out}")
        print(f"Groq output: {groq_out}")
        print(f"Normalized: {normalized_out}")
        print(f"Graph DB: {db_path}")
        print(f"Vault: {vault_path}")

    except Exception as e:
        mark_step_failed(engine, run_id, current_step, repr(e))
        raise


if __name__ == "__main__":
    main()
