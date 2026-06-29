#!/usr/bin/env python3
"""
Fetches current Steam deals via two sources:
  A) SteamSpy popular game lists  → covers AAA / well-known titles
  B) ITAD /deals/v2 recent feed   → covers new / smaller releases
Then verifies prices + history lows via ITAD /games/prices/v3 for US/CN/MY.
"""

import json
import logging
import os
import re
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

# ── paths ───────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
OUTPUT = ROOT / "docs" / "data" / "deals.json"
BACKUP = ROOT / "docs" / "data" / "deals.backup.json"

# ── API constants ───────────────────────────────────────────────────────────────
ITAD_BASE = "https://api.isthereanydeal.com"
STEAM_API = "https://store.steampowered.com/api/appdetails"
STEAMSPY_BASE = "https://steamspy.com/api.php"
ITAD_KEY = os.environ["ITAD_API_KEY"]
STEAM_SHOP_ID = 61
COUNTRY_CURRENCY = {"US": "USD", "CN": "CNY", "MY": "MYR"}

# ── SteamSpy sources ────────────────────────────────────────────────────────────
STEAMSPY_TOP_ENDPOINTS = ["top100forever", "top100in2weeks"]
STEAMSPY_GENRES = ["Action", "RPG", "Strategy", "Adventure", "Simulation", "Sports"]
STEAMSPY_GENRE_TOP_N = 150  # top N per genre by positive review count

# ── thresholds ──────────────────────────────────────────────────────────────────
MIN_CUT = 30            # minimum discount to consider a game at all
GOOD_DEAL_CUT = 50      # non-ATL games need ≥50% discount
GOOD_DEAL_SCORE = 80    # non-ATL games need ≥80% positive reviews
MIN_TIER_SCORE = 70     # minimum score to qualify for aaa/known tier
OTHER_ATL_MIN_SCORE = 80   # "other" tier ATL: minimum score
OTHER_ATL_MIN_REVIEWS = 500  # "other" tier ATL: minimum review count
AAA_MIN_REVIEWS = 10_000
KNOWN_MIN_REVIEWS = 1_000
MAX_DEALS_PAGES = 30    # /deals/v2 supplement pages (30×100 = 3 000 mixed deals)
MAX_OUTPUT = 300        # hard cap on final output
MAX_MEDIA_GAMES = 50
MAX_SCREENSHOTS = 3


# ── HTTP helpers ─────────────────────────────────────────────────────────────────

def _itad(method: str, path: str, retries: int = 3, **kwargs) -> object:
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
            time.sleep(2 ** attempt)


