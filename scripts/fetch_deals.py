#!/usr/bin/env python3
"""
Fetches current Steam deals from ITAD API, identifies historical lows,
and writes static JSON to docs/data/deals.json for GitHub Pages.
"""

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
OUTPUT = ROOT / "docs" / "data" / "deals.json"
BACKUP = ROOT / "docs" / "data" / "deals.backup.json"

# ── API constants ───────────────────────────────────────────────────────────────
ITAD_BASE = "https://api.isthereanydeal.com"
STEAM_API = "https://store.steampowered.com/api/appdetails"
ITAD_KEY = os.environ["ITAD_API_KEY"]
STEAM_SHOP_ID = 61  # Steam's numeric shop ID in ITAD

# ── thresholds ─────────────────────────────────────────────────────────────────
MIN_CUT = 40            # only scan deals with ≥40% discount
GOOD_DEAL_CUT = 50      # non-ATL games must have ≥50% discount
GOOD_DEAL_SCORE = 80    # non-ATL games must have ≥80% positive reviews
MIN_ATL_SCORE = 70      # ATL games below this score are still excluded
AAA_MIN_REVIEWS = 10_000
KNOWN_MIN_REVIEWS = 1_000
MAX_PAGES = 20          # 20×100 = up to 2 000 Steam-filtered deals
MAX_MEDIA_GAMES = 50    # fetch Steam screenshots/trailer for top N games
MAX_SCREENSHOTS = 3


# ── HTTP helpers ────────────────────────────────────────────────────────────────

def _itad(method: str, path: str, retries: int = 3, **kwargs) -> object:
    """ITAD API request with retry + back-off. Raises on final failure."""
    url = f"{ITAD_BASE}{path}"
    params = kwargs.pop("params", {})
    params["key"] = ITAD_KEY

    for attempt in range(retries):
        try:
            r = requests.request(method, url, params=params, timeout=30, **kwargs)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                log.warning("Rate-limited; sleeping %ds", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == retries - 1:
                raise
            wait = 2 ** attempt
            log.warning("Request failed (%s); retry in %ds", exc, wait)
            time.sleep(wait)


def _steam_appdetails(appid: int, retries: int = 3) -> dict:
    """Fetch screenshots and movies from Steam store API."""
    for attempt in range(retries):
        try:
            r = requests.get(
                STEAM_API,
                params={"appids": appid, "filters": "screenshots,movies", "l": "english"},
                timeout=20,
            )
            r.raise_for_status()
            entry = r.json().get(str(appid), {})
            if not entry.get("success"):
                return {}
            return entry.get("data", {})
        except Exception as exc:
            if attempt == retries - 1:
                log.warning("Steam appdetails failed for %s: %s", appid, exc)
                return {}
            time.sleep(2 ** attempt)


# ── ITAD calls ──────────────────────────────────────────────────────────────────

def fetch_steam_deals() -> list[dict]:
    """
    Paginate GET /deals/v2 filtered to Steam (shops[]=61).
    ITAD expects the shops param as a repeated key with [] suffix;
    we pass it as a list of tuples so requests encodes it correctly.
    Each returned item is a game object with price info under item["deal"].
    """
    deals: list[dict] = []
    offset = 0
    limit = 100

    for page in range(MAX_PAGES):
        log.info("  /deals/v2 page %d (offset=%d)", page + 1, offset)
        # Pass shops as repeated tuple so requests encodes as shops[]=61
        params = [
            ("key", ITAD_KEY),
            ("country", "US"),
            ("shops[]", STEAM_SHOP_ID),
            ("offset", offset),
            ("limit", limit),
        ]
        url = f"{ITAD_BASE}/deals/v2"
        for attempt in range(3):
            try:
                r = requests.get(url, params=params, timeout=30)
                if r.status_code == 429:
                    wait = int(r.headers.get("Retry-After", 60))
                    log.warning("Rate-limited; sleeping %ds", wait)
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except requests.RequestException as exc:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)

        batch: list[dict] = data.get("deals") or data.get("list") or []
        if not batch:
            log.info("  empty batch (keys: %s)", list(data.keys()))
            break

        if page == 0:
            deal_sample = (batch[0].get("deal") or {}) if batch else {}
            log.info("  deal fields available: %s", list(deal_sample.keys()))

        for item in batch:
            deal = item.get("deal") or {}
            if deal.get("cut", 0) >= MIN_CUT:
                deals.append(item)

        if not data.get("hasMore", False):
            break

        offset += limit
        time.sleep(0.4)

    log.info("Collected %d Steam deals with cut ≥ %d%%", len(deals), MIN_CUT)
    return deals


