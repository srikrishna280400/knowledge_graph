import sqlite3
from pathlib import Path

def main():
    db_path = Path("data") / "graph_store.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA foreign_keys=ON;")

    con.executescript("""
    CREATE TABLE IF NOT EXISTS nodes (
        node_num INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id TEXT NOT NULL UNIQUE,
        canonical_url TEXT NOT NULL UNIQUE,
        name TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS node_embeddings (
        node_id TEXT NOT NULL,
        model TEXT NOT NULL,
        dim INTEGER NOT NULL,
        embedding_json TEXT NOT NULL,
        PRIMARY KEY (node_id, model),
        FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS node_edges (
        source_node_id TEXT NOT NULL,
        target_node_id TEXT NOT NULL,
        method TEXT NOT NULL,
        strength REAL NOT NULL,
        PRIMARY KEY (source_node_id, target_node_id, method),
        FOREIGN KEY (source_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE,
        FOREIGN KEY (target_node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
    );
    """)

    con.commit()
    con.close()
    print(f"OK: {db_path}")

if __name__ == "__main__":
    main()
