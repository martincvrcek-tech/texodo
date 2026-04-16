"""SQLite úložiště pro stažené inzeráty."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id              TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    title           TEXT,
    locality        TEXT,
    price           INTEGER,
    price_currency  TEXT,
    category_main   INTEGER,
    category_sub    INTEGER,
    category_type   INTEGER,
    region_id       INTEGER,
    gps_lat         REAL,
    gps_lon         REAL,
    image_url       TEXT,
    raw_json        TEXT,
    scraped_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_category ON listings(category_main, category_type);
CREATE INDEX IF NOT EXISTS idx_region   ON listings(region_id);
CREATE INDEX IF NOT EXISTS idx_scraped  ON listings(scraped_at);
"""

UPSERT_SQL = """
INSERT INTO listings (
    id, source, title, locality, price, price_currency,
    category_main, category_sub, category_type, region_id,
    gps_lat, gps_lon, image_url, raw_json, scraped_at
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
    price       = excluded.price,
    title       = excluded.title,
    locality    = excluded.locality,
    raw_json    = excluded.raw_json,
    scraped_at  = excluded.scraped_at
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def upsert_listing(conn: sqlite3.Connection, row: tuple) -> None:
    conn.execute(UPSERT_SQL, row)


def count_listings(conn: sqlite3.Connection, region_id: int | None = None) -> int:
    if region_id is None:
        cur = conn.execute("SELECT COUNT(*) FROM listings")
    else:
        cur = conn.execute("SELECT COUNT(*) FROM listings WHERE region_id = ?", (region_id,))
    return cur.fetchone()[0]
