from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, List, Tuple


LAYOUT = {
    "source_board": {
        "W": 11000,
        "H": 5200,
        "xpad": 500,
        "y_root": 140,
        "y_domains": 900,
        "y_themes": 1800,
        "y_concepts": 2900,
        "y_micros": 4100,
    },
    "overview_radial": {
        "outer_pad_x": 2200,
        "outer_pad_y": 1200,
        "source_gap_x": 9000,
        "source_top_gap": 2600,
        "port_offset": 700,
        "router_offset": 1000,
        "lane_gap": 95,
        "tier_gap": 2200,
        "port_slot_gap": 18,
        "cluster_threshold": 0.30,
        "min_related_draw": 0.75,
    },
    "concept": {
        "W": 2400,
        "H": 2400,
        "cx": 1200,
        "cy": 1200,
        "r1": 360,
        "r2": 560,
    },
}


def even_spread_x(n: int, W: int, xpad: int) -> List[int]:
    n = max(1, n)
    step = (W - 2 * xpad) / n
    return [int(xpad + (i + 0.5) * step) for i in range(n)]


def group_children_under_parents(
    parents: List[str],
    children: List[str],
    child_parent: Dict[str, str],
) -> Dict[str, List[str]]:
    by = defaultdict(list)
    for c in children:
        p = child_parent.get(c)
        if p in parents:
            by[p].append(c)
        else:
            by[None].append(c)
    return by


