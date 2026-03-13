# graph_ingest_incremental.py

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
from sentence_transformers import SentenceTransformer

MODEL_NAME_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K_DEFAULT = 8
MIN_SIM_DEFAULT = 0.55


def _safe_list(x):
    return x if isinstance(x, list) else []


def build_source_embedding_text(obj: dict) -> str:
    """Text for source node embedding"""
    source_node = obj.get("source_node") or {}
    cm = obj.get("cognitive_map") or {}
    root = cm.get("root") if isinstance(cm, dict) else {}

    domains = _safe_list(cm.get("domains")) if isinstance(cm, dict) else []
    themes = _safe_list(cm.get("themes")) if isinstance(cm, dict) else []
    concepts = _safe_list(cm.get("concepts")) if isinstance(cm, dict) else []
    micro = _safe_list(cm.get("micro_insights")) if isinstance(cm, dict) else []

    parts = []
    parts.append(f"URL: {source_node.get('canonical_url') or ''}")

    if isinstance(root, dict):
        parts.append(f"Label: {root.get('label') or ''}")

    parts.append(f"Need: {root.get('intellectual_need') or ''}")
    parts.append(f"Why: {root.get('why_saved') or ''}")

    if domains:
        parts.append(
            "Domains: " + "; ".join([d.get("name", "") for d in domains if isinstance(d, dict) and d.get("name")])
        )
    if themes:
        parts.append(
            "Themes: " + "; ".join([t.get("name", "") for t in themes if isinstance(t, dict) and t.get("name")])
        )
    if concepts:
        parts.append(
            "Concepts: " + "; ".join([c.get("name", "") for c in concepts if isinstance(c, dict) and c.get("name")])
        )
    if micro:
        parts.append(
            "Micro: " + " | ".join([m.get("text", "") for m in micro if isinstance(m, dict) and m.get("text")])
        )

    return "\n".join([p for p in parts if p.strip()])


def build_concept_embedding_text(concept: dict) -> str:
    """Text for concept node embedding"""
    return f"Type: {concept.get('type') or ''}\nName: {concept.get('name') or ''}\nTier: {concept.get('tier', '')}"


def l2_normalize(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.clip(n, 1e-12, None)


def ensure_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()

    # Source nodes (one per URL)
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

    # Concept nodes (one per unique concept)
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

    # Source payloads
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

    # Source embeddings
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

    # Concept embeddings
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

    # Source -> Concept edges (mentions)  [confidence is now NULLABLE]
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_concept_edges (
          source_node_id TEXT NOT NULL,
          concept_node_id TEXT NOT NULL,
          confidence REAL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, concept_node_id)
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS ix_sc_edges_source ON source_concept_edges(source_node_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_sc_edges_concept ON source_concept_edges(concept_node_id);")

    # Concept <-> Concept edges (semantic similarity)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS concept_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength REAL NOT NULL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, target_node_id, method)
        );
        """
    )

    # Source <-> Source edges (document similarity)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS source_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength REAL NOT NULL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, target_node_id, method)
        );
        """
    )

    # Global concept stats (doc frequency)
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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Path to *20r_outputs.normalized.jsonl") # change every batch
    p.add_argument("--db", dest="db_path", default="data/graph_store.sqlite")
    p.add_argument("--model", dest="model_name", default=MODEL_NAME_DEFAULT)
    p.add_argument("--top-k", dest="top_k", type=int, default=TOP_K_DEFAULT)
    p.add_argument("--min-sim", dest="min_sim", type=float, default=MIN_SIM_DEFAULT)
    p.add_argument("--debug", action="store_true", help="Print edge-build diagnostics")
    return p.parse_args()


def _update_concept_doc_freq(cur: sqlite3.Cursor, concept_ids: list[str]) -> None:
    """Update doc_freq only for concepts touched in this batch."""
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


def main() -> None:
    args = parse_args()

    batch_normalized_jsonl = Path(args.in_path)
    batch_id = batch_normalized_jsonl.stem

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys=ON;")
    cur = con.cursor()

    ensure_schema(con)

    # Read batch
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
        con.close()
        print("No usable rows found.")
        return

    # Upsert source nodes + payloads
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

    # Upsert concept nodes (dedupe within batch)
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

    # Create / update source -> concept edges (mentions)
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

    # Update global doc_freq only for concepts touched in this batch
    _update_concept_doc_freq(cur, list(batch_concept_ids_set))
    con.commit()

    # Embed sources (only missing)
    source_ids = [obj["source_node"]["id"] for obj in batch_objs]
    existing_sources = set()
    if source_ids:
        placeholders = ",".join(["?"] * len(source_ids))
        rows = cur.execute(
            f"SELECT node_id FROM source_embeddings WHERE model=? AND node_id IN ({placeholders})",
            [args.model_name, *source_ids],
        ).fetchall()
        existing_sources = {r[0] for r in rows}

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

    # Embed concepts (only missing)
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

    # Build concept <-> concept edges for batch concepts against ALL concepts
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

    # Build source <-> source edges for batch sources against ALL sources
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

    con.close()
    print(f"OK batch ingested: {batch_id}")
    print(f"Source nodes: {len(batch_objs)}, Concept nodes: {len(all_concept_nodes)}")


if __name__ == "__main__":
    main()