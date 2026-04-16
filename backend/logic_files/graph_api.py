import json
import sqlite3
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()

def get_con():
    db_path = Path("data") / "graph_store.sqlite"
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row  # rows support mapping access by column name [web:2148]
    con.execute("PRAGMA foreign_keys=ON;")
    return con


@router.get("/graph")
def get_graph():
    con = get_con()
    cur = con.cursor()

    # Nodes: include node identity + the stored Groq payload (as dict)
    nodes = []
    for r in cur.execute(
        """
        SELECT
          n.node_num,
          n.node_id,
          n.canonical_url,
          n.name,
          p.groq_output_json,
          p.cognitive_map_json
        FROM nodes n
        LEFT JOIN (
          -- pick the latest payload per node (by created_at)
          SELECT node_id, groq_output_json, cognitive_map_json
          FROM node_payloads
          WHERE rowid IN (
            SELECT MAX(rowid) FROM node_payloads GROUP BY node_id
          )
        ) p ON p.node_id = n.node_id
        ORDER BY n.node_num
        """
    ):
        groq_output = json.loads(r["groq_output_json"]) if r["groq_output_json"] else None
        cognitive_map = json.loads(r["cognitive_map_json"]) if r["cognitive_map_json"] else None

        nodes.append(
            {
                "node_num": r["node_num"],
                "node_id": r["node_id"],
                "canonical_url": r["canonical_url"],
                "name": r["name"],
                "groq_output": groq_output,
                "cognitive_map": cognitive_map,
            }
        )

    # Edges
    edges = []
    for r in cur.execute(
        """
        SELECT
          source_node_id,
          target_node_id,
          method,
          strength,
          batch_id
        FROM node_edges
        ORDER BY strength DESC
        """
    ):
        edges.append(dict(r))

    con.close()
    return {"nodes": nodes, "edges": edges}