def fetch_prices_for_country(game_ids: list[str], country: str) -> dict[str, dict]:
    """
    POST /games/prices/v3 in chunks; return dict keyed by game ID.
    Each value: {current, regular, cut, historyLow, currency}
    """
    result: dict[str, dict] = {}
    chunk_size = 100

    for i in range(0, len(game_ids), chunk_size):
        chunk = game_ids[i : i + chunk_size]
        rows: list[dict] = _itad("POST", "/games/prices/v3",
                                  params={"country": country},
                                  json=chunk)
        for row in rows:
            gid = row.get("id")
            if not gid:
                continue

            # Find the Steam deal in this country
            steam_deal = next(
                (d for d in row.get("deals", []) if d.get("shop", {}).get("id") == STEAM_SHOP_ID),
                None,
            )
            history_low_all = (row.get("historyLow") or {}).get("all") or {}

            result[gid] = {
                "current": steam_deal["price"]["amount"] if steam_deal else None,
                "regular": steam_deal["regular"]["amount"] if steam_deal else None,
                "cut": steam_deal.get("cut") if steam_deal else None,
                "low": history_low_all.get("amount"),
                "currency": history_low_all.get("currency"),
            }
        time.sleep(0.3)

    return result


def fetch_game_info(game_id: str) -> dict:
    """GET /games/info/v2 for a single game."""
    return _itad("GET", "/games/info/v2", params={"id": game_id})


# ── classification ──────────────────────────────────────────────────────────────

def get_steam_review(reviews_list: list[dict]) -> dict:
    """Extract the Steam review entry from /games/info/v2 reviews array."""
    for r in reviews_list or []:
        if str(r.get("source", "")).lower() == "steam":
            return r
    return {}


def classify_tier(review_count: int, score: int) -> str:
    if review_count >= AAA_MIN_REVIEWS and score >= MIN_ATL_SCORE:
        return "aaa"
    if review_count >= KNOWN_MIN_REVIEWS and score >= MIN_ATL_SCORE:
        return "known"
    return "other"


def sort_priority(game: dict) -> int:
    is_atl = game["is_atl"]
    tier = game["tier"]
    if is_atl and tier == "aaa":    return 0
    if is_atl and tier == "known":  return 1
    if not is_atl and tier == "aaa":   return 2
    if not is_atl and tier == "known": return 3
    return 4


# ── media ───────────────────────────────────────────────────────────────────────

def fetch_media(appid: int) -> dict:
    """Return {screenshots: [...], trailer: url|None} from Steam appdetails."""
    data = _steam_appdetails(appid)
    screenshots = [
        s["path_full"] for s in (data.get("screenshots") or [])[:MAX_SCREENSHOTS]
    ]

    trailer = None
    movies = data.get("movies") or []
    if movies:
        # Prefer the highlight trailer; fall back to first
        movie = next((m for m in movies if m.get("highlight")), movies[0])
        mp4 = movie.get("mp4") or {}
        webm = movie.get("webm") or {}
        trailer = mp4.get("max") or mp4.get("480") or webm.get("max") or webm.get("480")

    return {"screenshots": screenshots, "trailer": trailer}


