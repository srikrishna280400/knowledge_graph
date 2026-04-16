import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any
import numpy as np
from sentence_transformers import SentenceTransformer


MODEL_NAME_DEFAULT = "sentence-transformers/all-MiniLM-L6-v2"
TOP_K_DEFAULT = 8
MIN_SIM_DEFAULT = 0.55


def stable_node_id(canonical_url: str) -> str:
    h = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()
    return f"node_{h}"


def _safe_list(x):
    return x if isinstance(x, list) else []


def build_embedding_text(obj: dict) -> str:
    node = obj.get("node") or {}
    cm = obj.get("cognitive_map") or {}
    root = cm.get("root") if isinstance(cm, dict) else {}

    domains = _safe_list(cm.get("domains")) if isinstance(cm, dict) else []
    themes = _safe_list(cm.get("themes")) if isinstance(cm, dict) else []
    concepts = _safe_list(cm.get("concepts")) if isinstance(cm, dict) else []
    micro = _safe_list(cm.get("micro_insights")) if isinstance(cm, dict) else []

    parts = []
    parts.append(f"URL: {node.get('canonical_url') or obj.get('canonical_url') or ''}")

    if isinstance(root, dict):
        parts.append(f"Label: {root.get('label') or ''}")
        parts.append(f"Need: {root.get('intellectual_need') or ''}")
        parts.append(f"Why: {root.get('why_saved') or ''}")

    if domains:
        parts.append(
            "Domains: "
            + "; ".join(
                [d.get("name", "") for d in domains if isinstance(d, dict) and d.get("name")]
            )
        )

    if themes:
        parts.append(
            "Themes: "
            + "; ".join(
                [t.get("name", "") for t in themes if isinstance(t, dict) and t.get("name")]
            )
        )

    if concepts:
        parts.append(
            "Concepts: "
            + "; ".join(
                [c.get("name", "") for c in concepts if isinstance(c, dict) and c.get("name")]
            )
        )

    if micro:
        parts.append(
            "Micro: "
            + " | ".join([m.get("text", "") for m in micro if isinstance(m, dict) and m.get("text")])
        )

    return "\n".join([p for p in parts if p.strip()])


def l2_normalize(a: np.ndarray) -> np.ndarray:
    a = a.astype(np.float32)
    n = np.linalg.norm(a, axis=1, keepdims=True)
    return a / np.clip(n, 1e-12, None)


