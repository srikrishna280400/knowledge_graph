import json
from sqlalchemy import text
from migration.config import PRIMARY, KG_90, KG90_SEED_OUTPUT_JSONL
from migration.db_factory import get_engine

OUT90_PATH = KG90_SEED_OUTPUT_JSONL
eng_kg = get_engine(PRIMARY)
eng_kg90 = get_engine(KG_90)

def main():
    if not OUT90_PATH.exists():
        raise FileNotFoundError(f"Missing: {OUT90_PATH}")

    with eng_kg.begin() as conn_kg, eng_kg90.connect() as conn_kg90:
        # Ensure table exists in kg.sqlite
        conn_kg.execute(text("""
        CREATE TABLE IF NOT EXISTS llm_processed (
            saved_item_id TEXT PRIMARY KEY,
            processed_at TEXT,
            source TEXT
        );
        """))

        total = 0
        inserted = 0
        skipped_no_canon = 0
        skipped_no_kg_match = 0

        for line in OUT90_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue

            total += 1
            obj = json.loads(line)

            kg90_id = (obj.get("id") or "").strip()
            if not kg90_id:
                continue

            # Step A: kg_90 id -> canonical_url (from kg_90.sqlite)
            canon = conn_kg90.execute(
                text("SELECT canonical_url FROM saved_items WHERE id = :id LIMIT 1"),
                {"id": kg90_id},
            ).scalar()

            if not canon:
                skipped_no_canon += 1
                continue

            # Step B: canonical_url -> kg.sqlite saved_items.id
            kg_id = conn_kg.execute(
                text("SELECT id FROM saved_items WHERE canonical_url = :u LIMIT 1"),
                {"u": canon},
            ).scalar()

            if not kg_id:
                skipped_no_kg_match += 1
                continue

            # Step C: mark processed in kg.sqlite
            conn_kg.execute(
                text("""
                INSERT OR IGNORE INTO llm_processed (saved_item_id, processed_at, source)
                VALUES (:id, datetime('now'), :source)
                """),
                {"id": kg_id, "source": str(OUT90_PATH.name)},
            )
            inserted += 1

        print(
            f"OK: scanned={total} inserted_or_ignored={inserted} "
            f"skipped_no_canon={skipped_no_canon} skipped_no_kg_match={skipped_no_kg_match}"
        )


if __name__ == "__main__":
    main()
