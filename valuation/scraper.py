"""Scraper Sreality.cz - stahuje inzeráty přes veřejné JSON API a ukládá do SQLite.

Použití:
    python scraper.py                        # výchozí: byty na prodej, Jihočeský kraj
    python scraper.py --region-id 12         # explicitně Jihočeský
    python scraper.py --category-main 2      # domy místo bytů
    python scraper.py --max-pages 50         # omezit počet stránek

Region IDs (locality_region_id):
    10=Praha, 11=Středočeský, 12=Jihočeský, 13=Plzeňský, 14=Karlovarský,
    15=Ústecký, 16=Liberecký, 17=Královéhradecký, 18=Pardubický, 19=Vysočina,
    20=Jihomoravský, 21=Olomoucký, 22=Zlínský, 23=Moravskoslezský
"""
import argparse
import json
import logging
import time
from datetime import datetime, timezone

import requests

from db import get_conn, init_db, upsert_listing, count_listings

API_URL = "https://www.sreality.cz/api/cs/v2/estates"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

DEFAULT_REGION_ID = 12  # Jihočeský kraj

log = logging.getLogger("scraper")


def fetch_page(session: requests.Session, *, category_main: int, category_type: int,
               page: int, per_page: int, region_id: int | None) -> dict:
    params = {
        "category_main_cb": category_main,
        "category_type_cb": category_type,
        "page": page,
        "per_page": per_page,
    }
    if region_id is not None:
        params["locality_region_id"] = region_id
    resp = session.get(API_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def extract_estate(estate: dict, *, category_main: int, category_type: int,
                   region_id: int | None) -> tuple | None:
    hash_id = estate.get("hash_id")
    if hash_id is None:
        return None

    gps = estate.get("gps") or {}
    seo = estate.get("seo") or {}
    links = estate.get("_links") or {}
    images = links.get("images") or []
    image_url = images[0].get("href") if images else None

    return (
        str(hash_id),
        "sreality",
        estate.get("name"),
        estate.get("locality"),
        estate.get("price"),
        "CZK",
        category_main,
        seo.get("category_sub_cb"),
        category_type,
        region_id,
        gps.get("lat"),
        gps.get("lon"),
        image_url,
        json.dumps(estate, ensure_ascii=False),
        datetime.now(timezone.utc).isoformat(),
    )


def scrape(*, category_main: int, category_type: int, max_pages: int,
           per_page: int, delay: float, region_id: int | None) -> int:
    init_db()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    total_seen = 0
    with get_conn() as conn:
        for page in range(1, max_pages + 1):
            try:
                data = fetch_page(
                    session,
                    category_main=category_main,
                    category_type=category_type,
                    page=page,
                    per_page=per_page,
                    region_id=region_id,
                )
            except requests.RequestException as e:
                log.warning("Stránka %d selhala (%s) – pauza 5s a pokračuji", page, e)
                time.sleep(5)
                continue

            estates = (data.get("_embedded") or {}).get("estates") or []
            if not estates:
                log.info("Konec dat na stránce %d", page)
                break

            for estate in estates:
                row = extract_estate(
                    estate,
                    category_main=category_main,
                    category_type=category_type,
                    region_id=region_id,
                )
                if row is not None:
                    upsert_listing(conn, row)
                    total_seen += 1

            conn.commit()
            log.info("Strana %d: %d záznamů (celkem zpracováno: %d)",
                     page, len(estates), total_seen)
            time.sleep(delay)

        total_in_db = count_listings(conn, region_id=region_id)
        log.info("Hotovo. Záznamů v DB pro region %s: %d", region_id, total_in_db)

    return total_seen


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description="Sreality scraper -> SQLite")
    p.add_argument("--category-main", type=int, default=1,
                   help="1=byty, 2=domy, 3=pozemky (výchozí: 1)")
    p.add_argument("--category-type", type=int, default=1,
                   help="1=prodej, 2=pronájem (výchozí: 1)")
    p.add_argument("--region-id", type=int, default=DEFAULT_REGION_ID,
                   help=f"locality_region_id (výchozí: {DEFAULT_REGION_ID} = Jihočeský)")
    p.add_argument("--max-pages", type=int, default=500,
                   help="Maximální počet stránek (výchozí: 500)")
    p.add_argument("--per-page", type=int, default=60,
                   help="Záznamů na stránku (výchozí: 60)")
    p.add_argument("--delay", type=float, default=1.0,
                   help="Pauza mezi requesty v sekundách (výchozí: 1.0)")
    p.add_argument("--all-regions", action="store_true",
                   help="Ignorovat region a stáhnout celou ČR")
    args = p.parse_args()

    region_id = None if args.all_regions else args.region_id
    scrape(
        category_main=args.category_main,
        category_type=args.category_type,
        max_pages=args.max_pages,
        per_page=args.per_page,
        delay=args.delay,
        region_id=region_id,
    )


if __name__ == "__main__":
    main()
