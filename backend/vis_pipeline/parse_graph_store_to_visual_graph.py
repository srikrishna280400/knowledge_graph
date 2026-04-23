# parse_graph_store_to_visual_graph.py

from __future__ import annotations
import json
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


def safe_json_loads(s: Optional[str]) -> Optional[dict]:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def norm_space(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def platform_from_url(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if "reddit.com" in host:
        return "reddit"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    if "twitter.com" in host or "x.com" in host:
        return "twitter"
    if "linkedin.com" in host:
        return "linkedin"
    return "web"


def stable_source_id(canonical_url: str) -> str:
    h = hashlib.sha1((canonical_url or "").encode("utf-8")).hexdigest()
    return f"source_{h}"


def norm_concept_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-/+.#]", "", s)
    return s.strip()


def stable_concept_id(concept_type: str, concept_name: str) -> Optional[str]:
    clean_type = (concept_type or "Concept").strip().lower()
    clean_name = norm_concept_name(concept_name)
    if not clean_name:
        return None
    identity = f"{clean_type}||{clean_name}"
    h = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"concept_{clean_type}_{h}"


def stable_micro_id(source_id: str, micro_text: str) -> str:
    txt = (micro_text or "").strip()
    h = hashlib.sha1(txt.encode("utf-8")).hexdigest()[:12]
    return f"micro_{source_id}_{h}"


def pick_root(cm: Optional[dict]) -> dict:
    if not isinstance(cm, dict):
        return {"label": "", "intellectual_need": "Learn", "why_saved": ""}

    root = cm.get("root")
    if not isinstance(root, dict):
        root = {}

    # Handle both snake_case and older keys
    label = root.get("label") or ""
    need = root.get("intellectual_need") or root.get("intellectualneed") or "Learn"
    why = root.get("why_saved") or root.get("whysaved") or ""

    return {"label": norm_space(label), "intellectual_need": norm_space(need), "why_saved": norm_space(why)}


def pick_list(cm: Optional[dict], key_a: str, key_b: str) -> list:
    if not isinstance(cm, dict):
        return []
    v = cm.get(key_a)
    if isinstance(v, list):
        return v
    v = cm.get(key_b)
    if isinstance(v, list):
        return v
    return []


def tokenize(s: str) -> set:
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    parts = [p for p in s.split() if p]
    return set(parts)


def best_parent_by_token_overlap(child_name: str, parent_names: List[str]) -> Optional[str]:
    c = tokenize(child_name)
    if not c or not parent_names:
        return None
    best = None
    best_score = -1
    for p in parent_names:
        t = tokenize(p)
        if not t:
            continue
        score = len(c & t)
        if score > best_score:
            best_score = score
            best = p
    return best


def connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def fetch_latest_cognitive_map(con: sqlite3.Connection) -> Dict[str, dict]:
    rows = con.execute(
        """
        SELECT p.source_node_id, p.cognitive_map_json
        FROM source_payloads p
        JOIN (
            SELECT source_node_id, MAX(updated_at) AS maxu
            FROM source_payloads
            GROUP BY source_node_id
        ) latest ON latest.source_node_id = p.source_node_id AND latest.maxu = p.updated_at
        """
    ).fetchall()
    out = {}
    for r in rows:
        out[r["source_node_id"]] = safe_json_loads(r["cognitive_map_json"]) or {}
    return out


def fetch_sources(con: sqlite3.Connection) -> List[dict]:
    rows = con.execute(
        """
        SELECT node_id, canonical_url, COALESCE(name, canonical_url) AS name, created_at
        FROM source_nodes
        """
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "db_source_id": r["node_id"],  # source_<sha1(url)> from normalizer
                "url": r["canonical_url"],
                "name": norm_space(r["name"] or r["canonical_url"]),
                "created_at": r["created_at"],
                "platform": platform_from_url(r["canonical_url"]),
            }
        )
    return out


def fetch_concept_tiers(con: sqlite3.Connection) -> Dict[str, int]:
    rows = con.execute("SELECT node_id, tier FROM concept_nodes").fetchall()
    return {r["node_id"]: int(r["tier"] or 0) for r in rows}


def fetch_concept_doc_freq(con: sqlite3.Connection) -> Dict[str, int]:
    rows = con.execute("SELECT concept_node_id, doc_freq FROM concept_stats").fetchall()
    return {r["concept_node_id"]: int(r["doc_freq"] or 0) for r in rows}


def fetch_concept_edges(con: sqlite3.Connection, min_strength: float) -> List[dict]:
    rows = con.execute(
        """
        SELECT source_node_id, target_node_id, strength
        FROM concept_edges
        WHERE method='embedding' AND strength >= ?
        """,
        (float(min_strength),),
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "from": r["source_node_id"],
                "to": r["target_node_id"],
                "type": "relates_to",
                "strength": float(r["strength"]),
            }
        )
    return out