def layout_source_board(graph: dict, source_id: str) -> dict:
    """
    Strict non-overlap hierarchical layout.

    Rules:
    - Every parent's children stay below it.
    - Sibling subtrees get dedicated horizontal space.
    - No clamping into narrow bands.
    - Prefer wider canvas over overlap/intermingling.
    - Micros are stacked vertically under each concept.
    """
    cfg = LAYOUT["source_board"]
    ps = graph["per_source"][source_id]

    domains = sorted(ps["domains"])
    themes = sorted(ps["themes"])
    concepts = sorted(ps["concepts"])
    micros = sorted(ps["micros"])

    theme_parent = ps["theme_parent"]
    concept_parent = ps["concept_parent"]
    micro_parent = ps["micro_parent"]

    themes_by_domain = group_children_under_parents(domains, themes, theme_parent)
    concepts_by_theme = group_children_under_parents(themes, concepts, concept_parent)
    micros_by_concept = group_children_under_parents(concepts, micros, micro_parent)

    pos = {"nodes": {}, "edges": []}

    root_w = 880
    domain_w = 420
    theme_w = 340
    concept_w = 320
    micro_w = 420

    xpad = 700
    domain_gap = 900
    theme_gap = 700
    concept_gap = 420
    micro_dx = 820
    micro_y_offset = 380
    micro_x_jitter = 90

    y_root = cfg["y_root"]
    y_domains = cfg["y_domains"]
    y_themes = cfg["y_themes"]
    y_concepts = cfg["y_concepts"]

    theme_lane_dy = 70
    concept_lane_dy = 80
    micro_dy = 320
    micro_max_cols = 3
    micro_cap = 10

    def concept_subtree_width(cid: str) -> int:
        mlist = sorted(micros_by_concept.get(cid, []))[:micro_cap]
        if not mlist:
            return concept_w + 80
        return max(concept_w + 80, micro_w + 80)

    def theme_subtree_width(tid: str) -> int:
        clist = sorted(concepts_by_theme.get(tid, []))
        if not clist:
            return theme_w + 120

        total = 0
        for i, cid in enumerate(clist):
            total += concept_subtree_width(cid)
            if i < len(clist) - 1:
                total += concept_gap

        return max(theme_w + 120, total + 120)

    def domain_subtree_width(did: str) -> int:
        tlist = sorted(themes_by_domain.get(did, []))
        if not tlist:
            return domain_w + 200

        total = 0
        for i, tid in enumerate(tlist):
            total += theme_subtree_width(tid)
            if i < len(tlist) - 1:
                total += theme_gap

        return max(domain_w + 200, total + 180)

    domain_widths = {did: domain_subtree_width(did) for did in domains}

    unparented_themes = sorted(themes_by_domain.get(None, []))
    unparented_width = 0
    if unparented_themes:
        for i, tid in enumerate(unparented_themes):
            unparented_width += theme_subtree_width(tid)
            if i < len(unparented_themes) - 1:
                unparented_width += theme_gap

    total_domains_width = 0
    for i, did in enumerate(domains):
        total_domains_width += domain_widths[did]
        if i < len(domains) - 1:
            total_domains_width += domain_gap

    content_width = max(total_domains_width, unparented_width, root_w)
    effective_w = max(cfg["W"], content_width + 2 * xpad)

    root_id = ps["root_node_id"]
    root_x = effective_w // 2
    pos["nodes"][root_id] = (int(root_x), int(y_root))

    domain_band: Dict[str, Tuple[int, int, int]] = {}

    if domains:
        total_needed = total_domains_width
        cursor = (effective_w - total_needed) // 2

        for i, did in enumerate(domains):
            w = domain_widths[did]
            left = cursor
            right = cursor + w
            cx = (left + right) // 2
            dy = y_domains + (i % 2) * 40

            pos["nodes"][did] = (int(cx), int(dy))
            pos["edges"].append(("contains", root_id, did))
            domain_band[did] = (int(left), int(right), int(cx))

            cursor = right + domain_gap

    theme_band: Dict[str, Tuple[int, int, int]] = {}

    for did in domains:
        tlist = sorted(themes_by_domain.get(did, []))
        if not tlist:
            continue

        _, _, dcx = domain_band[did]
        widths = {tid: theme_subtree_width(tid) for tid in tlist}
        total_needed = sum(widths[tid] for tid in tlist) + theme_gap * (len(tlist) - 1)

        cursor = dcx - total_needed // 2
        for j, tid in enumerate(tlist):
            tw = widths[tid]
            t_left = cursor
            t_right = cursor + tw
            tx = (t_left + t_right) // 2
            ty = y_themes + (j % 2) * theme_lane_dy

            pos["nodes"][tid] = (int(tx), int(ty))
            pos["edges"].append(("contains", did, tid))
            theme_band[tid] = (int(t_left), int(t_right), int(tx))

            cursor = t_right + theme_gap

    if unparented_themes:
        widths = {tid: theme_subtree_width(tid) for tid in unparented_themes}
        total_needed = sum(widths[tid] for tid in unparented_themes) + theme_gap * (len(unparented_themes) - 1)

        cursor = (effective_w - total_needed) // 2
        for j, tid in enumerate(unparented_themes):
            tw = widths[tid]
            t_left = cursor
            t_right = cursor + tw
            tx = (t_left + t_right) // 2
            ty = y_themes + ((j + 1) % 2) * theme_lane_dy

            pos["nodes"][tid] = (int(tx), int(ty))
            pos["edges"].append(("contains", root_id, tid))
            theme_band[tid] = (int(t_left), int(t_right), int(tx))

            cursor = t_right + theme_gap

    concept_band: Dict[str, Tuple[int, int, int]] = {}

    for tid in sorted(themes):
        clist = sorted(concepts_by_theme.get(tid, []))
        if not clist or tid not in theme_band:
            continue

        _, _, tcx = theme_band[tid]
        widths = {cid: concept_subtree_width(cid) for cid in clist}
        total_needed = sum(widths[cid] for cid in clist) + concept_gap * (len(clist) - 1)

        cursor = tcx - total_needed // 2
        for j, cid in enumerate(clist):
            cw = widths[cid]
            c_left = cursor
            c_right = cursor + cw
            cx = (c_left + c_right) // 2
            cy = y_concepts + (j % 3) * concept_lane_dy

            pos["nodes"][cid] = (int(cx), int(cy))
            pos["edges"].append(("contains", tid, cid))
            concept_band[cid] = (int(c_left), int(c_right), int(cx))

            cursor = c_right + concept_gap

    for cid in sorted(concepts):
        mlist = sorted(micros_by_concept.get(cid, []))[:micro_cap]
        if not mlist or cid not in pos["nodes"]:
            continue

        cx, cy = pos["nodes"][cid]

        if cid in concept_band:
            left, right, _ = concept_band[cid]
        else:
            left, right = cx - 1200, cx + 1200

        usable_left = left + 40
        usable_right = right - 40
        avail_w = max(1, usable_right - usable_left)

        fit_cols = max(1, int(avail_w // micro_dx))
        cols = max(1, min(fit_cols, micro_max_cols, len(mlist)))

        rows = [mlist[i:i + cols] for i in range(0, len(mlist), cols)]

        for row_i, row_ids in enumerate(rows):
            row_len = len(row_ids)
            row_span = (row_len - 1) * micro_dx
            row_start = cx - row_span // 2
            row_start = max(usable_left, min(usable_right - row_span, row_start))

            for col_i, mid in enumerate(row_ids):
                wave_x = -micro_x_jitter if (row_i + col_i) % 2 == 0 else micro_x_jitter
                wave_y = 35 if col_i % 2 else 0

                mx = row_start + col_i * micro_dx + wave_x
                mx = max(usable_left, min(usable_right, mx))
                my = cy + micro_y_offset + row_i * micro_dy + wave_y

                pos["nodes"][mid] = (int(mx), int(my))
                pos["edges"].append(("contains", cid, mid))

    return pos


def _node_box_size(graph: dict, base_nid: str) -> Tuple[int, int]:
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

    return 200, 120


def _layout_bbox(graph: dict, layout: dict) -> Tuple[int, int, int, int, int, int]:
    xs = []
    ys = []
    rs = []
    bs = []

    for nid, (x, y) in layout["nodes"].items():
        w, h = _node_box_size(graph, nid)
        xs.append(int(x))
        ys.append(int(y))
        rs.append(int(x + w))
        bs.append(int(y + h))

    min_x = min(xs) if xs else 0
    min_y = min(ys) if ys else 0
    max_r = max(rs) if rs else 0
    max_b = max(bs) if bs else 0
    return min_x, min_y, max_r, max_b, max_r - min_x, max_b - min_y


def _calculate_source_similarity(graph: dict) -> Dict[Tuple[str, str], float]:
    similarity = {}
    sources = list(graph["per_source"].keys())

    for i, sid1 in enumerate(sources):
        ps1 = graph["per_source"][sid1]
        nodes1 = set(ps1["domains"] + ps1["themes"] + ps1["concepts"] + ps1["micros"])

        for sid2 in sources[i + 1:]:
            ps2 = graph["per_source"][sid2]
            nodes2 = set(ps2["domains"] + ps2["themes"] + ps2["concepts"] + ps2["micros"])

            if not nodes1 or not nodes2:
                continue

            intersection = len(nodes1 & nodes2)
            union = len(nodes1 | nodes2)
            sim = intersection / union if union else 0.0

            if sim > 0:
                similarity[(sid1, sid2)] = sim

    return similarity


def _cluster_sources(
    source_ids: List[str],
    similarity: Dict[Tuple[str, str], float],
    threshold: float,
) -> List[List[str]]:
    edges_by_source = defaultdict(set)
    for sid in source_ids:
        edges_by_source[sid] = set()

    for (s1, s2), sim in similarity.items():
        if sim >= threshold:
            edges_by_source[s1].add(s2)
            edges_by_source[s2].add(s1)

    visited = set()
    clusters = []

    def dfs(node: str, cluster: List[str]) -> None:
        visited.add(node)
        cluster.append(node)
        for neighbor in sorted(edges_by_source.get(node, [])):
            if neighbor not in visited:
                dfs(neighbor, cluster)

    for source in sorted(source_ids):
        if source not in visited:
            cluster = []
            dfs(source, cluster)
            clusters.append(cluster)

    clusters.sort(key=lambda c: (-len(c), c))
    return clusters


def radial_positions(
    center: Tuple[int, int],
    radius: int,
    n: int,
    start_angle: float = -math.pi / 2,
) -> List[Tuple[int, int]]:
    if n <= 0:
        return []
    cx, cy = center
    out = []
    for i in range(n):
        ang = start_angle + (2 * math.pi * i / n)
        out.append((int(cx + radius * math.cos(ang)), int(cy + radius * math.sin(ang))))
    return out


def _scoped_node_id(sid: str, nid: str) -> str:
    return f"srcnode::{sid}::{nid}"


def _source_membership(graph: dict) -> Dict[str, List[str]]:
    memberships = defaultdict(set)
    for sid, ps in graph["per_source"].items():
        for nid in ps["domains"] + ps["themes"] + ps["concepts"] + ps["micros"]:
            memberships[nid].add(sid)
    return {nid: sorted(sids) for nid, sids in memberships.items()}


def _collect_cross_source_relations(graph: dict, min_strength: float) -> List[dict]:
    memberships = _source_membership(graph)
    tier_by_id = graph.get("tier_by_id", {})

    out = []
    seen = set()

    for edge in graph.get("global_edges", {}).get("relates_to", []):
        from_id = edge["from"]
        to_id = edge["to"]
        strength = float(edge.get("strength") or 0.0)

        if strength < min_strength or from_id == to_id:
            continue

        from_tier = tier_by_id.get(from_id)
        to_tier = tier_by_id.get(to_id)
        if from_tier is None or to_tier is None:
            continue

        from_tier = int(from_tier)
        to_tier = int(to_tier)

        if from_tier not in (1, 2, 3, 4) or to_tier != from_tier:
            continue

        from_sources = memberships.get(from_id, [])
        to_sources = memberships.get(to_id, [])

        if not from_sources or not to_sources:
            continue

        for from_sid in from_sources:
            for to_sid in to_sources:
                if from_sid == to_sid:
                    continue

                from_render = _scoped_node_id(from_sid, from_id)
                to_render = _scoped_node_id(to_sid, to_id)

                key = tuple(sorted((from_render, to_render))) + (from_tier,)
                if key in seen:
                    continue
                seen.add(key)

                out.append(
                    {
                        "type": "relates_to",
                        "tier": from_tier,
                        "from_sid": from_sid,
                        "to_sid": to_sid,
                        "from_base": from_id,
                        "to_base": to_id,
                        "from_render": from_render,
                        "to_render": to_render,
                        "strength": strength,
                    }
                )

    return out


def layout_overview_radial_sources(graph: dict) -> dict:
    """
    Source-islands overview:

    - Each source island uses the SAME hierarchy layout as layout_source_board().
    - Local hierarchy edges stay local and are drawn directly.
    - Cross-source relates_to edges are routed through external ports + highway routers.
    - Cross-source routing supports tiers 1/2/3/4 only, never root-to-root.
    """
    cfg = LAYOUT["overview_radial"]

    source_ids = list(graph["per_source"].keys())
    similarity = _calculate_source_similarity(graph)
    clusters = _cluster_sources(source_ids, similarity, threshold=float(cfg["cluster_threshold"]))
    ordered_sources = [sid for cluster in clusters for sid in cluster]

    source_layouts = {sid: layout_source_board(graph, sid) for sid in ordered_sources}
    source_boxes_local = {sid: _layout_bbox(graph, source_layouts[sid]) for sid in ordered_sources}

    cross_relations = _collect_cross_source_relations(graph, min_strength=float(cfg["min_related_draw"]))
    source_index = {sid: i for i, sid in enumerate(ordered_sources)}

    tier_groups = {1: [], 2: [], 3: [], 4: []}
    for rel in cross_relations:
        if rel["from_sid"] not in source_index or rel["to_sid"] not in source_index:
            continue
        tier_groups[int(rel["tier"])].append(rel)

    for tier in (1, 2, 3, 4):
        tier_groups[tier].sort(
            key=lambda r: (
                min(source_index[r["from_sid"]], source_index[r["to_sid"]]),
                max(source_index[r["from_sid"]], source_index[r["to_sid"]]),
                r["from_base"],
                r["to_base"],
            )
        )

    lane_start_y = {}
    cursor_y = int(cfg["outer_pad_y"])
    for tier in (1, 2, 3, 4):
        lane_start_y[tier] = cursor_y
        block_h = max(1, len(tier_groups[tier])) * int(cfg["lane_gap"])
        cursor_y += block_h + int(cfg["tier_gap"])

    source_top_y = cursor_y + int(cfg["source_top_gap"])

    final_layout = {
        "nodes": {},
        "edges": [],
        "source_centers": {},
        "source_boxes": {},
        "node_ref": {},
        "node_source": {},
        "node_kind": {},
    }

    real_geom = {}

    cursor_x = int(cfg["outer_pad_x"])
    for sid in ordered_sources:
        local_layout = source_layouts[sid]
        min_x, min_y, _, _, local_w, local_h = source_boxes_local[sid]

        dx = cursor_x - min_x
        dy = source_top_y - min_y

        left = cursor_x
        top = source_top_y
        right = left + local_w
        bottom = top + local_h

        final_layout["source_boxes"][sid] = {
            "left": int(left),
            "top": int(top),
            "right": int(right),
            "bottom": int(bottom),
            "width": int(local_w),
            "height": int(local_h),
        }
        final_layout["source_centers"][sid] = (int((left + right) / 2), int((top + bottom) / 2))

        for base_nid, (local_x, local_y) in local_layout["nodes"].items():
            render_nid = _scoped_node_id(sid, base_nid)
            gx = int(local_x + dx)
            gy = int(local_y + dy)

            final_layout["nodes"][render_nid] = (gx, gy)
            final_layout["node_ref"][render_nid] = base_nid
            final_layout["node_source"][render_nid] = sid
            final_layout["node_kind"][render_nid] = "real"

            w, h = _node_box_size(graph, base_nid)
            real_geom[render_nid] = {
                "x": gx,
                "y": gy,
                "w": w,
                "h": h,
                "cx": gx + w // 2,
                "cy": gy + h // 2,
                "left": gx,
                "right": gx + w,
                "top": gy,
                "bottom": gy + h,
            }

        for etype, frm, to in local_layout["edges"]:
            render_from = _scoped_node_id(sid, frm)
            render_to = _scoped_node_id(sid, to)
            if render_from not in final_layout["nodes"] or render_to not in final_layout["nodes"]:
                continue
            final_layout["edges"].append(
                {
                    "type": etype,
                    "from": render_from,
                    "to": render_to,
                    "style": "solid",
                    "scope": "local",
                }
            )

        cursor_x = right + int(cfg["source_gap_x"])

    port_slot_counts = defaultdict(int)
    edge_counter = 0

    for tier in (1, 2, 3, 4):
        for lane_idx, rel in enumerate(tier_groups[tier]):
            from_id = rel["from_render"]
            to_id = rel["to_render"]

            if from_id not in real_geom or to_id not in real_geom:
                continue

            from_sid = rel["from_sid"]
            to_sid = rel["to_sid"]

            from_box = final_layout["source_boxes"][from_sid]
            to_box = final_layout["source_boxes"][to_sid]

            from_geom = real_geom[from_id]
            to_geom = real_geom[to_id]

            lane_y = int(lane_start_y[tier] + lane_idx * int(cfg["lane_gap"]))

            from_left_to_right = source_index[from_sid] < source_index[to_sid]

            if from_left_to_right:
                from_side = "right"
                to_side = "left"
                from_port_x = int(from_box["right"] + cfg["port_offset"])
                to_port_x = int(to_box["left"] - cfg["port_offset"])
                from_router_x = int(from_port_x + cfg["router_offset"])
                to_router_x = int(to_port_x - cfg["router_offset"])
            else:
                from_side = "left"
                to_side = "right"
                from_port_x = int(from_box["left"] - cfg["port_offset"])
                to_port_x = int(to_box["right"] + cfg["port_offset"])
                from_router_x = int(from_port_x - cfg["router_offset"])
                to_router_x = int(to_port_x + cfg["router_offset"])

            from_slot = port_slot_counts[(from_id, from_side)]
            port_slot_counts[(from_id, from_side)] += 1

            to_slot = port_slot_counts[(to_id, to_side)]
            port_slot_counts[(to_id, to_side)] += 1

            from_port_y = int(from_geom["cy"] + ((from_slot % 9) - 4) * int(cfg["port_slot_gap"]))
            to_port_y = int(to_geom["cy"] + ((to_slot % 9) - 4) * int(cfg["port_slot_gap"]))

            edge_counter += 1
            port_from_id = f"port::{edge_counter}::from"
            port_to_id = f"port::{edge_counter}::to"
            router_from_id = f"router::{edge_counter}::from"
            router_to_id = f"router::{edge_counter}::to"

            final_layout["nodes"][port_from_id] = (from_port_x, from_port_y)
            final_layout["nodes"][port_to_id] = (to_port_x, to_port_y)
            final_layout["nodes"][router_from_id] = (from_router_x, lane_y)
            final_layout["nodes"][router_to_id] = (to_router_x, lane_y)

            final_layout["node_kind"][port_from_id] = "port"
            final_layout["node_kind"][port_to_id] = "port"
            final_layout["node_kind"][router_from_id] = "router"
            final_layout["node_kind"][router_to_id] = "router"

            final_layout["node_source"][port_from_id] = from_sid
            final_layout["node_source"][port_to_id] = to_sid

            vertical_from_side = "top" if lane_y < from_port_y else "bottom"
            vertical_to_side = "top" if lane_y < to_port_y else "bottom"
            vertical_from_router_side = "bottom" if lane_y < from_port_y else "top"
            vertical_to_router_side = "bottom" if lane_y < to_port_y else "top"

            final_layout["edges"].append(
                {
                    "type": "relates_to",
                    "from": from_id,
                    "to": port_from_id,
                    "style": "dashed",
                    "scope": "cross_source",
                    "fromSide": from_side,
                    "toSide": "left" if from_side == "right" else "right",
                }
            )
            final_layout["edges"].append(
                {
                    "type": "relates_to",
                    "from": port_from_id,
                    "to": router_from_id,
                    "style": "dashed",
                    "scope": "cross_source",
                    "fromSide": vertical_from_side,
                    "toSide": vertical_from_router_side,
                }
            )
            final_layout["edges"].append(
                {
                    "type": "relates_to",
                    "from": router_from_id,
                    "to": router_to_id,
                    "style": "dashed",
                    "scope": "cross_source",
                    "fromSide": "right" if from_router_x < to_router_x else "left",
                    "toSide": "left" if from_router_x < to_router_x else "right",
                }
            )
            final_layout["edges"].append(
                {
                    "type": "relates_to",
                    "from": router_to_id,
                    "to": port_to_id,
                    "style": "dashed",
                    "scope": "cross_source",
                    "fromSide": vertical_to_router_side,
                    "toSide": vertical_to_side,
                }
            )
            final_layout["edges"].append(
                {
                    "type": "relates_to",
                    "from": port_to_id,
                    "to": to_id,
                    "style": "dashed",
                    "scope": "cross_source",
                    "fromSide": "right" if to_side == "left" else "left",
                    "toSide": to_side,
                }
            )

    return final_layout


def layout_concept_deep_dive(graph: dict, center_concept_id: str, *, k_related: int = 12) -> dict:
    """
    Concept deep dive:
    - Center: concept card
    - Ring1: related nodes
    """
    cfg = LAYOUT["concept"]
    pos = {"nodes": {}, "edges": []}

    center = (cfg["cx"], cfg["cy"])
    pos["nodes"][center_concept_id] = center

    rel = []
    for e in graph["global_edges"]["relates_to"]:
        if e["from"] == center_concept_id:
            rel.append((e["to"], float(e.get("strength") or 0.0)))

    rel.sort(key=lambda x: (-x[1], x[0]))
    rel_ids = [rid for rid, _ in rel[:k_related]]

    ring = radial_positions(center, cfg["r1"], len(rel_ids))
    for (x, y), rid in zip(ring, rel_ids):
        pos["nodes"][rid] = (x, y)
        pos["edges"].append(("relates_to", center_concept_id, rid))

    return pos
