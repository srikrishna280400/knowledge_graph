import json, random

random.seed(90)  # stable-ish z values each run

with open("data.json", "r", encoding="utf-8") as f:
    g2 = json.load(f)

nodes_out = []
for n in g2.get("nodes", []):
    attrs = n.get("attributes", {})
    nodes_out.append({
        "key": n.get("key"),
        "label": attrs.get("label", n.get("key")),
        "size": attrs.get("size", 1),
        "color": attrs.get("color", "#94a3b8"),
        "x": attrs.get("x", 0),
        "y": attrs.get("y", 0),
        "z": attrs.get("z", random.random()),  # add z if missing
    })

edges_out = []
for e in g2.get("edges", []):
    attrs = e.get("attributes", {})
    edges_out.append({
        "key": e.get("key"),
        "source": e.get("source"),
        "target": e.get("target"),
        "size": attrs.get("size", 1),
        "color": attrs.get("color", "rgba(148,163,184,0.22)"),
        "label": attrs.get("label", ""),
        "edge_type": attrs.get("edge_type", ""),
    })

with open("graph90.json", "w", encoding="utf-8") as f:
    json.dump({"nodes": nodes_out, "edges": edges_out}, f)

print("Wrote graph90.json:", len(nodes_out), "nodes,", len(edges_out), "edges")