def assemble_visual_graph_from_graph_store(
    db_path: Path,
    *,
    min_related_strength: float = 0.78,
) -> dict:
    """
    Builds a unified graph + per-source local hierarchy inputs.

    Key points:
    - Root is per-source only (synthetic), read from source_payloads.cognitive_map_json.root.
    - Domains/Themes/Concepts are deduped globally using stable_concept_id (same scheme as normalizer).
    - Micro-insights are per-source synthetic nodes (so they exist even if upstream normalizer missed them).
    - Global relates_to edges come from concept_edges; we will only DISPLAY them on concept deep-dive boards.
    """
    con = connect_db(db_path)
    try:
        sources = fetch_sources(con)
        cm_by_source = fetch_latest_cognitive_map(con)
        tier_by_id = fetch_concept_tiers(con)
        doc_freq_by_id = fetch_concept_doc_freq(con)
        related_edges = fetch_concept_edges(con, min_strength=min_related_strength)

        nodes: Dict[str, dict] = {}
        per_source: Dict[str, dict] = {}

        for s in sources:
            sid = s["db_source_id"]
            cm = cm_by_source.get(sid) or {}

            root = pick_root(cm)
            root_node_id = f"root::{sid}"

            domains_raw = pick_list(cm, "domains", "domains")
            themes_raw = pick_list(cm, "themes", "themes")
            concepts_raw = pick_list(cm, "concepts", "concepts")
            micro_raw = pick_list(cm, "micro_insights", "microinsights")

            domain_nodes = []
            for d in domains_raw:
                if not isinstance(d, dict) or not d.get("name"):
                    continue
                name = norm_space(str(d["name"]))
                cid = stable_concept_id("Domain", name)
                if not cid:
                    continue
                nodes.setdefault(
                    cid,
                    {"id": cid, "tier": 1, "kind": "domain", "name": name, "type": "Domain", "doc_freq": doc_freq_by_id.get(cid, 0)},
                )
                domain_nodes.append(cid)

            theme_nodes = []
            for t in themes_raw:
                if not isinstance(t, dict) or not t.get("name"):
                    continue
                name = norm_space(str(t["name"]))
                cid = stable_concept_id("Theme", name)
                if not cid:
                    continue
                nodes.setdefault(
                    cid,
                    {"id": cid, "tier": 2, "kind": "theme", "name": name, "type": "Theme", "doc_freq": doc_freq_by_id.get(cid, 0)},
                )
                theme_nodes.append(cid)

            concept_nodes = []
            for c in concepts_raw:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                name = norm_space(str(c["name"]))
                ctype = norm_space(str(c.get("type") or "Concept")) or "Concept"
                cid = stable_concept_id(ctype, name)
                if not cid:
                    continue
                nodes.setdefault(
                    cid,
                    {
                        "id": cid,
                        "tier": 3,
                        "kind": "concept",
                        "name": name,
                        "type": ctype,
                        "doc_freq": doc_freq_by_id.get(cid, 0),
                    },
                )
                concept_nodes.append(cid)

            micro_nodes = []
            for m in micro_raw:
                if not isinstance(m, dict) or not m.get("text"):
                    continue
                txt = norm_space(str(m["text"]))
                mid = stable_micro_id(sid, txt)
                nodes.setdefault(
                    mid,
                    {"id": mid, "tier": 4, "kind": "micro", "name": txt, "type": norm_space(str(m.get("type") or "Quote"))},
                )
                micro_nodes.append(mid)

            # --- Per-source parent inference (local, deterministic) ---
            # We infer parent links by token overlap. This is not "cross-source".
            domain_names = [nodes[x]["name"] for x in domain_nodes]
            theme_names = [nodes[x]["name"] for x in theme_nodes]
            concept_names = [nodes[x]["name"] for x in concept_nodes]

            domain_id_by_name = {nodes[x]["name"]: x for x in domain_nodes}
            theme_id_by_name = {nodes[x]["name"]: x for x in theme_nodes}
            concept_id_by_name = {nodes[x]["name"]: x for x in concept_nodes}

            theme_parent = {}
            for tid in theme_nodes:
                tname = nodes[tid]["name"]
                best_d = best_parent_by_token_overlap(tname, domain_names) if domain_names else None
                theme_parent[tid] = domain_id_by_name.get(best_d) if best_d else (domain_nodes[0] if domain_nodes else None)

            concept_parent = {}
            for cid in concept_nodes:
                cname = nodes[cid]["name"]
                best_t = best_parent_by_token_overlap(cname, theme_names) if theme_names else None
                concept_parent[cid] = theme_id_by_name.get(best_t) if best_t else (theme_nodes[0] if theme_nodes else None)

            micro_parent = {}
            for mid in micro_nodes:
                mtxt = nodes[mid]["name"]
                best_c = best_parent_by_token_overlap(mtxt, concept_names) if concept_names else None
                micro_parent[mid] = concept_id_by_name.get(best_c) if best_c else (concept_nodes[0] if concept_nodes else None)

            per_source[sid] = {
                "source_id": sid,
                "url": s["url"],
                "platform": s["platform"],
                "created_at": s["created_at"],
                "root": root,
                "root_node_id": root_node_id,
                "domains": domain_nodes,
                "themes": theme_nodes,
                "concepts": concept_nodes,
                "micros": micro_nodes,
                "theme_parent": theme_parent,      # theme -> domain
                "concept_parent": concept_parent,  # concept -> theme
                "micro_parent": micro_parent,      # micro -> concept
            }

        graph = {
            "meta": {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "db_path": str(db_path),
                "min_related_strength": min_related_strength,
            },
            "nodes": nodes,               # all global nodes (domains/themes/concepts + per-source micro nodes)
            "sources": sources,           # sourcenodes rows
            "per_source": per_source,     # source-local structure incl root + parent maps
            "global_edges": {
                "relates_to": related_edges,  # for concept deep-dive boards only
            },
            "tier_by_id": tier_by_id,     # for filtering relates_to to tiers 2/3/4 (graph_store knows only conceptnodes)
            "doc_freq_by_id": doc_freq_by_id,
        }
        return graph
    finally:
        con.close()