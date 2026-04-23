# export_to_obsidian.py
#
# Export graph_store.sqlite -> Obsidian vault Markdown notes.
#
# Key change vs v1:
# - Filenames are now HUMAN-READABLE (slug + short suffix)
# - Stable IDs remain in frontmatter (source_node_id / concept_node_id)
# - A persistent mapping file is stored at: <vault>/<root>/_meta/id_to_filename.json
# - All wikilinks are emitted using the mapped filename stems
#
# Safe for iterative runs:
# - Preserves any existing user-written content ABOVE the AUTO marker.
# - Rewrites the auto-generated section BELOW the marker.
#
# Usage (PowerShell):
#   cd <your backend folder>
#   python .\app\export_to_obsidian.py --db data/graph_store.sqlite --vault "D:\Downloaded Apps\Obsidian\PKG" --root KG2
#
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple


AUTO_MARKER = "\n<!-- AUTO-GENERATED BELOW: do not edit by hand (your edits above are preserved) -->\n"


# -----------------------------
# ID compatibility with normalize
# -----------------------------
def _norm_concept_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s\-/+.#]", "", s)
    return s.strip()


def stable_concept_node_id(concept_type: str, concept_name: str) -> Optional[str]:
    """
    Must match normalize_groq_output.py semantics:
    - concept_type lowercased
    - concept_name normalized
    - sha1(type||name) first 12 hex
    """
    import hashlib

    clean_type = (concept_type or "Concept").strip().lower()
    clean_name = _norm_concept_name(concept_name)
    if not clean_name:
        return None
    identity = f"{clean_type}||{clean_name}"
    h = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"concept_{clean_type}_{h}"


# -----------------------------
# Pretty filename mapping (ID -> filename stem)
# -----------------------------
def slugify(s: str, max_len: int = 80) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"[^a-z0-9\-]+", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s[:max_len].strip("-") or "untitled")


def short_suffix_from_node_id(node_id: str) -> str:
    # source_<40hex> or concept_<type>_<12hex>
    if not node_id:
        return "unknown"
    last = node_id.split("_")[-1]
    return last[-8:] if len(last) >= 8 else last


def load_id_map(meta_dir: Path) -> dict[str, str]:
    p = meta_dir / "id_to_filename.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_id_map(meta_dir: Path, m: dict[str, str]) -> None:
    meta_dir.mkdir(parents=True, exist_ok=True)
    p = meta_dir / "id_to_filename.json"
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def allocate_filename(kind_prefix: str, node_id: str, title: str, id_map: dict[str, str], used: set[str]) -> str:
    """
    Returns filename stem WITHOUT ".md".
    Deterministic for a given (node_id) once stored in id_map.
    """
    if node_id in id_map:
        return id_map[node_id]

    suf = short_suffix_from_node_id(node_id)
    base = slugify(title)
    stem = f"{kind_prefix} - {base} - {suf}"

    candidate = stem
    i = 2
    while candidate in used:
        candidate = f"{stem}-{i}"
        i += 1

    id_map[node_id] = candidate
    used.add(candidate)
    return candidate


def wikilink_id(node_id: str, id_map: dict[str, str], display_text: Optional[str] = None) -> str:
    """
    Link to the NOTE filename stem from id_map, not the raw node_id.
    """
    stem = id_map.get(node_id) or node_id
    if display_text and display_text.strip() and display_text.strip() != stem:
        return f"[[{stem}|{display_text.strip()}]]"
    return f"[[{stem}]]"


# -----------------------------
# Helpers
# -----------------------------
def yaml_quote(s: Any) -> str:
    if s is None:
        return '""'
    s = str(s)
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_existing_user_header(md_path: Path) -> str:
    """
    If file exists:
      - If AUTO_MARKER exists, return everything before it (trim trailing whitespace lightly).
      - Else, treat whole file as user header and keep it, and we'll append marker+auto.
    """
    if not md_path.exists():
        return ""
    txt = md_path.read_text(encoding="utf-8", errors="ignore")
    idx = txt.find(AUTO_MARKER.strip("\n"))
    if idx >= 0:
        before = txt[:idx]
        return before.rstrip() + "\n\n"
    return txt.rstrip() + "\n\n"