def ensure_schema(con: sqlite3.Connection) -> None:
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
          node_num INTEGER PRIMARY KEY AUTOINCREMENT,
          node_id TEXT NOT NULL,
          name TEXT,
          created_at TEXT DEFAULT (datetime('now')),
          first_seen_batch TEXT,
          last_seen_batch TEXT
        );
    """)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_nodes_node_id ON nodes(node_id);")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS node_payloads (
          node_id TEXT NOT NULL,
          batch_id TEXT NOT NULL,
          canonical_url TEXT,
          groq_output_json TEXT,
          cognitive_map_json TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (node_id, batch_id)
        );
    """)
    # Upgrade older DBs
    try:
        cur.execute("ALTER TABLE node_payloads ADD COLUMN canonical_url TEXT;")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS node_embeddings (
          node_id TEXT NOT NULL,
          model TEXT NOT NULL,
          dim INTEGER NOT NULL,
          embedding_json TEXT NOT NULL,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (node_id, model)
        );
    """)
    # Upgrade older DBs
    try:
        cur.execute("ALTER TABLE node_embeddings ADD COLUMN updated_at TEXT;")
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS node_edges (
          source_node_id TEXT NOT NULL,
          target_node_id TEXT NOT NULL,
          method TEXT NOT NULL,
          strength REAL NOT NULL,
          batch_id TEXT,
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (source_node_id, target_node_id, method)
        );
    """)

    con.commit()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Path to *.normalized.jsonl")
    p.add_argument("--db", dest="db_path", default="data/graph_store.sqlite")
    p.add_argument("--model", dest="model_name", default=MODEL_NAME_DEFAULT)
    p.add_argument("--top-k", dest="top_k", type=int, default=TOP_K_DEFAULT)
    p.add_argument("--min-sim", dest="min_sim", type=float, default=MIN_SIM_DEFAULT)
    p.add_argument("--debug", action="store_true", help="Print edge-build diagnostics")
    return p.parse_args()


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

    # 1) Read batch
    batch_objs: list[dict[str, Any]] = []
    with batch_normalized_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            obj = json.loads(line)
            canonical_url = (obj.get("canonical_url") or "").strip()
            if not canonical_url:
                continue

            node = obj.get("node") or {}
            node_id = (node.get("id") or "").strip() or stable_node_id(canonical_url)
            node_name = (node.get("name") or "").strip() or canonical_url

            obj["node"] = {"id": node_id, "name": node_name, "canonical_url": canonical_url}
            batch_objs.append(obj)

    if not batch_objs:
        con.close()
        print("No usable rows found (missing canonical_url).")
        return

    # 2) Upsert nodes + payloads
    for obj in batch_objs:
        n = obj["node"]

        cur.execute(
            """
            INSERT INTO nodes (node_id, name, first_seen_batch, last_seen_batch)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
              name = COALESCE(excluded.name, nodes.name),
              last_seen_batch = excluded.last_seen_batch
            """,
            (n["id"], n["name"], batch_id, batch_id),
        )

        cm = obj.get("cognitive_map")
        cm_json = json.dumps(cm, ensure_ascii=False) if isinstance(cm, dict) else None

        cur.execute(
            """
            INSERT INTO node_payloads
              (node_id, batch_id, canonical_url, groq_output_json, cognitive_map_json, updated_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(node_id, batch_id) DO UPDATE SET
              canonical_url = excluded.canonical_url,
              groq_output_json = excluded.groq_output_json,
              cognitive_map_json = excluded.cognitive_map_json,
              updated_at = datetime('now')
            """,
            (
                n["id"],
                batch_id,
                n["canonical_url"],
                json.dumps(obj.get("groq_output"), ensure_ascii=False),
                cm_json,
            ),
        )

    con.commit()

    # 3) Determine which nodes need embeddings
    node_ids = [obj["node"]["id"] for obj in batch_objs]
    existing = set()
    if node_ids:
        placeholders = ",".join(["?"] * len(node_ids))
        rows = cur.execute(
            f"SELECT node_id FROM node_embeddings WHERE model=? AND node_id IN ({placeholders})",
            [args.model_name, *node_ids],
        ).fetchall()
        existing = {r[0] for r in rows}

    to_embed = [
        obj
        for obj in batch_objs
        if (obj["node"]["id"] not in existing) and isinstance(obj.get("cognitive_map"), dict)
    ]

    # 4) Embed only missing ones
    if to_embed:
        model = SentenceTransformer(args.model_name)
        texts = [build_embedding_text(obj) for obj in to_embed]
        new_emb = model.encode(texts, batch_size=16, convert_to_numpy=True, show_progress_bar=True)
        new_emb = l2_normalize(new_emb)

        for obj, vec in zip(to_embed, new_emb):
            nid = obj["node"]["id"]
            cur.execute(
                """
                INSERT INTO node_embeddings (node_id, model, dim, embedding_json, updated_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                ON CONFLICT(node_id, model) DO UPDATE SET
                  dim = excluded.dim,
                  embedding_json = excluded.embedding_json,
                  updated_at = datetime('now')
                """,
                (nid, args.model_name, int(vec.shape[0]), json.dumps(vec.round(6).tolist())),
            )
        con.commit()
    else:
        print(f"Nothing new to embed for this batch/model. Will still build edges. OK: {batch_id}")

    # 5) Build edges for ALL nodes in this batch (must already have embeddings)
    all_rows = cur.execute(
        "SELECT node_id, embedding_json FROM node_embeddings WHERE model=?",
        (args.model_name,),
    ).fetchall()

    all_node_ids = [r[0] for r in all_rows]
    if len(all_node_ids) < 2:
        con.close()
        print("Not enough embeddings to build edges (need at least 2).")
        return

    all_emb = np.array([json.loads(r[1]) for r in all_rows], dtype=np.float32)
    all_emb = l2_normalize(all_emb)
    idx = {nid: i for i, nid in enumerate(all_node_ids)}

    batch_node_ids = [obj["node"]["id"] for obj in batch_objs]

    if args.debug:
        print(
            "EDGE BUILD: all_rows=", len(all_rows),
            "batch_nodes=", len(batch_node_ids),
            "min_sim=", args.min_sim,
            "top_k=", args.top_k
        )

    inserted_total = 0
    skipped_missing_embedding = 0

    for src in batch_node_ids:
        src_i = idx.get(src)
        if src_i is None:
            skipped_missing_embedding += 1
            continue

        vec = all_emb[src_i]
        scores = all_emb @ vec
        scores[src_i] = -1.0

        if args.debug:
            best_sim = float(np.max(scores))
            print("SRC", src, "best_sim", best_sim)

        order = np.argsort(-scores)
        added = 0

        for j in order:
            s = float(scores[int(j)])
            if s < args.min_sim:
                break
            if added >= args.top_k:
                break

            tgt = all_node_ids[int(j)]
            cur.execute(
                """
                INSERT INTO node_edges (source_node_id, target_node_id, method, strength, batch_id, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(source_node_id, target_node_id, method) DO UPDATE SET
                  strength = excluded.strength,
                  batch_id = excluded.batch_id,
                  updated_at = datetime('now')
                """,
                (src, tgt, "embedding", round(s, 6), batch_id),
            )
            added += 1
            inserted_total += 1

    con.commit()

    if args.debug:
        edges_now = cur.execute("SELECT COUNT(*) FROM node_edges").fetchone()[0]
        print("EDGE BUILD DONE: inserted_total=", inserted_total,
              "skipped_missing_embedding=", skipped_missing_embedding,
              "node_edges_count_now=", edges_now)

    con.close()

    print(f"OK batch ingested: {batch_id}")
    print(f"New nodes embedded: {len(to_embed)}")
    print(f"Total nodes with embeddings: {len(all_node_ids)}")


if __name__ == "__main__":
    main()
