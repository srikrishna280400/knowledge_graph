import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "kg.sqlite"

def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    print("\nSTATUS COUNTS")
    cur.execute("select status, count(*) from saved_items group by status order by count(*) desc")
    for status, cnt in cur.fetchall():
        print(f"{status:20s} {cnt}")

    print("\nTOP 10 BY TEXT LENGTH")
    cur.execute("""
        select status, substr(title, 1, 80), length(extracted_text) as n
        from saved_items
        where extracted_text is not null
        order by n desc
        limit 10
    """)
    for status, title, n in cur.fetchall():
        print(f"{status:14s} chars={n:6d}  title={title}")

    print("\nSAMPLE FAILURES (up to 10)")
    cur.execute("""
        select status, canonical_url
        from saved_items
        where status in ('crawl_failed','extraction_failed','title_only')
        limit 10
    """)
    for status, url in cur.fetchall():
        print(f"{status:14s} {url}")

    con.close()

if __name__ == "__main__":
    main()
