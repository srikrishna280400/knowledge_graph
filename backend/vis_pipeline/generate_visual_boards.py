# generate_visual_boards.py

from __future__ import annotations
import argparse
from pathlib import Path
from .parse_graph_store_to_visual_graph import assemble_visual_graph_from_graph_store
from .layout_engine import (
    layout_source_board,
    layout_overview_radial_sources,  # NEW: Use radial clustering layout
    layout_concept_deep_dive,
)
from .export_to_canvas import (
    export_source_board,
    export_overview_board,
    export_concept_deep_dive_board,
)

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Path to graph_store.sqlite")
    ap.add_argument("--vault", required=True, help="Path to Obsidian vault root")
    ap.add_argument("--root", default="KG_CANVAS", help="Folder inside vault for generated output")

    # Purpose: remove practical export limits
    ap.add_argument("--max-sources", type=int, default=9999999999999999, help="How many per-source boards to generate")
    ap.add_argument("--max-domains", type=int, default=9999999999999999, help="How many domains on overview")
    ap.add_argument("--max-themes-per-domain", type=int, default=9999999999999999, help="How many themes per domain on overview")
    ap.add_argument("--max-concept-boards", type=int, default=9999999999999999, help="How many concept deep-dive boards")

    ap.add_argument("--min-related", type=float, default=0.78, help="Min strength for relates_to edges pulled from DB")
    ap.add_argument("--min-related-draw", type=float, default=0.80, help="Min strength actually drawn on concept boards")
    return ap.parse_args()

def main():
    args = parse_args()
    db_path = Path(args.db)
    vault_root = Path(args.vault)
    root_folder = args.root

    if not db_path.exists():
        raise FileNotFoundError(f"Missing DB: {db_path}")
    if not vault_root.exists():
        raise FileNotFoundError(f"Missing vault: {vault_root}")

    graph = assemble_visual_graph_from_graph_store(db_path, min_related_strength=float(args.min_related))

    # NEW: Use radial clustering overview layout
    print("Generating radial clustering overview layout...")
    ov_layout = layout_overview_radial_sources(graph)
    export_overview_board(vault_root, root_folder, graph, ov_layout)

    # Purpose: export all source boards
    sources_sorted = sorted(
        graph["sources"],
        key=lambda s: ((s.get("created_at") or ""), (s.get("url") or "")),
        reverse=True,
    )
    picked = [s["db_source_id"] for s in sources_sorted if s["db_source_id"] in graph["per_source"]][: int(args.max_sources)]

    for sid in picked:
        lay = layout_source_board(graph, sid)
        export_source_board(vault_root, root_folder, graph, sid, lay)

    # Purpose: export all tier-3 concept boards instead of top 25 only
    concepts = [n for n in graph["nodes"].values() if int(n.get("tier") or 0) == 3]
    concepts.sort(key=lambda n: (-int(n.get("doc_freq") or 0), n["name"].lower(), n["id"]))
    top = concepts[: int(args.max_concept_boards)]

    for c in top:
        lay = layout_concept_deep_dive(graph, c["id"], k_related=12)
        export_concept_deep_dive_board(
            vault_root,
            root_folder,
            graph,
            c["id"],
            lay,
            min_strength=float(args.min_related_draw),
            max_related=12,
        )

    print("OK: wrote generated canvases + notes into vault")
    print(f"Open in Obsidian: {root_folder}/Boards/00_Overview.canvas")
    print(f"Per-source boards: {root_folder}/Boards/Sources/")
    print(f"Concept boards: {root_folder}/Boards/Concepts/")

if __name__ == "__main__":
    main()