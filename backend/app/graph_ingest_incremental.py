# graph_ingest_incremental.py

import os
import re
import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any
import numpy as np
from dotenv import load_dotenv
from sentence_transformers import SentenceTransformer
from sqlalchemy import text
from sqlalchemy.engine import Engine
from migration.db_factory import get_primary_mirror_engine

load_dotenv()

MODEL_NAME_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K_DEFAULT = 8
MIN_SIM_DEFAULT = 0.55
PG_SCHEMA = os.getenv("GRAPH_PG_SCHEMA", "graph").strip() or "graph"


def _safe_list(x):
    return x if isinstance(x, list) else []


def _safe_schema_name(schema: str) -> str:
    schema = (schema or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise ValueError(f"Invalid PostgreSQL schema name: {schema!r}")
    return schema


def build_source_embedding_text(obj: dict) -> str:
    source_node = obj.get("source_node") or {}
    cm = obj.get("cognitive_map") or {}
    root = cm.get("root") if isinstance(cm, dict) else {}

    domains = _safe_list(cm.get("domains")) if isinstance(cm, dict) else []
    themes = _safe_list(cm.get("themes")) if isinstance(cm, dict) else []
    concepts = _safe_list(cm.get("concepts")) if isinstance(cm, dict) else []
    micro = _safe_list(cm.get("micro_insights")) if isinstance(cm, dict) else []

    parts = [f"URL: {source_node.get('canonical_url') or ''}"]

    if isinstance(root, dict):
        parts.append(f"Label: {root.get('label') or ''}")
        parts.append(f"Need: {root.get('intellectual_need') or ''}")
        parts.append(f"Why: {root.get('why_saved') or ''}")

    if domains:
        parts.append(
            "Domains: " + "; ".join(
                [d.get("name", "") for d in domains if isinstance(d, dict) and d.get("name")]
            )
        )
    if themes:
        parts.append(
            "Themes: " + "; ".join(
                [t.get("name", "") for t in themes if isinstance(t, dict) and t.get("name")]
            )
        )
    if concepts:
        parts.append(
            "Concepts: " + "; ".join(
                [c.get("name", "") for c in concepts if isinstance(c, dict) and c.get("name")]
            )
        )
    if micro:
        parts.append(
            "Micro: " + " | ".join(
                [m.get("text", "") for m in micro if isinstance(m, dict) and m.get("text")]
            )
        )

    return "\n".join([p for p in parts if p.strip()])


def build_concept_embedding_text(concept: dict) -> str:
    return (
        f"Type: {concept.get('type') or ''}\n"
        f"Name: {concept.get('name') or ''}\n"
        f"Tier: {concept.get('tier', '')}"
    )


def l2_normalize(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.clip(n, 1e-12, None)


def ensure_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_nodes (
          node_num INTEGER PRIMARY KEY AUTOINCREMENT,
          node_id TEXT NOT NULL UNIQUE,
          canonical_url TEXT NOT NULL UNIQUE,
          name TEXT,
          created_at TEXT DEFAULT (datetime('now')),
          first_seen_batch TEXT,
          last_seen_batch TEXT
        );
        """
    )
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_source_nodes_node_id ON source_nodes(node_id);")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_source_nodes_url ON source_nodes(canonical_url);")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_nodes (
          node_num INTEGER PRIMARY KEY AUTOINCREMENT,
          node_id TEXT NOT NULL UNIQUE,
          concept_type TEXT NOT NULL,
          name TEXT NOT NULL,
          tier INTEGER,
          created_at TEXT DEFAULT (datetime('now')),
          first_seen_batch TEXT,
          last_seen_batch TEXT
        );
        """
    )
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_concept_nodes_node_id ON concept_nodes(node_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_concept_nodes_type ON concept_nodes(concept_type);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_concept_nodes_tier ON concept_nodes(tier);")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_payloads (
          source_node_id TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          canonical_url TEXT,
          groq_output_json TEXT,
          cognitive_map_json TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, batch_id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_embeddings (
          node_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (node_id, model)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_embeddings (
          node_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (node_id, model)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_concept_edges (
          source_node_id TEXT NOT NULL,
          concept_node_id TEXT NOT NULL,
          confidence REAL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, concept_node_id, batch_id)
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_sc_edges_source ON source_concept_edges(source_node_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_sc_edges_concept ON source_concept_edges(concept_node_id);")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength REAL NOT NULL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, target_node_id, method, batch_id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength REAL NOT NULL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, target_node_id, method, batch_id)
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_stats (
          concept_node_id TEXT PRIMARY KEY,
          doc_freq INTEGER NOT NULL,
          updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_concept_stats_docfreq ON concept_stats(doc_freq);")

    con.commit()


def ensure_pg_graph_schema(pg_engine: Engine, schema: str) -> None:
    schema = _safe_schema_name(schema)
    ddl = [
        f"CREATE SCHEMA IF NOT EXISTS {schema}",

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.source_nodes (
          node_num BIGSERIAL,
          node_id TEXT PRIMARY KEY,
          canonical_url TEXT NOT NULL UNIQUE,
          name TEXT,
          created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          first_seen_batch TEXT,
          last_seen_batch TEXT
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.concept_nodes (
          node_num BIGSERIAL,
          node_id TEXT PRIMARY KEY,
          concept_type TEXT NOT NULL,
          name TEXT NOT NULL,
          tier INTEGER,
          created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          first_seen_batch TEXT,
          last_seen_batch TEXT
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.source_payloads (
          source_node_id TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          canonical_url TEXT,
          groq_output_json TEXT,
          cognitive_map_json TEXT,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (source_node_id, batch_id)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.source_embeddings (
          node_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (node_id, model)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.concept_embeddings (
          node_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (node_id, model)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.source_concept_edges (
          source_node_id TEXT NOT NULL,
          concept_node_id TEXT NOT NULL,
          confidence DOUBLE PRECISION,
          batch_id TEXT,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (source_node_id, concept_node_id, batch_id)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.concept_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength DOUBLE PRECISION NOT NULL,
          batch_id TEXT,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (source_node_id, target_node_id, method, batch_id)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.source_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength DOUBLE PRECISION NOT NULL,
          batch_id TEXT,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (source_node_id, target_node_id, method, batch_id)
        )
        """,

        f"""
        CREATE TABLE IF NOT EXISTS {schema}.concept_stats (
          concept_node_id TEXT PRIMARY KEY,
          doc_freq INTEGER NOT NULL,
          updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
        """,

        f"CREATE INDEX IF NOT EXISTS ix_concept_nodes_type ON {schema}.concept_nodes(concept_type)",
        f"CREATE INDEX IF NOT EXISTS ix_concept_nodes_tier ON {schema}.concept_nodes(tier)",
        f"CREATE INDEX IF NOT EXISTS ix_sc_edges_source ON {schema}.source_concept_edges(source_node_id)",
        f"CREATE INDEX IF NOT EXISTS ix_sc_edges_concept ON {schema}.source_concept_edges(concept_node_id)",
        f"CREATE INDEX IF NOT EXISTS ix_concept_stats_docfreq ON {schema}.concept_stats(doc_freq)",
    ]

    with pg_engine.begin() as pg:
        for sql in ddl:
            pg.execute(text(sql))


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Path to *20r_outputs.normalized.jsonl")
    p.add_argument("--db", dest="db_path", default="data/graph_store.sqlite")
    p.add_argument("--model", dest="model_name", default=MODEL_NAME_DEFAULT)
    p.add_argument("--top-k", dest="top_k", type=int, default=TOP_K_DEFAULT)
    p.add_argument("--min-sim", dest="min_sim", type=float, default=MIN_SIM_DEFAULT)
    p.add_argument("--debug", action="store_true", help="Print edge-build diagnostics")
    return p.parse_args()


def _update_concept_doc_freq(cur: sqlite3.Cursor, concept_ids: list[str]) -> None:
    if not concept_ids:
        return

    placeholders = ",".join(["?"] * len(concept_ids))
    rows = cur.execute(
        f"""
        SELECT concept_node_id, COUNT(DISTINCT source_node_id) AS doc_freq
        FROM source_concept_edges
        WHERE concept_node_id IN ({placeholders})
        GROUP BY concept_node_id
        """,
        concept_ids,
    ).fetchall()

    for concept_node_id, doc_freq in rows:
        cur.execute(
            """
            INSERT INTO concept_stats (concept_node_id, doc_freq, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(concept_node_id) DO UPDATE SET
              doc_freq = excluded.doc_freq,
              updated_at = datetime('now')
            """,
            (concept_node_id, int(doc_freq)),
        )


def _sqlite_rows(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def _sqlite_rows_for_ids(
    con: sqlite3.Connection,
    sql_prefix: str,
    ids: list[str],
    tail_sql: str = "",
    extra_params=(),
) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join(["?"] * len(ids))
    sql = f"{sql_prefix} ({placeholders}) {tail_sql}"
    params = [*ids, *extra_params]
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def mirror_batch_to_postgres(
    con: sqlite3.Connection,
    batch_id: str,
    model_name: str,
    source_ids: list[str],
    concept_ids: list[str],
) -> dict[str, int]:
    counts = {
        "source_nodes": 0,
        "concept_nodes": 0,
        "source_payloads": 0,
        "source_concept_edges": 0,
        "concept_edges": 0,
        "source_edges": 0,
        "source_embeddings": 0,
        "concept_embeddings": 0,
        "concept_stats": 0,
    }

    pg_engine = get_primary_mirror_engine()
    if pg_engine is None:
        print("ⓘ graph mirror disabled: no primary mirror DB configured")
        return counts

    schema = _safe_schema_name(PG_SCHEMA)
    ensure_pg_graph_schema(pg_engine, schema)

    source_ids = sorted(set(source_ids))
    concept_ids = sorted(set(concept_ids))

    source_nodes = _sqlite_rows(
        con,
        """
        SELECT node_id, canonical_url, name, created_at, first_seen_batch, last_seen_batch
        FROM source_nodes
        WHERE last_seen_batch = ?
        """,
        (batch_id,),
    )

    concept_nodes = _sqlite_rows(
        con,
        """
        SELECT node_id, concept_type, name, tier, created_at, first_seen_batch, last_seen_batch
        FROM concept_nodes
        WHERE last_seen_batch = ?
        """,
        (batch_id,),
    )

    source_payloads = _sqlite_rows(
        con,
        """
        SELECT source_node_id, batch_id, canonical_url, groq_output_json, cognitive_map_json, updated_at
        FROM source_payloads
        WHERE batch_id = ?
        """,
        (batch_id,),
    )

    source_concept_edges = _sqlite_rows(
        con,
        """
        SELECT source_node_id, concept_node_id, confidence, batch_id, updated_at
        FROM source_concept_edges
        WHERE batch_id = ?
        """,
        (batch_id,),
    )

    concept_edges = _sqlite_rows(
        con,
        """
        SELECT source_node_id, target_node_id, method, strength, batch_id, updated_at
        FROM concept_edges
        WHERE batch_id = ?
        """,
        (batch_id,),
    )

    source_edges = _sqlite_rows(
        con,
        """
        SELECT source_node_id, target_node_id, method, strength, batch_id, updated_at
        FROM source_edges
        WHERE batch_id = ?
        """,
        (batch_id,),
    )

    source_embeddings = _sqlite_rows_for_ids(
        con,
        """
        SELECT node_id, model, dim, embedding_json, updated_at
        FROM source_embeddings
        WHERE node_id IN
        """,
        source_ids,
        "AND model = ?",
        (model_name,),
    )

    concept_embeddings = _sqlite_rows_for_ids(
        con,
        """
        SELECT node_id, model, dim, embedding_json, updated_at
        FROM concept_embeddings
        WHERE node_id IN
        """,
        concept_ids,
        "AND model = ?",
        (model_name,),
    )

    concept_stats = _sqlite_rows_for_ids(
        con,
        """
        SELECT concept_node_id, doc_freq, updated_at
        FROM concept_stats
        WHERE concept_node_id IN
        """,
        concept_ids,
    )

    with pg_engine.begin() as pg:
        if source_nodes:
            pg.execute(text(f"""
                INSERT INTO {schema}.source_nodes
                (node_id, canonical_url, name, created_at, first_seen_batch, last_seen_batch)
                VALUES
                (:node_id, :canonical_url, :name, :created_at, :first_seen_batch, :last_seen_batch)
                ON CONFLICT (node_id) DO UPDATE SET
                  canonical_url = EXCLUDED.canonical_url,
                  name = COALESCE(EXCLUDED.name, {schema}.source_nodes.name),
                  first_seen_batch = COALESCE({schema}.source_nodes.first_seen_batch, EXCLUDED.first_seen_batch),
                  last_seen_batch = EXCLUDED.last_seen_batch
            """), source_nodes)

        if concept_nodes:
            pg.execute(text(f"""
                INSERT INTO {schema}.concept_nodes
                (node_id, concept_type, name, tier, created_at, first_seen_batch, last_seen_batch)
                VALUES
                (:node_id, :concept_type, :name, :tier, :created_at, :first_seen_batch, :last_seen_batch)
                ON CONFLICT (node_id) DO UPDATE SET
                  concept_type = EXCLUDED.concept_type,
                  name = EXCLUDED.name,
                  tier = EXCLUDED.tier,
                  first_seen_batch = COALESCE({schema}.concept_nodes.first_seen_batch, EXCLUDED.first_seen_batch),
                  last_seen_batch = EXCLUDED.last_seen_batch
            """), concept_nodes)

        if source_payloads:
            pg.execute(text(f"""
                INSERT INTO {schema}.source_payloads
                (source_node_id, batch_id, canonical_url, groq_output_json, cognitive_map_json, updated_at)
                VALUES
                (:source_node_id, :batch_id, :canonical_url, :groq_output_json, :cognitive_map_json, :updated_at)
                ON CONFLICT (source_node_id, batch_id) DO UPDATE SET
                  canonical_url = EXCLUDED.canonical_url,
                  groq_output_json = EXCLUDED.groq_output_json,
                  cognitive_map_json = EXCLUDED.cognitive_map_json,
                  updated_at = EXCLUDED.updated_at
            """), source_payloads)

        if source_concept_edges:
            pg.execute(text(f"""
                INSERT INTO {schema}.source_concept_edges
                (source_node_id, concept_node_id, confidence, batch_id, updated_at)
                VALUES
                (:source_node_id, :concept_node_id, :confidence, :batch_id, :updated_at)
                ON CONFLICT (source_node_id, concept_node_id) DO UPDATE SET
                  confidence = EXCLUDED.confidence,
                  batch_id = EXCLUDED.batch_id,
                  updated_at = EXCLUDED.updated_at
            """), source_concept_edges)

        if source_embeddings:
            pg.execute(text(f"""
                INSERT INTO {schema}.source_embeddings
                (node_id, model, dim, embedding_json, updated_at)
                VALUES
                (:node_id, :model, :dim, :embedding_json, :updated_at)
                ON CONFLICT (node_id, model) DO UPDATE SET
                  dim = EXCLUDED.dim,
                  embedding_json = EXCLUDED.embedding_json,
                  updated_at = EXCLUDED.updated_at
            """), source_embeddings)

        if concept_embeddings:
            pg.execute(text(f"""
                INSERT INTO {schema}.concept_embeddings
                (node_id, model, dim, embedding_json, updated_at)
                VALUES
                (:node_id, :model, :dim, :embedding_json, :updated_at)
                ON CONFLICT (node_id, model) DO UPDATE SET
                  dim = EXCLUDED.dim,
                  embedding_json = EXCLUDED.embedding_json,
                  updated_at = EXCLUDED.updated_at
            """), concept_embeddings)

        if concept_edges:
            pg.execute(text(f"""
                INSERT INTO {schema}.concept_edges
                (source_node_id, target_node_id, method, strength, batch_id, updated_at)
                VALUES
                (:source_node_id, :target_node_id, :method, :strength, :batch_id, :updated_at)
                ON CONFLICT (source_node_id, target_node_id, method) DO UPDATE SET
                  strength = EXCLUDED.strength,
                  batch_id = EXCLUDED.batch_id,
                  updated_at = EXCLUDED.updated_at
            """), concept_edges)

        if source_edges:
            pg.execute(text(f"""
                INSERT INTO {schema}.source_edges
                (source_node_id, target_node_id, method, strength, batch_id, updated_at)
                VALUES
                (:source_node_id, :target_node_id, :method, :strength, :batch_id, :updated_at)
                ON CONFLICT (source_node_id, target_node_id, method) DO UPDATE SET
                  strength = EXCLUDED.strength,
                  batch_id = EXCLUDED.batch_id,
                  updated_at = EXCLUDED.updated_at
            """), source_edges)

        if concept_stats:
            pg.execute(text(f"""
                INSERT INTO {schema}.concept_stats
                (concept_node_id, doc_freq, updated_at)
                VALUES
                (:concept_node_id, :doc_freq, :updated_at)
                ON CONFLICT (concept_node_id) DO UPDATE SET
                  doc_freq = EXCLUDED.doc_freq,
                  updated_at = EXCLUDED.updated_at
            """), concept_stats)

    counts = {
        "source_nodes": len(source_nodes),
        "concept_nodes": len(concept_nodes),
        "source_payloads": len(source_payloads),
        "source_concept_edges": len(source_concept_edges),
        "concept_edges": len(concept_edges),
        "source_edges": len(source_edges),
        "source_embeddings": len(source_embeddings),
        "concept_embeddings": len(concept_embeddings),
        "concept_stats": len(concept_stats),
    }
    return counts


def main() -> None:
    args = parse_args()

    batch_normalized_jsonl = Path(args.in_path)
    batch_id = batch_normalized_jsonl.stem

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    cur = con.cursor()

    try:
        ensure_schema(con)

        batch_objs: list[dict[str, Any]] = []
        with batch_normalized_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                obj = json.loads(line)
                sn = obj.get("source_node") or {}
                canonical_url = (sn.get("canonical_url") or "").strip()
                if not canonical_url:
                    continue

                batch_objs.append(obj)

        if not batch_objs:
            print("No usable rows found.")
            return

        for obj in batch_objs:
            sn = obj.get("source_node") or {}

            cur.execute(
                """
                INSERT INTO source_nodes (node_id, canonical_url, name, first_seen_batch, last_seen_batch)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                  name = COALESCE(excluded.name, source_nodes.name),
                  last_seen_batch = excluded.last_seen_batch
                """,
                (sn["id"], sn["canonical_url"], sn["name"], batch_id, batch_id),
            )

            cm = obj.get("cognitive_map")
            cm_json = json.dumps(cm, ensure_ascii=False) if isinstance(cm, dict) else None

            cur.execute(
                """
                INSERT INTO source_payloads (source_node_id, batch_id, canonical_url, groq_output_json, cognitive_map_json, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_node_id, batch_id) DO UPDATE SET
                  canonical_url = excluded.canonical_url,
                  groq_output_json = excluded.groq_output_json,
                  cognitive_map_json = excluded.cognitive_map_json,
                  updated_at = datetime('now')
                """,
                (
                    sn["id"],
                    batch_id,
                    sn["canonical_url"],
                    json.dumps(obj.get("groq_output"), ensure_ascii=False),
                    cm_json,
                ),
            )

        all_concept_nodes: dict[str, dict] = {}
        for obj in batch_objs:
            for cn in (obj.get("concept_nodes") or []):
                cid = cn["id"]
                if cid not in all_concept_nodes:
                    all_concept_nodes[cid] = cn

        for cid, cn in all_concept_nodes.items():
            cur.execute(
                """
                INSERT INTO concept_nodes (node_id, concept_type, name, tier, first_seen_batch, last_seen_batch)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                  last_seen_batch = excluded.last_seen_batch
                """,
                (cn["id"], cn["type"], cn["name"], cn["tier"], batch_id, batch_id),
            )

        batch_concept_ids_set = set()
        for obj in batch_objs:
            sn = obj.get("source_node") or {}
            concept_nodes = obj.get("concept_nodes") or []

            for cn in concept_nodes:
                batch_concept_ids_set.add(cn["id"])
                cur.execute(
                    """
                    INSERT INTO source_concept_edges (source_node_id, concept_node_id, confidence, batch_id, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(source_node_id, concept_node_id) DO UPDATE SET
                      confidence = excluded.confidence,
                      batch_id = excluded.batch_id,
                      updated_at = datetime('now')
                    """,
                    (sn["id"], cn["id"], cn.get("confidence"), batch_id),
                )

        con.commit()

        _update_concept_doc_freq(cur, list(batch_concept_ids_set))
        con.commit()

        source_ids = [obj["source_node"]["id"] for obj in batch_objs]
        existing_sources = set()
        if source_ids:
            placeholders = ",".join(["?"] * len(source_ids))
            rows = cur.execute(
                f"SELECT node_id FROM source_embeddings WHERE model=? AND node_id IN ({placeholders})",
                [args.model_name, *source_ids],
            ).fetchall()
            existing_sources = {r[0] for r in rows}

        model = None

        to_embed_sources = [
            obj
            for obj in batch_objs
            if obj["source_node"]["id"] not in existing_sources and isinstance(obj.get("cognitive_map"), dict)
        ]
        if to_embed_sources:
            model = SentenceTransformer(args.model_name)
            texts = [build_source_embedding_text(obj) for obj in to_embed_sources]
            new_emb = model.encode(texts, batch_size=16, convert_to_numpy=True, show_progress_bar=True)
            new_emb = l2_normalize(new_emb)

            for obj, vec in zip(to_embed_sources, new_emb):
                sid = obj["source_node"]["id"]
                cur.execute(
                    """
                    INSERT INTO source_embeddings (node_id, model, dim, embedding_json, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(node_id, model) DO UPDATE SET
                      dim = excluded.dim,
                      embedding_json = excluded.embedding_json,
                      updated_at = datetime('now')
                    """,
                    (sid, args.model_name, int(vec.shape[0]), json.dumps(vec.round(6).tolist())),
                )
            con.commit()

        concept_ids = list(all_concept_nodes.keys())
        existing_concepts = set()
        if concept_ids:
            placeholders = ",".join(["?"] * len(concept_ids))
            rows = cur.execute(
                f"SELECT node_id FROM concept_embeddings WHERE model=? AND node_id IN ({placeholders})",
                [args.model_name, *concept_ids],
            ).fetchall()
            existing_concepts = {r[0] for r in rows}

        to_embed_concepts = [cn for cid, cn in all_concept_nodes.items() if cid not in existing_concepts]
        if to_embed_concepts:
            if model is None:
                model = SentenceTransformer(args.model_name)

            texts = [build_concept_embedding_text(cn) for cn in to_embed_concepts]
            new_emb = model.encode(texts, batch_size=16, convert_to_numpy=True, show_progress_bar=True)
            new_emb = l2_normalize(new_emb)

            for cn, vec in zip(to_embed_concepts, new_emb):
                cur.execute(
                    """
                    INSERT INTO concept_embeddings (node_id, model, dim, embedding_json, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(node_id, model) DO UPDATE SET
                      dim = excluded.dim,
                      embedding_json = excluded.embedding_json,
                      updated_at = datetime('now')
                    """,
                    (cn["id"], args.model_name, int(vec.shape[0]), json.dumps(vec.round(6).tolist())),
                )
            con.commit()

        all_concept_rows = cur.execute(
            "SELECT node_id, embedding_json FROM concept_embeddings WHERE model=?",
            (args.model_name,),
        ).fetchall()

        if len(all_concept_rows) >= 2 and concept_ids:
            all_concept_ids = [r[0] for r in all_concept_rows]
            all_concept_emb = np.array([json.loads(r[1]) for r in all_concept_rows], dtype=np.float32)
            all_concept_emb = l2_normalize(all_concept_emb)

            idx_concept = {cid: i for i, cid in enumerate(all_concept_ids)}
            batch_concept_ids = list(all_concept_nodes.keys())

            for src_cid in batch_concept_ids:
                src_i = idx_concept.get(src_cid)
                if src_i is None:
                    continue

                vec = all_concept_emb[src_i]
                scores = all_concept_emb @ vec
                scores[src_i] = -1.0
                order = np.argsort(-scores)

                added = 0
                for j in order:
                    s = float(scores[int(j)])
                    if s < args.min_sim:
                        break
                    if added >= args.top_k:
                        break

                    tgt_cid = all_concept_ids[int(j)]
                    cur.execute(
                        """
                        INSERT INTO concept_edges (source_node_id, target_node_id, method, strength, batch_id, updated_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(source_node_id, target_node_id, method) DO UPDATE SET
                          strength = excluded.strength,
                          batch_id = excluded.batch_id,
                          updated_at = datetime('now')
                        """,
                        (src_cid, tgt_cid, "embedding", round(s, 6), batch_id),
                    )
                    added += 1
                con.commit()

        all_source_rows = cur.execute(
            "SELECT node_id, embedding_json FROM source_embeddings WHERE model=?",
            (args.model_name,),
        ).fetchall()

        if len(all_source_rows) >= 2 and source_ids:
            all_source_ids = [r[0] for r in all_source_rows]
            all_source_emb = np.array([json.loads(r[1]) for r in all_source_rows], dtype=np.float32)
            all_source_emb = l2_normalize(all_source_emb)

            idx_source = {sid: i for i, sid in enumerate(all_source_ids)}
            batch_source_ids = [obj["source_node"]["id"] for obj in batch_objs]

            for src_sid in batch_source_ids:
                src_i = idx_source.get(src_sid)
                if src_i is None:
                    continue

                vec = all_source_emb[src_i]
                scores = all_source_emb @ vec
                scores[src_i] = -1.0
                order = np.argsort(-scores)

                added = 0
                for j in order:
                    s = float(scores[int(j)])
                    if s < args.min_sim:
                        break
                    if added >= args.top_k:
                        break

                    tgt_sid = all_source_ids[int(j)]
                    cur.execute(
                        """
                        INSERT INTO source_edges (source_node_id, target_node_id, method, strength, batch_id, updated_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(source_node_id, target_node_id, method) DO UPDATE SET
                          strength = excluded.strength,
                          batch_id = excluded.batch_id,
                          updated_at = datetime('now')
                        """,
                        (src_sid, tgt_sid, "embedding", round(s, 6), batch_id),
                    )
                    added += 1
                con.commit()

        try:
            mirror_counts = mirror_batch_to_postgres(
        con=con,
        batch_id=batch_id,
        model_name=args.model_name,
        source_ids=source_ids,
        concept_ids=concept_ids,
    )
            print(
        "OK mirrored batch to Postgres:",
        f"source_nodes={mirror_counts['source_nodes']},",
        f"concept_nodes={mirror_counts['concept_nodes']},",
        f"source_payloads={mirror_counts['source_payloads']},",
        f"source_concept_edges={mirror_counts['source_concept_edges']},",
        f"concept_edges={mirror_counts['concept_edges']},",
        f"source_edges={mirror_counts['source_edges']},",
        f"source_embeddings={mirror_counts['source_embeddings']},",
        f"concept_embeddings={mirror_counts['concept_embeddings']},",
        f"concept_stats={mirror_counts['concept_stats']}",
    )
        except Exception as e:
            print(f"graph mirror sync failed; aborting batch: {e}")
            raise

        print(f"OK batch ingested: {batch_id}")
        print(f"Source nodes: {len(batch_objs)}, Concept nodes: {len(all_concept_nodes)}")

    finally:
        con.close()


if __name__ == "__main__":
    main()