# ── main ────────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Steam Deals Fetch Start ===")

    # Back up existing output before touching anything
    if OUTPUT.exists():
        shutil.copy2(OUTPUT, BACKUP)
        log.info("Backed up existing data → %s", BACKUP)

    try:
        # ── Step 1: discover current Steam deals (USD) ─────────────────────────
        log.info("Step 1: fetch current Steam deals (US)...")
        raw_deals = fetch_steam_deals()

        # Build primary deal map from /deals/v2 data (USD is ground truth).
        # Game-level fields (id, title, slug, assets) are at item root;
        # price/discount/storeLow are nested under item["deal"].
        deal_map: dict[str, dict] = {}
        for item in raw_deals:
            gid = item.get("id")
            if not gid:
                continue
            deal_obj = item.get("deal") or {}
            price_obj = deal_obj.get("price") or {}
            regular_obj = deal_obj.get("regular") or {}
            # historyLow (cross-shop ATL) and storeLow (Steam-specific ATL) are both
            # present in the deal payload; prefer storeLow for Steam ATL comparison,
            # fall back to historyLow if storeLow is absent.
            store_low_obj = deal_obj.get("storeLow") or deal_obj.get("historyLow") or {}
            assets = item.get("assets") or {}
            cut = deal_obj.get("cut", 0)

            deal_map[gid] = {
                "title": item.get("title", ""),
                "slug": item.get("slug", ""),
                "cut": cut,
                "banner": assets.get("banner300") or assets.get("banner145"),
                "prices": {
                    "USD": {
                        "current": price_obj.get("amount"),
                        "regular": regular_obj.get("amount"),
                        "low": store_low_obj.get("amount"),
                        "cut": cut,
                        "currency": "USD",
                        "symbol": "$",
                        "is_atl": False,  # computed below
                    }
                },
            }

            # USD ATL: current price ≤ Steam store all-time low (with float tolerance)
            cur = price_obj.get("amount")
            low = store_low_obj.get("amount")
            if cur is not None and low is not None:
                deal_map[gid]["prices"]["USD"]["is_atl"] = cur <= low + 0.01

        game_ids = list(deal_map.keys())
        log.info("Unique games: %d", len(game_ids))

        # ── Step 2: CN and MY prices via /games/prices/v3 ──────────────────────
        for country, currency, symbol in [("CN", "CNY", "¥"), ("MY", "MYR", "RM")]:
            log.info("Step 2: fetch %s prices (%d games)...", country, len(game_ids))
            prices = fetch_prices_for_country(game_ids, country)
            for gid, p in prices.items():
                if gid not in deal_map:
                    continue
                cur = p.get("current")
                low = p.get("low")
                is_atl = (cur is not None and low is not None and cur <= low + 0.01)
                deal_map[gid]["prices"][currency] = {
                    "current": cur,
                    "regular": p.get("regular"),
                    "low": low,
                    "cut": p.get("cut") or deal_map[gid]["cut"],
                    "currency": currency,
                    "symbol": symbol,
                    "is_atl": is_atl,
                }

        # ── Step 3: determine candidates ───────────────────────────────────────
        # A game is a candidate if it's at USD ATL, OR high discount (potential good deal)
        candidates = [
            gid for gid, d in deal_map.items()
            if d["prices"]["USD"]["is_atl"] or d["cut"] >= GOOD_DEAL_CUT
        ]
        log.info("Candidates (ATL or ≥%d%% off): %d", GOOD_DEAL_CUT, len(candidates))

        # ── Step 4: fetch game info for candidates ──────────────────────────────
        log.info("Step 4: fetch game info for %d candidates...", len(candidates))
        info_map: dict[str, dict] = {}
        for i, gid in enumerate(candidates, 1):
            try:
                info_map[gid] = fetch_game_info(gid)
                if i % 20 == 0:
                    log.info("  %d/%d done", i, len(candidates))
                time.sleep(0.25)
            except Exception as exc:
                log.warning("  info failed for %s: %s", gid, exc)

        # ── Step 5: build final list ────────────────────────────────────────────
        log.info("Step 5: classify and filter...")
        games: list[dict] = []

        for gid in candidates:
            info = info_map.get(gid) or {}
            dm = deal_map[gid]

            steam_rev = get_steam_review(info.get("reviews") or [])
            score = steam_rev.get("score") or 0      # 0–100
            rev_count = steam_rev.get("count") or 0
            rev_text = steam_rev.get("text") or ""

            tier = classify_tier(rev_count, score)
            usd_is_atl = dm["prices"]["USD"]["is_atl"]
            cut = dm["cut"]

            # Inclusion rules:
            # • ATL games: include if score ≥ MIN_ATL_SCORE (already baked into tier for AAA/known)
            #   but also allow "other" tier ATL if score is decent
            # • Non-ATL (good deal): only AAA or known tier, cut ≥ GOOD_DEAL_CUT, score ≥ GOOD_DEAL_SCORE
            if usd_is_atl:
                if score < MIN_ATL_SCORE and tier == "other":
                    continue
            else:
                if cut < GOOD_DEAL_CUT or score < GOOD_DEAL_SCORE:
                    continue
                if tier == "other":
                    continue

            appid = info.get("appid")
            assets = info.get("assets") or {}
            capsule = (
                assets.get("boxart")
                or dm.get("banner")
                or (f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg" if appid else None)
            )

            game: dict = {
                "id": gid,
                "title": dm["title"],
                "slug": dm["slug"],
                "appid": appid,
                "tier": tier,
                "is_atl": usd_is_atl,
                "tags": (info.get("tags") or [])[:8],
                "reviews": {
                    "score": score,
                    "count": rev_count,
                    "text": rev_text,
                },
                "images": {
                    "capsule": capsule,
                    "screenshots": [],
                },
                "trailer": None,
                "steam_url": f"https://store.steampowered.com/app/{appid}" if appid else None,
                "prices": dm["prices"],
            }
            games.append(game)

        # ── Step 6: sort ────────────────────────────────────────────────────────
        games.sort(key=sort_priority)
        log.info("Final list: %d games", len(games))

        # ── Step 7: fetch Steam media for top N games only ──────────────────────
        media_count = min(MAX_MEDIA_GAMES, len(games))
        log.info("Step 7: fetch Steam media for top %d games...", media_count)
        for game in games[:media_count]:
            appid = game.get("appid")
            if appid:
                media = fetch_media(appid)
                game["images"]["screenshots"] = media["screenshots"]
                game["trailer"] = media["trailer"]
                time.sleep(0.6)  # be polite to Steam API

        # ── Step 8: write output ────────────────────────────────────────────────
        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(games),
            "games": games,
        }

        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        log.info("Wrote %s (%d games)", OUTPUT, len(games))

    except Exception:
        log.exception("Fatal error during fetch")
        # If output was partially written or missing, restore backup
        if BACKUP.exists() and (not OUTPUT.exists() or OUTPUT.stat().st_size < 10):
            shutil.copy2(BACKUP, OUTPUT)
            log.info("Restored backup to %s", OUTPUT)
        sys.exit(1)


if __name__ == "__main__":
    main()
