"""dem_selector.py — OpenTopography highest-available DEM selection + download.

Owns the "which DEM, at what resolution, from which endpoint" decision for a
given lat/lon bounding box, plus the actual streamed GeoTIFF download with clean,
user-facing error messages on the failure modes OpenTopography throws (401/403
auth, 429 rate limit, 400 area-too-large).

No QGIS imports here — pure stdlib + ``requests`` so it stays importable and
``py_compile``-able outside QGIS. The caller (rem_task.py) walks the ordered
candidate list from :func:`candidate_datasets` and tries each with
:func:`download_dem` until one succeeds.

Selection logic (locked spec):
  - Inside a rough North-America envelope, try USGS 3DEP high-res first:
      USGS1m  (<=   250 km^2) -> USGS10m (<= 25000 km^2) -> USGS30m (<= 225000 km^2)
    on the usgsdem endpoint (param ``datasetName=``).
  - Otherwise, or as the fallback after USGS, global coverage:
      COP30   (<= 450000 km^2) -> SRTMGL3 (<= 4050000 km^2)
    on the globaldem endpoint (param ``demtype=``).
  - Only datasets whose per-request area cap the bbox respects are offered.
"""

from __future__ import annotations

import math
from typing import Callable, List, Dict, Optional

import requests


# ---------------------------------------------------------------------------
# Endpoints (locked spec)
# ---------------------------------------------------------------------------
USGSDEM_ENDPOINT = "https://portal.opentopography.org/API/usgsdem"
GLOBALDEM_ENDPOINT = "https://portal.opentopography.org/API/globaldem"

# Network timeout for the DEM download. Generous because high-res tiles can take
# a while server-side to cut; tune here if downloads stall vs. time out. (s)
_REQUEST_TIMEOUT = 300


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def bbox_area_km2(s: float, n: float, w: float, e: float) -> float:
    """Approximate area of an EPSG:4326 bbox in square kilometres.

    Uses a simple equirectangular approximation: latitude degrees are ~111.32 km
    apart everywhere; longitude degrees shrink by cos(mean latitude). Plenty
    accurate for picking an area-cap tier (we are nowhere near a tier boundary
    in practice).
    """
    # Normalise ordering so callers can pass corners in any order.
    south, north = min(s, n), max(s, n)
    west, east = min(w, e), max(w, e)

    km_per_deg_lat = 111.32  # mean meridional km per degree
    mean_lat_rad = math.radians((south + north) / 2.0)
    km_per_deg_lon = 111.32 * math.cos(mean_lat_rad)

    height_km = (north - south) * km_per_deg_lat
    width_km = (east - west) * km_per_deg_lon
    return abs(height_km * width_km)


def in_north_america(s: float, n: float, w: float, e: float) -> bool:
    """True if the bbox centroid falls inside a rough North-America envelope.

    This is the gate for trying USGS 3DEP high-res (CONUS + a generous margin
    covering Alaska/Hawaii/lower Canada/northern Mexico). Deliberately loose —
    if USGS has no data for the box, the download simply fails and the caller
    falls through to the global COP30/SRTMGL3 candidates anyway.
    """
    south, north = min(s, n), max(s, n)
    west, east = min(w, e), max(w, e)
    lat = (south + north) / 2.0
    lon = (west + east) / 2.0
    # Tunable envelope: lat 15..72 N, lon -170..-50 W.
    return (15.0 <= lat <= 72.0) and (-170.0 <= lon <= -50.0)


# ---------------------------------------------------------------------------
# Candidate dataset list
# ---------------------------------------------------------------------------
# Each tier: (param-name carrier, dataset code, native resolution m, area cap km^2,
#             is_dsm flag). is_dsm marks datasets that are surface (canopy/building)
# rather than bare-earth terrain — surfaced as a caveat outside the US.
#
# USGS 3DEP products are bare-earth DTM. COP30 (Copernicus GLO-30) and SRTMGL3 are
# digital *surface* models (DSM) — they include vegetation/structures.
_USGS_TIERS = [
    {"endpoint": USGSDEM_ENDPOINT, "param": "datasetName", "value": "USGS1m",
     "res_m": 1,  "max_km2": 250,    "is_dsm": False},
    {"endpoint": USGSDEM_ENDPOINT, "param": "datasetName", "value": "USGS10m",
     "res_m": 10, "max_km2": 25000,  "is_dsm": False},
    {"endpoint": USGSDEM_ENDPOINT, "param": "datasetName", "value": "USGS30m",
     "res_m": 30, "max_km2": 225000, "is_dsm": False},
]

_GLOBAL_TIERS = [
    {"endpoint": GLOBALDEM_ENDPOINT, "param": "demtype", "value": "COP30",
     "res_m": 30, "max_km2": 450000,   "is_dsm": True},
    {"endpoint": GLOBALDEM_ENDPOINT, "param": "demtype", "value": "SRTMGL3",
     "res_m": 90, "max_km2": 4050000,  "is_dsm": True},
]


