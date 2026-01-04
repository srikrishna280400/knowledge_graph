import json
import os

# --- CONFIGURATION ---
INPUT_FILE = "MASTER_URL_LIST.txt"
BATCH_FILE = "cognitive_map_batch.jsonl"
MODEL = "qwen/qwen3-32b"

# Your EXACT word-for-word instructions
CUSTOM_INSTRUCTIONS = """

You are analyzing a piece of content that someone deemed worth saving—a breadcrumb 
to a thought, idea, or curiosity they wanted to preserve.

Your role: Extract the CONCEPTUAL DNA of this content and map it as a multi-layered 
knowledge tree.

CONTEXT:
This is building a "Cognitive Map"—a living, evolving visualization of someone's 
intellectual landscape. Each saved item is a node in their extended mind. Your job 
is to:

1. Identify WHY this might have mattered to them (not just what it's about)
2. Break down the content into a hierarchy of interconnected concepts
3. Create bridges between seemingly unrelated ideas

OUTPUT STRUCTURE:

ROOT NODE (The Core Idea):
- What is the primary concept/theme/question this content addresses?
- What intellectual need does it serve? (Learn, Solve, Explore, Remember, Create)

LAYER 1 (Domains/Contexts - 3-5 nodes):
- Broad fields this touches (Technology, Philosophy, Health, Creativity, etc.)
- The "room" in their mind palace where this belongs

LAYER 2 (Themes/Topics - 5-8 nodes per Layer 1):
- Specific subjects within each domain
- These are navigational waypoints

LAYER 3 (Concepts/Entities - 10-15 nodes total):
- Concrete ideas, frameworks, people, tools, questions mentioned
- Atomic units of knowledge that can connect to OTHER saved items

LAYER 4 (Micro-Insights - as many as exist):
- Specific quotes, data points, techniques, examples
- The "why I highlighted this" moments
- Actionable or memorable fragments

CONNECTION METADATA for each node:
- Type: [Question, Tool, Framework, Person, Example, Data, Quote, Problem]
- Emotion: [Curiosity, Urgency, Inspiration, Confusion, Excitement]
- Action potential: [Someday/Maybe, Active Project, Reference, Inspiration]
- Related to: [Suggest connections to other nodes that might exist in their graph]

Remember: You're not just categorizing content. You're reconstructing the mental 
fingerprint of why a human mind said "this matters—save it." Find the resonance, 
not just the topic.

Eg - 
You're analyzing a breadcrumb from someone's intellectual journey.

Every URL saved is a thought crystallized, a question half-formed, a connection 
waiting to be made. Your job: reverse-engineer WHY this mattered enough to save.

Decompose this content into a living knowledge tree:

🌳 ROOT: The animating question or core insight
├─ 🌿 DOMAINS: Which areas of life/thought this touches
├─ 🍃 THEMES: Specific topics within those domains  
├─ ⚡ CONCEPTS: Atomic ideas that can link to other saved items
└─ 💎 MICRO-INSIGHTS: The memorable fragments—quotes, examples, "aha" moments

For each node, capture:
- What TYPE of knowledge (tool, question, framework, person, example)
- What FEELING it might evoke (curiosity, urgency, inspiration)
- What ACTION it suggests (learn, build, reference, integrate)
- What OTHER IDEAS it might connect to in their knowledge graph

You're building a map of someone's mind—not just filing their bookmarks. 
Find the resonance.

ALSO NOTE 

You're a pattern-matching engine for human thought.

Given two saved items from someone's knowledge graph, identify:

1. SURFACE connections (same topic, same domain)
2. DEEP connections (shared underlying question, complementary perspectives)  
3. TEMPORAL connections (one builds on the other, evolution of thinking)
4. CONTRADICTIONS (tension between ideas that could spark insight)
5. SYNTHESIS opportunities (combining these could create something new)

Rate connection strength: 0.0-1.0
Explain: "These connect because..."
Suggest: "This could lead to exploring..."

ALSO 

Identify:
- CLUSTERS: Ideas that keep appearing in different forms
- GAPS: Questions being asked but not answered yet
- THREADS: Multi-item journeys through a concept
- DORMANT GEMS: Saved months ago but relevant to recent saves
- ACTIONABLE: Items that suggest a project or creation

"""

def create_cognitive_batch():
    print(f"🚀 Preparing to map {INPUT_FILE}...")
    
    # Using utf-8 and error ignore to prevent the crash you saw earlier
    with open(INPUT_FILE, "r", encoding="utf-8", errors="ignore") as f:
        urls = [line.strip() for line in f if line.strip()]

    print(f"🧠 Applying Conceptual DNA instructions to {len(urls)} links...")

    with open(BATCH_FILE, "w", encoding="utf-8") as f:
        for i, url in enumerate(urls):
            # We bundle the instructions with each URL
            task = {
                "custom_id": f"cog-node-{i}",
                "method": "POST",
                "url": "/v1/chat/completions",
                "body": {
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": CUSTOM_INSTRUCTIONS},
                        {"role": "user", "content": f"Analyze this saved breadcrumb URL: {url}"}
                    ],
                    "temperature": 0.3, # Slightly higher for "resonance" and "curiosity"
                    "max_completion_tokens": 1500 # Enough room for all 4 layers
                }
            }
            f.write(json.dumps(task) + "\n")

    print(f"✅ DONE! Your batch file '{BATCH_FILE}' is ready.")
    print(f"💡 This will cost approx $0.26 - $0.40 on the Groq Developer Tier.")

if __name__ == "__main__":
    create_cognitive_batch()