def write_with_preserved_header(md_path: Path, auto_body: str, *, user_header: str = "") -> None:
    safe_mkdir(md_path.parent)
    out = (user_header or "") + AUTO_MARKER + auto_body.strip() + "\n"
    md_path.write_text(out, encoding="utf-8")


def md_escape_inline(s: str) -> str:
    return (s or "").replace("\n", " ").strip()


def as_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except Exception:
        return None


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# -----------------------------
# DB access
# -----------------------------
def connect_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def fetch_one(con: sqlite3.Connection, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
    cur = con.execute(sql, params)
    return cur.fetchone()


def fetch_all(con: sqlite3.Connection, sql: str, params: Tuple = ()) -> list[sqlite3.Row]:
    cur = con.execute(sql, params)
    return cur.fetchall()


# -----------------------------
# Export config
# -----------------------------
@dataclass
class ExportConfig:
    db_path: Path
    vault_path: Path
    root_folder: str = "KG2"
    top_k: int = 10
    min_sim: float = 0.55
    include_raw_json: bool = False
    overwrite_maps: bool = True


# -----------------------------
# Renderers
# -----------------------------
def render_frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {yaml_quote(item)}")
        else:
            lines.append(f"{k}: {yaml_quote(v)}")
    lines.append("---\n")
    return "\n".join(lines)


def render_cognitive_map_section(cmap: dict, id_map: dict[str, str]) -> str:
    """
    Renders root/domains/themes/concepts/micro_insights into readable bullets.
    Uses cognitive map hierarchy; links point to pretty concept notes via id_map.
    """
    if not isinstance(cmap, dict):
        return "## Cognitive map\n\n(No cognitive map JSON available for this source.)\n"

    root = cmap.get("root") if isinstance(cmap.get("root"), dict) else {}
    domains = cmap.get("domains") if isinstance(cmap.get("domains"), list) else []
    themes = cmap.get("themes") if isinstance(cmap.get("themes"), list) else []
    concepts = cmap.get("concepts") if isinstance(cmap.get("concepts"), list) else []
    micro = cmap.get("micro_insights") if isinstance(cmap.get("micro_insights"), list) else []

    out: list[str] = []
    out.append("## Cognitive map\n")

    # Root
    label = md_escape_inline(root.get("label", "")) if isinstance(root, dict) else ""
    need = md_escape_inline(root.get("intellectual_need", "")) if isinstance(root, dict) else ""
    why = md_escape_inline(root.get("why_saved", "")) if isinstance(root, dict) else ""
    if label or need or why:
        out.append("### Root\n")
        if label:
            out.append(f"- Label: {label}\n")
        if need:
            out.append(f"- Intellectual need: {need}\n")
        if why:
            out.append(f"- Why saved: {why}\n")

    def _link_for(concept_type: str, concept_name: str) -> str:
        cid = stable_concept_node_id(concept_type, concept_name)
        if cid and cid in id_map:
            return wikilink_id(cid, id_map, concept_name)
        return concept_name

    # Domains
    if domains:
        out.append("\n### Domains\n")
        for d in domains:
            if not isinstance(d, dict) or not d.get("name"):
                continue
            name = str(d.get("name"))
            link = _link_for("Domain", name)
            conf = as_float(d.get("confidence"))
            conf_txt = f" (confidence {conf:.2f})" if conf is not None else ""
            out.append(f"- {link}{conf_txt}\n")

    # Themes
    if themes:
        out.append("\n### Themes\n")
        for t in themes:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            ttype = str(t.get("type") or "Theme")
            name = str(t.get("name"))
            link = _link_for(ttype, name)
            conf = as_float(t.get("confidence"))
            conf_txt = f" (confidence {conf:.2f})" if conf is not None else ""
            out.append(f"- {link}{conf_txt}\n")

    # Concepts
    if concepts:
        out.append("\n### Concepts\n")
        for c in concepts:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            ctype = str(c.get("type") or "Concept")
            name = str(c.get("name"))
            link = _link_for(ctype, name)
            conf = as_float(c.get("confidence"))
            conf_txt = f" (confidence {conf:.2f})" if conf is not None else ""
            out.append(f"- {link}{conf_txt}\n")

    # Micro-insights
    if micro:
        out.append("\n### Micro-insights\n")
        for m in micro:
            if not isinstance(m, dict) or not m.get("text"):
                continue
            txt = str(m.get("text"))
            short = txt[:100]
            link = _link_for("Insight", short)
            conf = as_float(m.get("confidence"))
            conf_txt = f" (confidence {conf:.2f})" if conf is not None else ""
            out.append(f"- {link}{conf_txt}\n")

    if len(out) == 1:
        out.append("(Cognitive map JSON is present but empty.)\n")

    return "".join(out).rstrip() + "\n"


def render_related_list(title: str, items: list[Tuple[str, str, float]], id_map: dict[str, str]) -> str:
    """
    items: [(node_id, display_name, strength)]
    """
    if not items:
        return f"## {title}\n\n(None)\n"
    out = [f"## {title}\n\n"]
    for nid, name, strength in items:
        out.append(f"- {wikilink_id(nid, id_map, name)} (sim {strength:.3f})\n")
    return "".join(out).rstrip() + "\n"


# -----------------------------
# Prepare ID->filename map (critical so source notes can link to concepts prettily)
# -----------------------------
def prepare_id_map(con: sqlite3.Connection, root_dir: Path) -> dict[str, str]:
    meta_dir = root_dir / "_meta"
    id_map = load_id_map(meta_dir)
    used = set(id_map.values())

    # Allocate for ALL sources
    src_rows = fetch_all(con, "SELECT node_id, COALESCE(name, canonical_url) AS title FROM source_nodes ORDER BY node_num ASC")
    for r in src_rows:
        allocate_filename("S", r["node_id"], r["title"] or r["node_id"], id_map, used)

    # Allocate for ALL concepts
    con_rows = fetch_all(con, "SELECT node_id, name FROM concept_nodes ORDER BY node_num ASC")
    for r in con_rows:
        allocate_filename("C", r["node_id"], r["name"] or r["node_id"], id_map, used)

    save_id_map(meta_dir, id_map)
    return id_map


# -----------------------------
# Exporters
# -----------------------------
def export_source_notes(con: sqlite3.Connection, cfg: ExportConfig, out_dir: Path, id_map: dict[str, str]) -> int:
    sources = fetch_all(con, "SELECT node_id, canonical_url, name FROM source_nodes ORDER BY node_num ASC")
    count = 0

    for s in sources:
        sid = s["node_id"]
        url = s["canonical_url"]
        name = s["name"] or url or sid

        # Latest payload for this source (by updated_at desc)
        payload = fetch_one(
            con,
            """
            SELECT cognitive_map_json, groq_output_json, batch_id, updated_at
            FROM source_payloads
            WHERE source_node_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (sid,),
        )

        cmap = None
        groq_raw = None
        batch_id = None
        if payload:
            batch_id = payload["batch_id"]
            try:
                cmap = json.loads(payload["cognitive_map_json"]) if payload["cognitive_map_json"] else None
            except Exception:
                cmap = None
            try:
                groq_raw = json.loads(payload["groq_output_json"]) if payload["groq_output_json"] else None
            except Exception:
                groq_raw = None

        # Concepts mentioned (edges)
        rows = fetch_all(
            con,
            """
            SELECT e.concept_node_id, e.confidence, c.concept_type, c.name, c.tier
            FROM source_concept_edges e
            JOIN concept_nodes c ON c.node_id = e.concept_node_id
            WHERE e.source_node_id=?
            ORDER BY c.tier ASC, c.concept_type ASC, c.name ASC
            """,
            (sid,),
        )

        # Related sources
        rel_src_rows = fetch_all(
            con,
            """
            SELECT se.target_node_id, sn.name AS target_name, se.strength
            FROM source_edges se
            JOIN source_nodes sn ON sn.node_id = se.target_node_id
            WHERE se.source_node_id=? AND se.method='embedding' AND se.strength >= ?
            ORDER BY se.strength DESC
            LIMIT ?
            """,
            (sid, cfg.min_sim, cfg.top_k),
        )
        related_sources = [(r["target_node_id"], r["target_name"] or r["target_node_id"], float(r["strength"])) for r in rel_src_rows]

        # Render
        fm = render_frontmatter(
            {
                "kind": "source",
                "source_node_id": sid,
                "canonical_url": url,
                "title": name,
                "exported_at": now_iso(),
                "batch_id": batch_id or "",
                "aliases": [name] if name and name != url else [],
            }
        )

        body: list[str] = []
        body.append(f"# {name}\n\n")
        if url:
            body.append(f"- URL: {url}\n")

        # User area prompt (kept above marker if you write there)
        body.append("\n## My notes\n\n(Write your takeaways / claims / links here.)\n")

        # Cognitive map hierarchy
        body.append("\n")
        body.append(render_cognitive_map_section(cmap, id_map))

        # Mentioned concepts (flattened, guaranteed to match DB)
        body.append("\n## Mentioned concepts (flattened)\n\n")
        if not rows:
            body.append("(None)\n")
        else:
            for r in rows:
                cid = r["concept_node_id"]
                ctype = r["concept_type"]
                cname = r["name"]
                tier = r["tier"]
                conf = as_float(r["confidence"])
                conf_txt = f"{conf:.2f}" if conf is not None else "unknown"
                body.append(f"- {wikilink_id(cid, id_map, cname)} — {ctype}, tier {tier}, confidence {conf_txt}\n")

        # Related sources (embedding sim)
        body.append("\n")
        body.append(render_related_list("Related sources (semantic)", related_sources, id_map))

        # Optional: raw JSON
        if cfg.include_raw_json and groq_raw is not None:
            body.append("\n## Raw Groq output (JSON)\n\n")
            body.append("```json\n")
            body.append(json.dumps(groq_raw, ensure_ascii=False, indent=2))
            body.append("\n```\n")

        stem = id_map.get(sid, sid)
        md_path = out_dir / f"{stem}.md"
        user_hdr = read_existing_user_header(md_path)
        write_with_preserved_header(md_path, fm + "".join(body), user_header=user_hdr)
        count += 1

    return count


def export_concept_notes(con: sqlite3.Connection, cfg: ExportConfig, out_dir: Path, id_map: dict[str, str]) -> int:
    concepts = fetch_all(
        con,
        """
        SELECT c.node_id, c.concept_type, c.name, c.tier,
               COALESCE(cs.doc_freq, 0) AS doc_freq
        FROM concept_nodes c
        LEFT JOIN concept_stats cs ON cs.concept_node_id = c.node_id
        ORDER BY c.tier ASC, doc_freq DESC, c.name ASC
        """
    )
    count = 0

    for c in concepts:
        cid = c["node_id"]
        ctype = c["concept_type"]
        name = c["name"]
        tier = c["tier"]
        doc_freq = int(c["doc_freq"] or 0)

        # Sources that mention this concept (limit for readability)
        src_rows = fetch_all(
            con,
            """
            SELECT e.source_node_id, s.name AS source_name, s.canonical_url, e.confidence
            FROM source_concept_edges e
            JOIN source_nodes s ON s.node_id = e.source_node_id
            WHERE e.concept_node_id=?
            ORDER BY s.node_num ASC
            LIMIT 200
            """,
            (cid,),
        )

        # Similar concepts
        sim_rows = fetch_all(
            con,
            """
            SELECT ce.target_node_id, cn.name AS target_name, ce.strength
            FROM concept_edges ce
            JOIN concept_nodes cn ON cn.node_id = ce.target_node_id
            WHERE ce.source_node_id=? AND ce.method='embedding' AND ce.strength >= ?
            ORDER BY ce.strength DESC
            LIMIT ?
            """,
            (cid, cfg.min_sim, cfg.top_k),
        )
        similar = [(r["target_node_id"], r["target_name"] or r["target_node_id"], float(r["strength"])) for r in sim_rows]

        fm = render_frontmatter(
            {
                "kind": "concept",
                "concept_node_id": cid,
                "concept_type": ctype,
                "tier": tier,
                "doc_freq": doc_freq,
                "exported_at": now_iso(),
                "aliases": [name] if name else [],
            }
        )

        body: list[str] = []
        body.append(f"# {name}\n\n")
        body.append(f"- Type: {ctype}\n")
        body.append(f"- Tier: {tier}\n")
        body.append(f"- Doc frequency (sources mentioning): {doc_freq}\n")

        body.append("\n## My evergreen note\n\n(Write the idea in your own words, then link sources/concepts.)\n")

        body.append("\n## Mentioned in sources\n\n")
        if not src_rows:
            body.append("(None)\n")
        else:
            for r in src_rows:
                sid = r["source_node_id"]
                sname = r["source_name"] or r["canonical_url"] or sid
                conf = as_float(r["confidence"])
                conf_txt = f"{conf:.2f}" if conf is not None else "unknown"
                body.append(f"- {wikilink_id(sid, id_map, sname)} (confidence {conf_txt})\n")

        body.append("\n")
        body.append(render_related_list("Similar concepts (semantic)", similar, id_map))

        stem = id_map.get(cid, cid)
        md_path = out_dir / f"{stem}.md"
        user_hdr = read_existing_user_header(md_path)
        write_with_preserved_header(md_path, fm + "".join(body), user_header=user_hdr)
        count += 1

    return count


def export_maps(con: sqlite3.Connection, cfg: ExportConfig, maps_dir: Path, id_map: dict[str, str]) -> int:
    safe_mkdir(maps_dir)
    written = 0

    # Map: Top concepts by doc_freq
    rows = fetch_all(
        con,
        """
        SELECT c.node_id, c.name, c.concept_type, c.tier, cs.doc_freq
        FROM concept_stats cs
        JOIN concept_nodes c ON c.node_id = cs.concept_node_id
        ORDER BY cs.doc_freq DESC, c.tier ASC, c.name ASC
        LIMIT 500
        """
    )
    lines: list[str] = []
    lines.append(render_frontmatter({"kind": "map", "map": "top_concepts", "exported_at": now_iso()}))
    lines.append("# Top concepts (by frequency)\n\n")
    if not rows:
        lines.append("(None)\n")
    else:
        for r in rows:
            lines.append(
                f"- {wikilink_id(r['node_id'], id_map, r['name'])} — {r['concept_type']}, tier {r['tier']}, doc_freq {int(r['doc_freq'])}\n"
            )

    p = maps_dir / "Top Concepts.md"
    if cfg.overwrite_maps:
        write_with_preserved_header(p, "".join(lines), user_header="")
    else:
        if not p.exists():
            write_with_preserved_header(p, "".join(lines), user_header="")
    written += 1

    # Map: Concept type indexes
    for (ctype,) in fetch_all(con, "SELECT DISTINCT concept_type FROM concept_nodes ORDER BY concept_type ASC"):
        ctype = str(ctype)
        rows2 = fetch_all(
            con,
            """
            SELECT c.node_id, c.name, c.tier, COALESCE(cs.doc_freq, 0) AS doc_freq
            FROM concept_nodes c
            LEFT JOIN concept_stats cs ON cs.concept_node_id = c.node_id
            WHERE c.concept_type=?
            ORDER BY doc_freq DESC, c.name ASC
            LIMIT 500
            """,
            (ctype,),
        )
        lines2: list[str] = []
        lines2.append(render_frontmatter({"kind": "map", "map": "concept_type", "concept_type": ctype, "exported_at": now_iso()}))
        lines2.append(f"# {ctype} index\n\n")
        if not rows2:
            lines2.append("(None)\n")
        else:
            for r in rows2:
                lines2.append(f"- {wikilink_id(r['node_id'], id_map, r['name'])} — tier {r['tier']}, doc_freq {int(r['doc_freq'])}\n")

        p2 = maps_dir / f"{ctype} Index.md"
        if cfg.overwrite_maps:
            write_with_preserved_header(p2, "".join(lines2), user_header="")
        else:
            if not p2.exists():
                write_with_preserved_header(p2, "".join(lines2), user_header="")
        written += 1

    # Map: Sources index
    srcs = fetch_all(con, "SELECT node_id, name, canonical_url FROM source_nodes ORDER BY node_num ASC LIMIT 5000")
    lines3: list[str] = []
    lines3.append(render_frontmatter({"kind": "map", "map": "sources", "exported_at": now_iso()}))
    lines3.append("# Sources index\n\n")
    for s in srcs:
        title = s["name"] or s["canonical_url"] or s["node_id"]
        lines3.append(f"- {wikilink_id(s['node_id'], id_map, title)}\n")

    p3 = maps_dir / "Sources Index.md"
    if cfg.overwrite_maps:
        write_with_preserved_header(p3, "".join(lines3), user_header="")
    else:
        if not p3.exists():
            write_with_preserved_header(p3, "".join(lines3), user_header="")
    written += 1

    return written


# -----------------------------
# CLI
# -----------------------------
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", dest="db_path", default="data/graph_store.sqlite", help="Path to graph_store.sqlite")
    ap.add_argument("--vault", dest="vault_path", required=True, help="Path to Obsidian vault folder")
    ap.add_argument("--root", dest="root_folder", default="KG2", help="Root folder INSIDE vault to write into (default: KG2)")
    ap.add_argument("--top-k", dest="top_k", type=int, default=10, help="Top-K similar items to show")
    ap.add_argument("--min-sim", dest="min_sim", type=float, default=0.55, help="Min similarity for related lists")
    ap.add_argument("--include-raw-json", action="store_true", help="Include raw Groq output JSON blocks in Source notes")
    ap.add_argument("--no-overwrite-maps", action="store_true", help="Do not overwrite map pages if they already exist")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ExportConfig(
        db_path=Path(args.db_path),
        vault_path=Path(args.vault_path),
        root_folder=args.root_folder,
        top_k=args.top_k,
        min_sim=args.min_sim,
        include_raw_json=bool(args.include_raw_json),
        overwrite_maps=not bool(args.no_overwrite_maps),
    )

    if not cfg.db_path.exists():
        raise FileNotFoundError(f"Missing DB: {cfg.db_path}")
    if not cfg.vault_path.exists():
        raise FileNotFoundError(f"Vault folder does not exist: {cfg.vault_path}")

    root = cfg.vault_path / cfg.root_folder
    sources_dir = root / "Sources"
    concepts_dir = root / "Concepts"
    maps_dir = root / "Maps"
    meta_dir = root / "_meta"

    safe_mkdir(sources_dir)
    safe_mkdir(concepts_dir)
    safe_mkdir(maps_dir)
    safe_mkdir(meta_dir)

    con = connect_db(cfg.db_path)
    try:
        # Build/extend ID -> filename mapping first (so links resolve prettily everywhere)
        id_map = prepare_id_map(con, root)

        n_sources = export_source_notes(con, cfg, sources_dir, id_map)
        n_concepts = export_concept_notes(con, cfg, concepts_dir, id_map)
        n_maps = export_maps(con, cfg, maps_dir, id_map)
    finally:
        con.close()

    # Write a small run log
    log: list[str] = []
    log.append(render_frontmatter({"kind": "meta", "exported_at": now_iso(), "db": str(cfg.db_path)}))
    log.append("# Export log\n\n")
    log.append(f"- Exported sources: {n_sources}\n")
    log.append(f"- Exported concepts: {n_concepts}\n")
    log.append(f"- Exported map pages: {n_maps}\n")
    log.append(f"- Root folder: {cfg.root_folder}\n")
    (meta_dir / "export_log.md").write_text("".join(log), encoding="utf-8")

    print("OK exported to vault.")
    print(f"Sources: {n_sources}, Concepts: {n_concepts}, Maps: {n_maps}")
    print(f"Wrote under: {root}")


if __name__ == "__main__":
    main()
