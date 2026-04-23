from __future__ import annotations
import json
import re
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple


COLORS = {
    "root": "#B8DCFF",
    "tier_1": "#FF8F8F",
    "tier_2": "#FFD24A",
    "tier_3": "#7EE787",
    "tier_4": "#C297FF",
    "edge_contains": "#FFA94D",
    "edge_relates": "#7C6CFF",
}


def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def slug_short(s: str, max_words: int = 6, max_chars: int = 44) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    words = s.split(" ")
    s = " ".join(words[:max_words]).strip()
    s = re.sub(r"[^\w\s\-]", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return (s[:max_chars].strip() or "Untitled")


def vault_rel(root_folder: str, rel_path: str) -> str:
    rf = (root_folder or "").strip()
    if rf in ("", ".", "./"):
        return rel_path
    return f"{rf}/{rel_path}"


def load_id_map(meta_dir: Path) -> Dict[str, str]:
    p = meta_dir / "id_map.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_id_map(meta_dir: Path, m: Dict[str, str]) -> None:
    safe_mkdir(meta_dir)
    (meta_dir / "id_map.json").write_text(
        json.dumps(m, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def allocate_filename(node_id: str, label: str, id_map: Dict[str, str], used: set) -> str:
    if node_id in id_map:
        return id_map[node_id]

    base = slug_short(label)
    cand = base
    i = 2
    while cand in used:
        cand = f"{base} ({i})"
        i += 1

    id_map[node_id] = cand
    used.add(cand)
    return cand


def canvas_node_text(node_id: str, text: str, x: int, y: int, w: int, h: int, color: Optional[str]) -> dict:
    n = {"id": node_id, "type": "text", "text": text, "x": int(x), "y": int(y), "width": int(w), "height": int(h)}
    if color:
        n["color"] = color
    return n


def canvas_node_file(
    node_id: str,
    file_path: str,
    x: int,
    y: int,
    w: int,
    h: int,
    color: Optional[str],
    subpath: Optional[str] = None,
) -> dict:
    n = {
        "id": node_id,
        "type": "file",
        "file": file_path,
        "x": int(x),
        "y": int(y),
        "width": int(w),
        "height": int(h),
    }
    if color:
        n["color"] = color
    if subpath:
        n["subpath"] = subpath
    return n


def canvas_edge(
    edge_id: str,
    frm: str,
    to: str,
    label: str,
    color: Optional[str],
    dashed: bool = False,
    fromSide: Optional[str] = None,
    toSide: Optional[str] = None,
) -> dict:
    e = {"id": edge_id, "fromNode": frm, "toNode": to, "label": label}
    if fromSide:
        e["fromSide"] = fromSide
    if toSide:
        e["toSide"] = toSide
    if color:
        e["color"] = color
    if dashed:
        e["style"] = "dashed"
    return e


def color_for_tier(tier: int) -> str:
    if int(tier) == 1:
        return COLORS["tier_1"]
    if int(tier) == 2:
        return COLORS["tier_2"]
    if int(tier) == 3:
        return COLORS["tier_3"]
    if int(tier) == 4:
        return COLORS["tier_4"]
    return COLORS["root"]


def origin_edge_color(graph: dict, frm: str) -> str:
    if str(frm).startswith("root::"):
        return COLORS["root"]
    node = graph["nodes"].get(frm) or {}
    return color_for_tier(int(node.get("tier") or 0))


def write_canvas(path: Path, nodes: List[dict], edges: List[dict]) -> None:
    safe_mkdir(path.parent)
    path.write_text(json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, indent=2), encoding="utf-8")


def write_md_source(md_path: Path, ps: dict) -> None:
    safe_mkdir(md_path.parent)
    root = ps["root"]

    fm = [
        "---",
        f"url: {ps.get('url','')}",
        f"platform: {ps.get('platform','web')}",
        f"source_id: {ps.get('source_id')}",
        f"created_at: {ps.get('created_at','')}",
        "---",
        "",
    ]
    body = []
    body.append(f"# {md_path.stem}\n\n")
    body.append("## Root\n")
    body.append(f"- Label: {root.get('label','')}\n")
    body.append(f"- Need: {root.get('intellectual_need','')}\n")
    body.append(f"- Why saved: {root.get('why_saved','')}\n")
    body.append("\n## Notes\n")
    body.append("(Write your notes here)\n")

    md_path.write_text("".join(fm) + "".join(body), encoding="utf-8")


def write_md_concept(md_path: Path, node: dict) -> None:
    safe_mkdir(md_path.parent)

    title = md_path.stem
    fm = [
        "---",
        f"concept_id: {node.get('id')}",
        f"type: {node.get('type','Concept')}",
        f"tier: {node.get('tier',3)}",
        f"doc_freq: {node.get('doc_freq',0)}",
        "---",
        "",
    ]
    body = []
    body.append(f"# {title}\n\n")
    body.append("(Write your concept notes here)\n")

    md_path.write_text("".join(fm) + "".join(body), encoding="utf-8")


def _pick_side(ax: int, ay: int, bx: int, by: int) -> Tuple[str, str]:
    dx = bx - ax
    dy = by - ay
    if abs(dx) >= abs(dy):
        from_side = "right" if dx > 0 else "left"
    else:
        from_side = "bottom" if dy > 0 else "top"
    opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
    return from_side, opposite[from_side]


def _real_node_size(base_nid: str, graph: dict) -> Tuple[int, int]:
    if str(base_nid).startswith("root::"):
        return 880, 260

    node = graph["nodes"].get(base_nid) or {}
    tier = int(node.get("tier") or 0)

    if tier == 1:
        return 420, 210
    if tier == 2:
        return 340, 170
    if tier == 3:
        return 320, 160
    if tier == 4:
        return 420, 140

    return 220, 120


def export_source_board(
    vault_root: Path,
    root_folder: str,
    graph: dict,
    source_id: str,
    layout: dict,
) -> None:
    base = vault_root / root_folder
    boards_dir = base / "Boards" / "Sources"
    src_dir = base / "Sources"
    concept_dir = base / "Concepts"
    meta_dir = base / "_meta"

    safe_mkdir(boards_dir)
    safe_mkdir(src_dir)
    safe_mkdir(concept_dir)

    id_map = load_id_map(meta_dir)
    used = set(id_map.values())

    ps = graph["per_source"][source_id]
    root = ps["root"]
    root_id = ps["root_node_id"]

    src_title = root.get("label") or ps.get("url") or source_id
    src_fname = allocate_filename(source_id, src_title, id_map, used)
    write_md_source(src_dir / f"{src_fname}.md", ps)

    for cid in ps["concepts"]:
        node = graph["nodes"].get(cid)
        if not node:
            continue
        cfname = allocate_filename(cid, node["name"], id_map, used)
        write_md_concept(concept_dir / f"{cfname}.md", node)

    board_fname = allocate_filename(f"board::{source_id}", src_title, id_map, used)
    board_path = boards_dir / f"{board_fname}.canvas"

    nodes: List[dict] = []
    edges: List[dict] = []

    rx, ry = layout["nodes"][root_id]
    rw, rh = 880, 260
    root_text = (
        f"# {root.get('label','')}\n\n"
        f"Need: {root.get('intellectual_need','')}\n"
        f"Why: {root.get('why_saved','')}\n\n"
        f"Open note: [[{root_folder}/Sources/{src_fname}.md|{src_fname}]]"
    )
    nodes.append(canvas_node_text(root_id, root_text, rx, ry, rw, rh, COLORS["root"]))

    for nid, (x, y) in layout["nodes"].items():
        if nid == root_id:
            continue
        node = graph["nodes"].get(nid)
        if not node:
            continue

        tier = int(node.get("tier") or 0)

        if tier == 1:
            w, h = 420, 210
            txt = f"# 🌍 {node['name']}"
            nodes.append(canvas_node_text(nid, txt, x, y, w, h, COLORS["tier_1"]))

        elif tier == 2:
            w, h = 340, 170
            txt = f"## 🗂 {node['name']}"
            nodes.append(canvas_node_text(nid, txt, x, y, w, h, COLORS["tier_2"]))

        elif tier == 3:
            w, h = 320, 160
            txt = f"# {node['name']}"
            nodes.append(canvas_node_text(nid, txt, x, y, w, h, COLORS["tier_3"]))

        elif tier == 4:
            w, h = 420, 140
            micro_txt = node["name"]
            if len(micro_txt) > 220:
                micro_txt = micro_txt[:217] + "..."
            txt = f"💡 {micro_txt}"
            nodes.append(canvas_node_text(nid, txt, x, y, w, h, COLORS["tier_4"]))

    ec = 0
    for etype, frm, to in layout["edges"]:
        if etype != "contains":
            continue
        if frm not in layout["nodes"] or to not in layout["nodes"]:
            continue

        ax, ay = layout["nodes"][frm]
        bx, by = layout["nodes"][to]
        fs, ts = _pick_side(ax, ay, bx, by)

        ec += 1
        edge_color = origin_edge_color(graph, frm)
        edges.append(canvas_edge(f"e_{ec}", frm, to, "", edge_color, fromSide=fs, toSide=ts))

    write_canvas(board_path, nodes, edges)
    save_id_map(meta_dir, id_map)


def export_overview_board(
    vault_root: Path,
    root_folder: str,
    graph: dict,
    layout: dict,
) -> None:
    """
    Overview board using:
    - real source nodes rendered like source boards
    - tiny synthetic port/router nodes for cross-source routing
    """
    base = vault_root / root_folder
    boards_dir = base / "Boards"
    src_dir = base / "Sources"
    meta_dir = base / "_meta"

    safe_mkdir(boards_dir)
    safe_mkdir(src_dir)

    id_map = load_id_map(meta_dir)
    used = set(id_map.values())

    nodes: List[dict] = []
    edges: List[dict] = []

    node_ref = layout.get("node_ref", {})
    node_kind = layout.get("node_kind", {})

    for render_nid, (x, y) in layout["nodes"].items():
        kind = node_kind.get(render_nid, "real")

        if kind == "router":
            nodes.append(canvas_node_text(render_nid, "", x - 3, y - 3, 6, 6, None))
            continue

        if kind == "port":
            nodes.append(canvas_node_text(render_nid, "", x - 5, y - 5, 10, 10, None))
            continue

        base_nid = node_ref.get(render_nid, render_nid)

        if str(base_nid).startswith("root::"):
            source_id = str(base_nid).replace("root::", "")
            ps = graph["per_source"].get(source_id)
            if not ps:
                continue

            root = ps["root"]
            src_title = root.get("label") or ps.get("url") or source_id
            src_fname = allocate_filename(source_id, src_title, id_map, used)
            write_md_source(src_dir / f"{src_fname}.md", ps)

            txt = (
                f"# {root.get('label','')}\n\n"
                f"Need: {root.get('intellectual_need','')}\n"
                f"Why: {root.get('why_saved','')}\n\n"
                f"Open note: [[{root_folder}/Sources/{src_fname}.md|{src_fname}]]"
            )
            w, h = 880, 260
            color = COLORS["root"]
            nodes.append(canvas_node_text(render_nid, txt, x, y, w, h, color))
            continue

        node = graph["nodes"].get(base_nid)
        if not node:
            continue

        tier = int(node.get("tier") or 0)

        if tier == 1:
            w, h = 420, 210
            txt = f"# 🌍 {node['name']}"
            color = COLORS["tier_1"]
        elif tier == 2:
            w, h = 340, 170
            txt = f"## 🗂 {node['name']}"
            color = COLORS["tier_2"]
        elif tier == 3:
            w, h = 320, 160
            txt = f"# {node['name']}"
            color = COLORS["tier_3"]
        elif tier == 4:
            w, h = 420, 140
            micro_txt = node["name"]
            if len(micro_txt) > 220:
                micro_txt = micro_txt[:217] + "..."
            txt = f"💡 {micro_txt}"
            color = COLORS["tier_4"]
        else:
            continue

        nodes.append(canvas_node_text(render_nid, txt, x, y, w, h, color))

    ec = 0
    for edge in layout.get("edges", []):
        edge_type = edge.get("type")
        from_id = edge.get("from")
        to_id = edge.get("to")
        style = edge.get("style", "solid")
        scope = edge.get("scope", "local")

        if from_id not in layout["nodes"] or to_id not in layout["nodes"]:
            continue

        from_side = edge.get("fromSide")
        to_side = edge.get("toSide")

        if scope == "local" and (not from_side or not to_side):
            ax, ay = layout["nodes"][from_id]
            bx, by = layout["nodes"][to_id]
            from_side, to_side = _pick_side(ax, ay, bx, by)

        base_from_id = node_ref.get(from_id, from_id)

        if scope == "cross_source" or edge_type == "relates_to":
            edge_color = COLORS["edge_relates"]
        elif edge_type == "contains":
            edge_color = origin_edge_color(graph, base_from_id)
        else:
            edge_color = COLORS["edge_contains"]

        ec += 1
        edges.append(
            canvas_edge(
                f"e_{ec}",
                from_id,
                to_id,
                "",
                edge_color,
                dashed=(style == "dashed"),
                fromSide=from_side,
                toSide=to_side,
            )
        )

    write_canvas(boards_dir / "00_Overview.canvas", nodes, edges)
    save_id_map(meta_dir, id_map)


def export_concept_deep_dive_board(
    vault_root: Path,
    root_folder: str,
    graph: dict,
    center_concept_id: str,
    layout: dict,
    *,
    min_strength: float = 0.80,
    max_related: int = 12,
) -> None:
    base = vault_root / root_folder
    boards_dir = base / "Boards" / "Concepts"
    concept_dir = base / "Concepts"
    meta_dir = base / "_meta"

    safe_mkdir(boards_dir)
    safe_mkdir(concept_dir)

    id_map = load_id_map(meta_dir)
    used = set(id_map.values())

    center_node = graph["nodes"].get(center_concept_id)
    if not center_node:
        return

    center_fname = allocate_filename(center_concept_id, center_node["name"], id_map, used)
    write_md_concept(concept_dir / f"{center_fname}.md", center_node)

    board_path = boards_dir / f"02_Concept__{slug_short(center_fname, max_words=4, max_chars=32)}.canvas"

    nodes: List[dict] = []
    edges: List[dict] = []

    cx, cy = layout["nodes"][center_concept_id]
    nodes.append(
        canvas_node_text(
            center_concept_id,
            f"# {center_node['name']}",
            cx,
            cy,
            620,
            260,
            COLORS["tier_3"],
        )
    )

    related_ids = []
    for nid in layout["nodes"].keys():
        if nid == center_concept_id:
            continue
        tier = graph.get("tier_by_id", {}).get(nid)
        if tier is None:
            continue
        if int(tier) not in (2, 3, 4):
            continue
        related_ids.append(nid)

    related_ids = related_ids[:max_related]

    for nid in related_ids:
        node = graph["nodes"].get(nid)
        if not node:
            continue

        x, y = layout["nodes"][nid]
        tier = int(node.get("tier") or graph.get("tier_by_id", {}).get(nid) or 3)

        if tier == 2:
            nodes.append(canvas_node_text(nid, f"## 🗂 {node['name']}", x, y, 300, 150, COLORS["tier_2"]))
        else:
            fname = allocate_filename(nid, node["name"], id_map, used)
            write_md_concept(concept_dir / f"{fname}.md", node)
            nodes.append(canvas_node_text(nid, f"# {node['name']}", x, y, 250, 120, COLORS["tier_3"]))

    ec = 0
    for etype, frm, to in layout["edges"]:
        if etype != "relates_to":
            continue
        if to not in related_ids:
            continue

        ok = False
        for e in graph["global_edges"]["relates_to"]:
            if e["from"] == frm and e["to"] == to and float(e.get("strength") or 0) >= min_strength:
                ok = True
                break
        if not ok:
            continue

        ec += 1
        edges.append(
            canvas_edge(
                f"rel_{ec}",
                frm,
                to,
                "",
                COLORS["edge_relates"],
                dashed=True,
                fromSide="right",
                toSide="left",
            )
        )

    write_canvas(board_path, nodes, edges)
    save_id_map(meta_dir, id_map)

    (meta_dir / "last_run.json").write_text(
        json.dumps({"ran_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