def _steamspy_get(params: dict, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(STEAMSPY_BASE, params=params,
                             headers={"User-Agent": "Mozilla/5.0"},
                             timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt == retries - 1:
                log.warning("SteamSpy request failed: %s", exc)
                return {}
            time.sleep(2 ** attempt)


def _steam_appdetails(appid: int, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.get(STEAM_API,
                             params={"appids": appid, "filters": "screenshots,movies", "l": "english"},
                             timeout=20)
            r.raise_for_status()
            entry = r.json().get(str(appid), {})
            return entry.get("data", {}) if entry.get("success") else {}
        except Exception as exc:
            if attempt == retries - 1:
                log.warning("Steam appdetails failed for %s: %s", appid, exc)
                return {}
            time.sleep(2 ** attempt)


# ── Source A: SteamSpy popular AppIDs ───────────────────────────────────────────

def fetch_popular_appids() -> list[int]:
    """
    Pull popular game AppIDs from SteamSpy top lists + genre charts.
    These cover AAA and well-known titles that Summer Sale commonly discounts.
    """
    appids: dict[int, str] = {}

    for ep in STEAMSPY_TOP_ENDPOINTS:
        log.info("  SteamSpy %s ...", ep)
        data = _steamspy_get({"request": ep})
        for aid, info in data.items():
            appids[int(aid)] = info.get("name", "")
        time.sleep(1.0)

    for genre in STEAMSPY_GENRES:
        log.info("  SteamSpy genre=%s ...", genre)
        data = _steamspy_get({"request": "genre", "genre": genre})
        # Sort by positive reviews desc and take top N to avoid swamping with small games
        top_genre = sorted(data.items(), key=lambda x: x[1].get("positive", 0), reverse=True)[:STEAMSPY_GENRE_TOP_N]
        before = len(appids)
        for aid, info in top_genre:
            appids[int(aid)] = info.get("name", "")
        log.info("    +%d new from genre (total %d)", len(appids) - before, len(appids))
        time.sleep(1.2)

    result = list(appids.keys())
    log.info("SteamSpy: %d unique popular AppIDs", len(result))
    return result


# ── Source B: ITAD /deals/v2 recent Steam deals ──────────────────────────────────

def fetch_recent_steam_deals() -> list[dict]:
    """
    Paginate /deals/v2 (all shops, country=US) and collect Steam deals with
    cut ≥ MIN_CUT.  Supplements SteamSpy with newer / smaller releases.
    Returns list of {id, title, cut, banner, appid_from_deal}.
    """
    deals: list[dict] = []
    offset = 0
    limit = 100

    for page in range(MAX_DEALS_PAGES):
        data = _itad("GET", "/deals/v2", params={"country": "US", "offset": offset, "limit": limit})
        batch: list[dict] = data.get("deals") or data.get("list") or []
        if not batch:
            break

        for item in batch:
            deal = item.get("deal") or {}
            if (deal.get("shop") or {}).get("id") != STEAM_SHOP_ID:
                continue
            if deal.get("cut", 0) < MIN_CUT:
                continue
            assets = item.get("assets") or {}
            deals.append({
                "id": item.get("id"),
                "title": item.get("title", ""),
                "cut": deal.get("cut", 0),
                "banner": assets.get("banner300") or assets.get("banner145"),
            })

        if not data.get("hasMore", False):
            break
        offset += limit
        time.sleep(0.35)

    log.info("/deals/v2: %d Steam deals with cut ≥ %d%%", len(deals), MIN_CUT)
    return deals


# ── AppID → ITAD UUID lookup ─────────────────────────────────────────────────────

def lookup_itad_ids(appids: list[int]) -> dict[int, str]:
    """
    POST /lookup/id/shop/61/v1 to convert Steam AppIDs to ITAD UUIDs.
    ITAD expects shop IDs in format "app/APPID".
    Returns {appid: uuid}.
    """
    result: dict[int, str] = {}
    chunk_size = 500

    for i in range(0, len(appids), chunk_size):
        chunk = appids[i : i + chunk_size]
        shop_ids = [f"app/{aid}" for aid in chunk]
        data: dict = _itad("POST", "/lookup/id/shop/61/v1", json=shop_ids)
        for shop_id, uuid in data.items():
            if uuid:
                m = re.search(r"\d+", shop_id)
                if m:
                    result[int(m.group())] = uuid
        log.info("  lookup chunk %d-%d: %d resolved", i, i + len(chunk), len(result))
        time.sleep(0.4)

    return result


# ── Prices (all three currencies) ────────────────────────────────────────────────

def fetch_prices_for_country(game_ids: list[str], country: str) -> dict[str, dict]:
    """
    POST /games/prices/v3 in chunks; return dict keyed by ITAD game UUID.
    Validates that returned prices are in the expected regional currency.
    Returns {uuid: {current, regular, cut, store_low, low}}.
    """
    expected_currency = COUNTRY_CURRENCY.get(country, "").upper()
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

            steam_deal = next(
                (d for d in row.get("deals", []) if d.get("shop", {}).get("id") == STEAM_SHOP_ID),
                None,
            )

            # Discard deals returned in wrong currency (e.g. USD for country=MY)
            if steam_deal:
                deal_currency = (steam_deal.get("price") or {}).get("currency", "").upper()
                if expected_currency and deal_currency and deal_currency != expected_currency:
                    steam_deal = None

            history_low_all = (row.get("historyLow") or {}).get("all") or {}
            low_currency = history_low_all.get("currency", "").upper()
            low_amount = history_low_all.get("amount") if low_currency == expected_currency else None

            # storeLow in the deal = Steam-specific store all-time low
            store_low_obj = (steam_deal or {}).get("storeLow") or {}
            store_low_cur = store_low_obj.get("currency", "").upper()
            store_low = (
                store_low_obj.get("amount")
                if store_low_cur == expected_currency
                else None
            )

            result[gid] = {
                "current": steam_deal["price"]["amount"] if steam_deal else None,
                "regular": steam_deal["regular"]["amount"] if steam_deal else None,
                "cut": steam_deal.get("cut") if steam_deal else None,
                "store_low": store_low,   # Steam-specific ATL (preferred)
                "low": low_amount,        # cross-shop ATL (fallback display)
            }
        time.sleep(0.3)

    return result


# ── Game info ────────────────────────────────────────────────────────────────────

def fetch_game_info(game_id: str) -> dict:
    return _itad("GET", "/games/info/v2", params={"id": game_id})


# ── Classification + sorting ─────────────────────────────────────────────────────

def get_steam_review(reviews_list: list[dict]) -> dict:
    for r in reviews_list or []:
        if str(r.get("source", "")).lower() == "steam":
            return r
    return {}


def classify_tier(review_count: int, score: int) -> str:
    if review_count >= AAA_MIN_REVIEWS and score >= MIN_TIER_SCORE:
        return "aaa"
    if review_count >= KNOWN_MIN_REVIEWS and score >= MIN_TIER_SCORE:
        return "known"
    return "other"


def sort_priority(game: dict) -> tuple:
    tier_rank = {"aaa": 0, "known": 1, "other": 2}[game["tier"]]
    atl_rank = 0 if game["is_atl"] else 1
    # Primary: (ATL first, then tier); secondary: discount descending
    cut = game["prices"].get("USD", {}).get("cut") or 0
    return (atl_rank, tier_rank, -cut)


# ── Media ────────────────────────────────────────────────────────────────────────

def fetch_media(appid: int) -> dict:
    data = _steam_appdetails(appid)
    screenshots = [
        s["path_full"] for s in (data.get("screenshots") or [])[:MAX_SCREENSHOTS]
    ]
    trailer = None
    movies = data.get("movies") or []
    if movies:
        movie = next((m for m in movies if m.get("highlight")), movies[0])
        mp4 = movie.get("mp4") or {}
        webm = movie.get("webm") or {}
        trailer = mp4.get("max") or mp4.get("480") or webm.get("max") or webm.get("480")
    return {"screenshots": screenshots, "trailer": trailer}


# ── Main ──────────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Steam Deals Fetch Start ===")

    if OUTPUT.exists():
        shutil.copy2(OUTPUT, BACKUP)
        log.info("Backed up existing data")

    try:
        # ── A: SteamSpy popular AppIDs → ITAD UUIDs ────────────────────────────
        log.info("Step A: SteamSpy popular games...")
        popular_appids = fetch_popular_appids()

        log.info("Step A2: ITAD lookup for %d AppIDs...", len(popular_appids))
        appid_to_uuid = lookup_itad_ids(popular_appids)
        log.info("  resolved %d/%d AppIDs", len(appid_to_uuid), len(popular_appids))

        # ── B: /deals/v2 recent Steam deals ─────────────────────────────────────
        log.info("Step B: ITAD recent Steam deals...")
        recent_deals = fetch_recent_steam_deals()

        # ── Merge UUID sets ──────────────────────────────────────────────────────
        uuid_to_appid: dict[str, int] = {v: k for k, v in appid_to_uuid.items()}
        uuid_set: set[str] = set(appid_to_uuid.values())

        # For /deals/v2 games, ITAD game ID is directly available
        recent_by_id: dict[str, dict] = {}
        for d in recent_deals:
            gid = d.get("id")
            if gid:
                uuid_set.add(gid)
                recent_by_id[gid] = d

        all_uuids = list(uuid_set)
        log.info("Total unique UUIDs to price-check: %d", len(all_uuids))

        # ── Fetch prices for US / CN / MY ────────────────────────────────────────
        log.info("Step C: fetch USD prices (%d games)...", len(all_uuids))
        us_prices = fetch_prices_for_country(all_uuids, "US")

        # Filter: must have a current Steam deal with cut ≥ MIN_CUT
        candidates = [
            gid for gid in all_uuids
            if (us_prices.get(gid) or {}).get("current") is not None
            and (us_prices.get(gid) or {}).get("cut", 0) >= MIN_CUT
        ]
        log.info("Candidates with current Steam deal ≥ %d%% off: %d", MIN_CUT, len(candidates))

        log.info("Step C2: fetch CNY prices (%d games)...", len(candidates))
        cn_prices = fetch_prices_for_country(candidates, "CN")

        log.info("Step C3: fetch MYR prices (%d games)...", len(candidates))
        my_prices = fetch_prices_for_country(candidates, "MY")

        # ── Fetch game info for candidates ───────────────────────────────────────
        log.info("Step D: fetch game info for %d candidates...", len(candidates))
        info_map: dict[str, dict] = {}
        for i, gid in enumerate(candidates, 1):
            try:
                info_map[gid] = fetch_game_info(gid)
                if i % 20 == 0:
                    log.info("  %d/%d", i, len(candidates))
                time.sleep(0.2)
            except Exception as exc:
                log.warning("  info failed for %s: %s", gid, exc)

        # ── Build final game list ────────────────────────────────────────────────
        log.info("Step E: classify and filter...")
        games: list[dict] = []

        for gid in candidates:
            info = info_map.get(gid) or {}
            usd = us_prices.get(gid) or {}
            cny = cn_prices.get(gid) or {}
            myr = my_prices.get(gid) or {}

            steam_rev = get_steam_review(info.get("reviews") or [])
            score = steam_rev.get("score") or 0      # 0–100
            rev_count = steam_rev.get("count") or 0
            rev_text = steam_rev.get("text") or ""

            tier = classify_tier(rev_count, score)
            cut = usd.get("cut", 0)

            # ATL check: prefer Steam-specific store_low; fall back to cross-shop low
            cur_usd = usd.get("current")
            atl_ref = usd.get("store_low") or usd.get("low")
            is_atl = (cur_usd is not None and atl_ref is not None and cur_usd <= atl_ref + 0.01)

            # Inclusion rules
            if is_atl:
                if tier == "other":
                    # ATL "other": only include games with decent quality
                    if score < OTHER_ATL_MIN_SCORE or rev_count < OTHER_ATL_MIN_REVIEWS:
                        continue
            else:
                # Non-ATL: only AAA/known with big discount and high score
                if tier == "other":
                    continue
                if cut < GOOD_DEAL_CUT or score < GOOD_DEAL_SCORE:
                    continue

            appid = info.get("appid") or uuid_to_appid.get(gid)
            assets = info.get("assets") or {}
            banner_from_deal = (recent_by_id.get(gid) or {}).get("banner")
            capsule = (
                assets.get("boxart")
                or banner_from_deal
                or (f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/capsule_616x353.jpg"
                    if appid else None)
            )

            def price_block(p: dict, currency: str, symbol: str) -> dict | None:
                cur = p.get("current")
                if cur is None:
                    return None
                low = p.get("store_low") or p.get("low")
                p_is_atl = (cur is not None and low is not None and cur <= low + 0.01)
                return {
                    "current": cur,
                    "regular": p.get("regular"),
                    "low": low,
                    "cut": p.get("cut") or cut,
                    "currency": currency,
                    "symbol": symbol,
                    "is_atl": p_is_atl,
                }

            prices: dict = {}
            usd_block = price_block(usd, "USD", "$")
            if usd_block:
                prices["USD"] = usd_block
            cny_block = price_block(cny, "CNY", "¥")
            if cny_block:
                prices["CNY"] = cny_block
            myr_block = price_block(myr, "MYR", "RM")
            if myr_block:
                prices["MYR"] = myr_block

            if not prices.get("USD"):
                continue

            games.append({
                "id": gid,
                "title": info.get("title") or (recent_by_id.get(gid) or {}).get("title", ""),
                "slug": info.get("slug", ""),
                "appid": appid,
                "tier": tier,
                "is_atl": is_atl,
                "tags": (info.get("tags") or [])[:8],
                "reviews": {"score": score, "count": rev_count, "text": rev_text},
                "images": {"capsule": capsule, "screenshots": []},
                "trailer": None,
                "steam_url": f"https://store.steampowered.com/app/{appid}" if appid else None,
                "prices": prices,
            })

        # ── Sort + cap ───────────────────────────────────────────────────────────
        games.sort(key=sort_priority)
        games = games[:MAX_OUTPUT]
        log.info("Final list: %d games (capped at %d)", len(games), MAX_OUTPUT)

        # ── Step F: Fill missing MYR prices from Steam API ───────────────────────
        myr_missing = [g for g in games if not g["prices"].get("MYR") and g["appid"]]
        log.info("Step F: fetching Steam MYR prices for %d games without ITAD data...", len(myr_missing))
        for game in myr_missing:
            try:
                r = requests.get(
                    STEAM_API,
                    params={"appids": game["appid"], "cc": "MY", "filters": "price_overview", "l": "english"},
                    timeout=15,
                )
                r.raise_for_status()
                entry = r.json().get(str(game["appid"]), {})
                po = entry.get("data", {}).get("price_overview", {}) if entry.get("success") else {}
                if po and po.get("currency") == "MYR":
                    current_myr = round(po["final"] / 100, 2)
                    regular_myr = round(po["initial"] / 100, 2)
                    cut_myr = po.get("discount_percent", 0)
                    game["prices"]["MYR"] = {
                        "current": current_myr,
                        "regular": regular_myr,
                        "cut": cut_myr,
                        "low": None,
                        "currency": "MYR",
                        "symbol": "RM",
                        "is_atl": False,
                    }
            except Exception as exc:
                log.debug("Steam MYR price failed for %s: %s", game.get("title"), exc)
            time.sleep(0.4)

        # ── Step G: Media for top N ───────────────────────────────────────────────
        media_count = min(MAX_MEDIA_GAMES, len(games))
        log.info("Step G: Steam media for top %d games...", media_count)
        for game in games[:media_count]:
            if game["appid"]:
                media = fetch_media(game["appid"])
                game["images"]["screenshots"] = media["screenshots"]
                game["trailer"] = media["trailer"]
                time.sleep(0.6)

        # ── Write output ──────────────────────────────────────────────────────────
        output = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total": len(games),
            "games": games,
        }
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(output, ensure_ascii=False, indent=2))
        log.info("Wrote %s (%d games)", OUTPUT, len(games))

    except Exception:
        log.exception("Fatal error")
        if BACKUP.exists() and (not OUTPUT.exists() or OUTPUT.stat().st_size < 10):
            shutil.copy2(BACKUP, OUTPUT)
            log.info("Restored backup")
        sys.exit(1)


if __name__ == "__main__":
    main()
