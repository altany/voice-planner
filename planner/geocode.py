"""Place-name → coordinates.

Two providers, cheapest first:
- OpenStreetMap's Nominatim (free): streets, neighbourhoods, cities. Its usage
  policy asks for a descriptive User-Agent and max 1 request/second, so
  lookups are throttled and cached.
- Google Places (optional fallback, needs GOOGLE_PLACES_API_KEY): businesses
  and venues by name ("Παπαδάκης οδοντίατρος, Λάρισα") that OSM doesn't know.

Everything here is best-effort — a failed geocode never blocks saving a task.
"""

import logging
import os
import threading
import time

import requests

log = logging.getLogger("planner.geocode")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "voice-planner/1.0 (personal Raspberry Pi tool)"}

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

_lock = threading.Lock()
_last_request = 0.0
_cache: dict[str, list[dict]] = {}


def search(query: str, limit: int = 5) -> list[dict]:
    """Return [{label, lat, lon}] suggestions for a place query."""
    query = (query or "").strip()
    if len(query) < 3:
        return []
    key = query.lower()
    if key in _cache:
        return _cache[key][:limit]

    global _last_request
    with _lock:
        wait = 1.0 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": limit},
            headers=HEADERS,
            timeout=5,
        )
        resp.raise_for_status()
        results = [
            {"label": r["display_name"], "lat": float(r["lat"]), "lon": float(r["lon"]),
             "provider": "osm"}
            for r in resp.json()
        ]
    except Exception as e:
        log.warning("geocode failed for %r: %s", query, e)
        return []
    if not results:
        results = _places_search(query, limit)
    _cache[key] = results
    return results


def _places_search(query: str, limit: int) -> list[dict]:
    if not PLACES_KEY:
        return []
    try:
        resp = requests.post(
            PLACES_URL,
            json={"textQuery": query, "maxResultCount": min(limit, 20)},
            headers={
                "X-Goog-Api-Key": PLACES_KEY,
                "X-Goog-FieldMask": (
                    "places.displayName,places.formattedAddress,places.location,"
                    "places.googleMapsUri"
                ),
            },
            timeout=5,
        )
        resp.raise_for_status()
        return [
            {
                "label": p["displayName"]["text"]
                + (", " + p["formattedAddress"] if p.get("formattedAddress") else ""),
                "lat": p["location"]["latitude"],
                "lon": p["location"]["longitude"],
                "provider": "google",
                # keep just the stable ?cid= part; the g_mp suffix is tracking noise
                "maps_url": (p.get("googleMapsUri") or "").split("&g_mp=")[0] or None,
            }
            for p in resp.json().get("places", [])
        ]
    except Exception as e:
        log.warning("places search failed for %r: %s", query, e)
        return []


def lookup(query: str) -> dict | None:
    """Best single match, or None. Used when a new voice note mentions a place."""
    results = search(query, limit=1)
    return results[0] if results else None
