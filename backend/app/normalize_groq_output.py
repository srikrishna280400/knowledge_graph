# normalize_groq_output.py

import json
import hashlib
from pathlib import Path
import re
import argparse

def stable_source_node_id(canonical_url: str) -> str:
    """Source node = one per URL"""
    h = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()
    return f"source_{h}"

def _norm_concept_name(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)                 # collapse whitespace
    s = re.sub(r"[^\w\s\-/+.#]", "", s)        # drop most punctuation, keep useful chars
    return s.strip()

def stable_concept_node_id(concept_type: str, concept_name: str) -> str | None:
    """Concept node = stable across URLs"""
    clean_type = (concept_type or "Concept").strip().lower()
    clean_name = _norm_concept_name(concept_name)
    
    if not clean_name:
        return None
    
    identity = f"{clean_type}||{clean_name}"
    h = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
    return f"concept_{clean_type}_{h}"

def extract_assistant_json_text(obj: dict) -> str | None:
    s = obj.get("assistant_content")
    if isinstance(s, str) and s.strip():
        return s.strip()
    
    raw = obj.get("raw")
    if isinstance(raw, dict):
        choices = raw.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                s2 = msg.get("content")
                if isinstance(s2, str) and s2.strip():
                    return s2.strip()
    
    return None

def try_parse_json(s: str | None) -> tuple[dict | None, str | None]:
    if not s:
        return None, "missing_assistant_content"
    try:
        return json.loads(s), None
    except Exception as e:
        return None, f"json_loads_failed: {type(e).__name__}: {e}"
    
def _as_confidence(x):
    """Return float in [0,1] else None (unknown)."""
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if 0.0 <= v <= 1.0:
        return v
    return None

def extract_concepts_from_cognitive_map(cognitive_map: dict | None) -> list[dict]:
    """Extract all concepts/themes/domains from cognitive map"""
    if not isinstance(cognitive_map, dict):
        return []

    concepts = []

    # Domains (tier 1)
    domains = cognitive_map.get("domains") or []
    if isinstance(domains, list):
        for d in domains:
            if isinstance(d, dict) and d.get("name"):
                concepts.append({
                    "type": "Domain",
                    "name": d["name"],
                    "tier": 1,
                    "confidence": _as_confidence(d.get("confidence")),
                })

    # Themes (tier 2)
    themes = cognitive_map.get("themes") or []
    if isinstance(themes, list):
        for t in themes:
            if isinstance(t, dict) and t.get("name"):
                concepts.append({
                    "type": t.get("type", "Theme"),
                    "name": t["name"],
                    "tier": 2,
                    "confidence": _as_confidence(t.get("confidence")),
                })

    # Concepts (tier 3)
    concept_list = cognitive_map.get("concepts") or []
    if isinstance(concept_list, list):
        for c in concept_list:
            if isinstance(c, dict) and c.get("name"):
                concepts.append({
                    "type": c.get("type", "Concept"),
                    "name": c["name"],
                    "tier": 3,
                    "confidence": _as_confidence(c.get("confidence")),
                })

    # Micro-insights (tier 4)
    micro = cognitive_map.get("micro_insights") or []
    if isinstance(micro, list):
        for m in micro:
            if isinstance(m, dict) and m.get("text"):
                concepts.append({
                    "type": "Insight",
                    "name": m["text"][:100],
                    "tier": 4,
                    "confidence": _as_confidence(m.get("confidence")),
                })

    return concepts

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--in", dest="in_path", required=True, help="Path to Groq output JSONL")
    return p.parse_args()

def main():
    args = parse_args()
    in_path = Path(args.in_path)
    if not in_path.is_absolute():
        in_path = Path(__file__).resolve().parents[1] / in_path
    out_path = in_path.with_name(in_path.stem + ".normalized.jsonl")
    
    total = 0
    bad = 0
    
    with in_path.open("r", encoding="utf-8") as f_in, out_path.open("w", encoding="utf-8") as f_out:
        for line in f_in:
            line = line.strip()
            if not line:
                continue
            
            total += 1
            obj = json.loads(line)
            http_status = obj.get("http_status")
            parsed = obj.get("parsed")
            
            canonical_url = (obj.get("canonical_url") or "").strip()
            if not canonical_url:
                bad += 1
                continue

            if http_status is None or int(http_status) >= 400 or parsed is None:
                bad += 1
                continue
            
            source_node_id = stable_source_node_id(canonical_url)
            
            assistant_text = extract_assistant_json_text(obj)
            cognitive_map, parse_err = try_parse_json(assistant_text)
            
            label = None
            if isinstance(cognitive_map, dict):
                root = cognitive_map.get("root")
                if isinstance(root, dict):
                    label = root.get("label")
            
            # Extract concepts and create concept nodes
            concepts = extract_concepts_from_cognitive_map(cognitive_map)
            concept_nodes = []
            
            for concept in concepts:
                concept_id = stable_concept_node_id(concept["type"], concept["name"])
                if concept_id:
                    concept_nodes.append({
                        "id": concept_id,
                        "type": concept["type"],
                        "name": concept["name"],
                        "tier": concept["tier"],
                        "confidence": concept["confidence"]
                    })
            
            out_obj = {
                "id": obj.get("id"),
                
                "source_node": {
                    "id": source_node_id,
                    "name": label or canonical_url,
                    "canonical_url": canonical_url,
                    "type": "source"
                },
                
                "concept_nodes": concept_nodes,
                
                "groq_output": obj,
                "cognitive_map": cognitive_map,
            }
            
            if parse_err:
                out_obj["normalize_warning"] = parse_err
                bad += 1
            
            f_out.write(json.dumps(out_obj, ensure_ascii=False) + "\n")
    
    print(f"Wrote: {out_path}")
    print(f"Rows: {total}, warnings/failed-parse: {bad}")

if __name__ == "__main__":
    main()