def candidate_datasets(s: float, n: float, w: float, e: float) -> List[Dict]:
    """Ordered list of DEM candidates to try for this bbox, finest first.

    Returns a list of dicts ``{endpoint, param, value, res_m, max_km2, is_dsm}``.
    Inside North America the USGS high-res tiers come first (finest that the
    bbox area fits), then the global COP30/SRTMGL3 tiers as fallback. Outside
    North America only the global tiers apply. Any tier whose ``max_km2`` cap
    the bbox exceeds is dropped (the API would reject it as area-too-large).
    """
    area = bbox_area_km2(s, n, w, e)
    tiers: List[Dict] = []

    if in_north_america(s, n, w, e):
        tiers.extend(_USGS_TIERS)
    tiers.extend(_GLOBAL_TIERS)

    # Keep only candidates whose area cap the bbox respects; preserve order
    # (finest-resolution / smallest-cap first within each group).
    eligible = [dict(t) for t in tiers if area <= t["max_km2"]]
    return eligible


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _build_url_and_params(candidate: Dict, s: float, n: float, w: float, e: float,
                          api_key: str) -> (str, Dict[str, str]):
    """Assemble the request URL + query params for one candidate.

    usgsdem carries the dataset in ``datasetName=``; globaldem in ``demtype=``.
    Both take south/north/west/east, ``outputFormat=GTiff`` and ``API_Key=``.
    """
    south, north = min(s, n), max(s, n)
    west, east = min(w, e), max(w, e)

    params = {
        candidate["param"]: candidate["value"],
        "south": f"{south:.8f}",
        "north": f"{north:.8f}",
        "west": f"{west:.8f}",
        "east": f"{east:.8f}",
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    return candidate["endpoint"], params


def _redact(url: str) -> str:
    """Strip the API key from a URL before it appears in any error message."""
    if "API_Key=" not in url:
        return url
    head, _, tail = url.partition("API_Key=")
    # Drop everything from the key value up to the next param boundary.
    rest = tail.split("&", 1)
    suffix = ("&" + rest[1]) if len(rest) > 1 else ""
    return f"{head}API_Key=***{suffix}"


def download_dem(candidate: Dict, bbox, api_key: str, out_path: str,
                 progress_cb: Optional[Callable[[float], None]] = None) -> str:
    """Download one DEM candidate to ``out_path`` as a GeoTIFF.

    ``bbox`` is ``(s, n, w, e)`` in EPSG:4326. Streams the response to disk so a
    large tile never has to sit in memory. ``progress_cb`` (if given) is called
    with a 0..100 float as bytes arrive (only meaningful when the server sends a
    Content-Length; high-res cuts often don't, in which case progress just sits).

    Raises:
        requests.HTTPError — with a clean, key-free message on 401/403 (auth),
            429 (rate limit), or 400 (commonly area-too-large), and on any other
            non-2xx status.
        RuntimeError — on a network failure, an empty/non-GeoTIFF body, or an
            error payload returned with a 200.
    """
    s, n, w, e = bbox
    url, params = _build_url_and_params(candidate, s, n, w, e, api_key)
    label = candidate["value"]

    try:
        resp = requests.get(url, params=params, stream=True,
                            timeout=_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Network error contacting OpenTopography for {label}: {exc}"
        ) from exc

    safe_url = _redact(resp.url) if resp is not None else _redact(url)

    # --- Map the documented failure status codes to clean messages. ---
    status = resp.status_code
    if status != 200:
        # Try to surface the server's own short explanation (it returns text on
        # 400 area-too-large / bad params), but never echo the key-bearing URL.
        detail = ""
        try:
            detail = resp.text.strip()
        except Exception:
            detail = ""
        # Keep the detail terse so the message bar stays readable.
        if len(detail) > 300:
            detail = detail[:300] + "…"

        if status in (401, 403):
            msg = (f"OpenTopography rejected the request for {label} "
                   f"(HTTP {status}). The API key is missing, invalid, or not "
                   f"authorized for this dataset (USGS1m can be access-gated). "
                   f"Check the key in River REM Settings.")
        elif status == 429:
            msg = (f"OpenTopography rate limit hit for {label} (HTTP 429). "
                   f"Wait and retry, or use a higher-quota key.")
        elif status == 400:
            # 400 is overwhelmingly "requested area too large" or a bad-param.
            msg = (f"OpenTopography refused {label} (HTTP 400). Usually the "
                   f"requested area is too large for this dataset — zoom in and "
                   f"try again."
                   + (f" Server said: {detail}" if detail else ""))
        else:
            msg = (f"OpenTopography returned HTTP {status} for {label}."
                   + (f" Server said: {detail}" if detail else ""))
        # Raise as HTTPError so the caller can distinguish protocol failures;
        # attach the response and a redacted request line for debugging.
        err = requests.HTTPError(msg, response=resp)
        err.request_url = safe_url  # type: ignore[attr-defined]
        resp.close()
        raise err

    # --- 200 OK: guard against an error payload masquerading as success. ---
    content_type = (resp.headers.get("Content-Type") or "").lower()
    if "text" in content_type or "json" in content_type or "html" in content_type:
        body = ""
        try:
            body = resp.text.strip()
        except Exception:
            body = ""
        resp.close()
        if len(body) > 300:
            body = body[:300] + "…"
        raise RuntimeError(
            f"OpenTopography returned an error instead of a GeoTIFF for {label}"
            + (f": {body}" if body else ".")
        )

    # --- Stream the GeoTIFF to disk. ---
    total = resp.headers.get("Content-Length")
    try:
        total_bytes = int(total) if total else 0
    except (TypeError, ValueError):
        total_bytes = 0

    written = 0
    try:
        with open(out_path, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1024 * 256):  # 256 KiB
                if not chunk:
                    continue
                fh.write(chunk)
                written += len(chunk)
                if progress_cb and total_bytes > 0:
                    progress_cb(min(100.0, 100.0 * written / total_bytes))
    except OSError as exc:
        resp.close()
        raise RuntimeError(
            f"Failed writing DEM {label} to {out_path}: {exc}"
        ) from exc
    finally:
        resp.close()

    # GeoTIFFs start with "II"/"MM" + 0x2A; if we got essentially nothing the
    # request "succeeded" but produced no data — treat as an error so the caller
    # falls through to the next candidate.
    if written < 256:
        raise RuntimeError(
            f"OpenTopography returned an empty/too-small file for {label} "
            f"({written} bytes). The dataset likely has no coverage here."
        )

    return out_path
