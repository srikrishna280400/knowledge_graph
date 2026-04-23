from __future__ import annotations
import os
import time
from dataclasses import dataclass
from pathlib import Path


def _resolve_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if raw and raw.strip():
        return _resolve_path(raw.strip())
    return _resolve_path(default)


BASE_DIR = _env_path("KG_BACKEND_BASE_DIR", Path(__file__).resolve().parents[1])
DATA_DIR = _env_path("KG_DATA_DIR", BASE_DIR / "data")

LOGIC_DIR = BASE_DIR / "logic_files"
COMMAND_DIR = BASE_DIR / "command_files"
VIS_PIPELINE_DIR = BASE_DIR / "vis_pipeline"


@dataclass(frozen=True)
class DatasetTarget:
    name: str
    db_path: Path
    import_txt: Path | None = None


PRIMARY = "primary"
#KG_90 = "kg_90"
GRAPH_STORE = "graph_store"


DATASET_TARGETS: dict[str, DatasetTarget] = {
    PRIMARY: DatasetTarget(
        name=PRIMARY,
        db_path=_env_path("KG_PRIMARY_DB_PATH", DATA_DIR / "kg.sqlite"),
        import_txt=_env_path("KG_PRIMARY_IMPORT_TXT", DATA_DIR / "MASTER_URL_LIST.txt"),
    ),
    # KG_90: DatasetTarget(
    #     name=KG_90,
    #     db_path=_env_path("KG_90_DB_PATH", DATA_DIR / "kg_90.sqlite"),
    #     import_txt=_env_path("KG_90_IMPORT_TXT", DATA_DIR / "MASTER_URL90.txt"),
    # ),
    GRAPH_STORE: DatasetTarget(
        name=GRAPH_STORE,
        db_path=_env_path("KG_GRAPH_STORE_DB_PATH", DATA_DIR / "graph_store.sqlite"),
        import_txt=None,
    ),
}


PRIMARY_BATCH_PREFIX = os.getenv("KG_PRIMARY_BATCH_PREFIX", "groq_batch_all")
#KG90_BATCH_PREFIX = os.getenv("KG90_BATCH_PREFIX", "groq_batch_90")


PRIMARY_GROQ_RUN_INPUT = _env_path(
    "KG_PRIMARY_GROQ_RUN_INPUT",
    DATA_DIR / "groq_batch_20r.jsonl",
)
PRIMARY_GROQ_RUN_OUTPUT = _env_path(
    "KG_PRIMARY_GROQ_RUN_OUTPUT",
    DATA_DIR / "groq_batch_20r_outputs.jsonl",
)
PRIMARY_GROQ_RUN_CSV = _env_path(
    "KG_PRIMARY_GROQ_RUN_CSV",
    DATA_DIR / "groq_batch_20r_outputs.csv",
)

# KG90_GROQ_RUN_INPUT = _env_path(
#     "KG90_GROQ_RUN_INPUT",
#     DATA_DIR / "groq_batch_90.jsonl",
# )
# KG90_GROQ_RUN_OUTPUT = _env_path(
#     "KG90_GROQ_RUN_OUTPUT",
#     DATA_DIR / "groq_batch_90_outputs.jsonl",
# )
# KG90_GROQ_RUN_CSV = _env_path(
#     "KG90_GROQ_RUN_CSV",
#     DATA_DIR / "groq_batch_90_outputs.csv",
# )

# KG90_SEED_OUTPUT_JSONL = _env_path(
#     "KG90_SEED_OUTPUT_JSONL",
#     DATA_DIR / "groq_batch90_outputs.jsonl",
# )

NORMALIZE_DEFAULT_INPUT = PRIMARY_GROQ_RUN_OUTPUT


_obsidian_vault_raw = os.getenv("KG_OBSIDIAN_VAULT_ROOT", "").strip()
OBSIDIAN_VAULT_ROOT = _resolve_path(_obsidian_vault_raw) if _obsidian_vault_raw else None
OBSIDIAN_CANVAS_ROOT = os.getenv("KG_OBSIDIAN_CANVAS_ROOT", "KG_CANVAS")


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def get_dataset(name: str) -> DatasetTarget:
    try:
        return DATASET_TARGETS[name]
    except KeyError as exc:
        valid = ", ".join(DATASET_TARGETS.keys())
        raise ValueError(f"Unknown dataset target: {name}. Valid targets: {valid}") from exc


def get_db_path(name: str = PRIMARY) -> Path:
    return get_dataset(name).db_path

def get_database_url(name: str = PRIMARY) -> str:
    if name == PRIMARY:
        db_path = get_db_path(name)
        return f"sqlite:///{db_path}"

    env_name = f"KG_{name.upper()}_DATABASE_URL"
    raw = os.getenv(env_name, "").strip()
    if raw:
        return raw

    db_path = get_db_path(name)
    return f"sqlite:///{db_path}"


def get_import_txt_path(name: str = PRIMARY) -> Path:
    dataset = get_dataset(name)
    if dataset.import_txt is None:
        raise ValueError(f"Dataset target '{name}' does not define an import TXT path.")
    return dataset.import_txt


def get_batch_prefix(name: str = PRIMARY) -> str:
    if name == PRIMARY:
        return PRIMARY_BATCH_PREFIX
    # if name == KG_90:
    #     return KG90_BATCH_PREFIX
    raise ValueError(f"No batch prefix configured for dataset target: {name}")


def make_timestamped_batch_path(name: str = PRIMARY, ts: int | None = None) -> Path:
    ensure_data_dir()
    ts = int(ts or time.time())
    prefix = get_batch_prefix(name)
    return DATA_DIR / f"{prefix}_{ts}.jsonl"


def get_groq_run_paths(name: str = PRIMARY) -> dict[str, Path]:
    if name != PRIMARY:
        raise ValueError(f"No Groq run path bundle configured for dataset target: {name}")

    output = PRIMARY_GROQ_RUN_OUTPUT
    return {
        "input": PRIMARY_GROQ_RUN_INPUT,
        "output": output,
        "csv": PRIMARY_GROQ_RUN_CSV,
        "incomplete": make_incomplete_output_path(output),
        "normalized": make_normalized_output_path(output),
    }


def make_incomplete_output_path(output_path: str | Path) -> Path:
    p = _resolve_path(output_path)
    return p.with_name(f"{p.stem}.incomplete{p.suffix}")


def make_normalized_output_path(output_path: str | Path) -> Path:
    p = _resolve_path(output_path)
    return p.with_name(f"{p.stem}.normalized.jsonl")
