import json
import random
import re

def extract_links(text):
    # This searches for ANY line starting with "Layer 4", "Related", or "Connections"
    pattern = re.compile(r"(?:LAYER 4|Related|Connections).*?:\s*(.*)", re.IGNORECASE)
    match = pattern.search(text)
    
    if match:
        # Split by commas, semicolons, or newlines to be safe
        raw_list = re.split(r'[,;\n]', match.group(1))
        return [item.strip().strip('-*•').strip() for item in raw_list if len(item.strip()) > 1]
    return []

def extract_root(text):
    match = re.search(r"(?:ROOT NODE|Idea).*?:\s*([^(\n]*)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None

def convert():
    nodes = []
    edges = []
    seen_nodes = set()
    edge_id = 0

    print("Aggressively weaving 4,945 connections...")

    with open("raw_brain_results.jsonl", "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            try:
                data = json.loads(line)
                content = data['response']['body']['choices'][0]['message']['content']
                
                root = extract_root(content) or f"Note_{i}"
                related_nodes = extract_links(content)

                # Create the Source Node
                if root not in seen_nodes:
                    nodes.append({
                        "key": root,
                        "attributes": {"label": root, "x": random.random(), "y": random.random(), "size": 8, "color": "#38bdf8"}
                    })
                    seen_nodes.add(root)

                # Create Edges to all Related Nodes
                for target in related_nodes:
                    if target not in seen_nodes:
                        nodes.append({
                            "key": target,
                            "attributes": {"label": target, "x": random.random(), "y": random.random(), "size": 4, "color": "#94a3b8"}
                        })
                        seen_nodes.add(target)
                    
                    edges.append({
                        "key": f"e_{edge_id}",
                        "source": root,
                        "target": target,
                        "attributes": {"color": "#64748b", "size": 1} # Visible grey lines
                    })
                    edge_id += 1
            except Exception:
                continue

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump({"nodes": nodes, "edges": edges}, f)
    
    print(f"✅ Success! Weaved {len(nodes)} nodes and {len(edges)} connections.")

if __name__ == "__main__":
    convert()