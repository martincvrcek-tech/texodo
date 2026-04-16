"""Scraper Bazos.cz (reality.bazos.cz) - parsuje HTML listingy a ukládá do SQLite.

Použití:
    python bazos.py                              # výchozí: byty na prodej, ČB + okolí 100km
    python bazos.py --hlokalita 37001 --okruh 50
    python bazos.py --rubrika prodej-dum         # domy
    python bazos.py --max-pages 50

Výchozí filtr je centrum České Budějovice (PSČ 37001) + okolí 100 km
→ v podstatě pokrývá celý Jihočeský kraj.

Při ukládání se PSČ vytahuje z lokality regexem; sloupec region_id
se naplní 12, pokud PSČ začíná na "37" nebo "38" (Jihočeský), jinak NULL.
"""
import argparse
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from db import get_conn, init_db, upsert_listing, count_listings

BASE_URL = "https://reality.bazos.cz"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Mapování bazos rubrika -> (category_main, category_type) pro DB
RUBRIKA_MAP = {
    "prodej-byt":    (1, 1),
    "pronajem-byt":  (1, 2),
    "prodej-dum":    (2, 1),
    "pronajem-dum":  (2, 2),
    "prodej-pozemek":(3, 1),
}

PSC_RE   = re.compile(r"\b(\d{3})\s?(\d{2})\b")
PRICE_RE = re.compile(r"([\d\s\u00a0]+)\s*Kč")
ID_RE    = re.compile(r"/inzerat/(\d+)/")

log = logging.getLogger("bazos")


def build_url(rubrika: str, page_offset: int, hlokalita: str | None,
              okruh: int | None) -> str:
    """Sestaví URL listing stránky.

    Bazos rubriky používají vlastní cestu: /prodej/byt/, /prodej/dum/, ...
    Stránkování: ke konci cesty se přidá /20/, /40/, /60/ ...
    """
    rubrika_path = rubrika.replace("-", "/")  # 'prodej-byt' -> 'prodej/byt'
    path = f"/{rubrika_path}/"
    if page_offset:
        path += f"{page_offset}/"

    qs = []
    if hlokalita:
        qs.append(f"hlokalita={hlokalita}")
    if okruh is not None:
        qs.append(f"humkreis={okruh}")
    if qs:
        qs.append("Submit=Hledat")
        path += "?" + "&".join(qs)
    return urljoin(BASE_URL, path)


def parse_price(text: str) -> int | None:
    """Vyparsuje cenu z textu typu '1 234 567 Kč'."""
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return int(digits) if digits else None


def parse_psc(text: str) -> str | None:
    """Vrátí PSČ ve formátu '37001' (bez mezery), pokud je v textu."""
    if not text:
        return None
    m = PSC_RE.search(text)
    if not m:
        return None
    return m.group(1) + m.group(2)


def region_from_psc(psc: str | None) -> int | None:
    """PSČ -> region_id. Pro MVP řešíme jen Jihočeský (12)."""
    if not psc:
        return None
    if psc.startswith(("37", "38")):
        return 12
    return None


def parse_listing_page(html: str) -> list[dict]:
    """Vrátí seznam inzerátů z jedné listing stránky bazosu."""
    soup = BeautifulSoup(html, "html.parser")
    items = []
    # Bazos používá div.inzeraty (každý inzerát je v takovém divu)
    for box in soup.select("div.inzeraty"):
        link_tag = box.select_one("h2.nadpis a") or box.select_one("a[href*='/inzerat/']")
        if not link_tag or not link_tag.get("href"):
            continue
        href = link_tag["href"]
        m = ID_RE.search(href)
        if not m:
            continue
        item = {
            "id": m.group(1),
            "url": urljoin(BASE_URL, href),
            "title": link_tag.get_text(strip=True),
        }

        lok = box.select_one(".inzeratylok")
        item["locality"] = lok.get_text(" ", strip=True) if lok else None
        item["psc"] = parse_psc(item["locality"] or "")

        cena = box.select_one(".inzeratycena")
        item["price"] = parse_price(cena.get_text(" ", strip=True) if cena else "")
        item["price_text"] = cena.get_text(" ", strip=True) if cena else None

        text = box.select_one(".inzeratytext")
        item["description"] = text.get_text(" ", strip=True) if text else None

        items.append(item)
    return items


def fetch_page(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def to_db_row(item: dict, *, rubrika: str) -> tuple:
    cat_main, cat_type = RUBRIKA_MAP.get(rubrika, (None, None))
    psc = item.get("psc")
    raw = {
        "url": item.get("url"),
        "title": item.get("title"),
        "locality": item.get("locality"),
        "psc": psc,
        "price": item.get("price"),
        "price_text": item.get("price_text"),
        "description": item.get("description"),
    }
    import json
    return (
        f"bazos:{item['id']}",
        "bazos",
        item.get("title"),
        item.get("locality"),
        item.get("price"),
        "CZK",
        cat_main,
        None,                    # bazos nemá strukturovanou subkategorii
        cat_type,
        region_from_psc(psc),
        None, None,              # GPS – z listing page nedostupné
        None,                    # image – řešíme později
        json.dumps(raw, ensure_ascii=False),
        datetime.now(timezone.utc).isoformat(),
    )


def scrape(*, rubrika: str, hlokalita: str | None, okruh: int | None,
           max_pages: int, delay: float, page_size: int = 20) -> int:
    init_db()
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "cs-CZ,cs;q=0.9",
    })

    total = 0
    with get_conn() as conn:
        for page_idx in range(max_pages):
            offset = page_idx * page_size
            url = build_url(rubrika, offset, hlokalita, okruh)
            try:
                html = fetch_page(session, url)
            except requests.RequestException as e:
                log.warning("Stránka %s selhala (%s) – pauza 5s", url, e)
                time.sleep(5)
                continue

            items = parse_listing_page(html)
            if not items:
                log.info("Žádné další inzeráty (offset=%d). Konec.", offset)
                break

            for item in items:
                upsert_listing(conn, to_db_row(item, rubrika=rubrika))
                total += 1
            conn.commit()

            jc = sum(1 for it in items if region_from_psc(it.get("psc")) == 12)
            log.info("offset=%d: %d záznamů (z toho jihočeských: %d, celkem: %d)",
                     offset, len(items), jc, total)
            time.sleep(delay)

        in_db = count_listings(conn, region_id=12)
        log.info("Hotovo. Záznamů v DB pro Jihočeský kraj (region_id=12): %d", in_db)
    return total


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Bazos reality scraper -> SQLite")
    p.add_argument("--rubrika", default="prodej-byt",
                   choices=list(RUBRIKA_MAP.keys()),
                   help="Co stahovat (výchozí: prodej-byt)")
    p.add_argument("--hlokalita", default="37001",
                   help="PSČ centra hledání (výchozí 37001 = České Budějovice)")
    p.add_argument("--okruh", type=int, default=100,
                   help="Okruh v km (výchozí: 100, pokryje celý Jihočeský kraj)")
    p.add_argument("--max-pages", type=int, default=200,
                   help="Maximální počet stránek (výchozí: 200, à 20 inzerátů)")
    p.add_argument("--delay", type=float, default=1.5,
                   help="Pauza mezi requesty v sekundách (výchozí: 1.5)")
    args = p.parse_args()

    scrape(
        rubrika=args.rubrika,
        hlokalita=args.hlokalita,
        okruh=args.okruh,
        max_pages=args.max_pages,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
