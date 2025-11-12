from fastapi import FastAPI, UploadFile, Body, Query
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import polars as pl
import pandas as pd
import io, time, re, math
from typing import Dict, Any
from pathlib import Path

# ------------------------------
# App & Static UI
# ------------------------------
app = FastAPI()
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

@app.get("/")
def root():
    return RedirectResponse(url="/ui/")

# ------------------------------
# Global in-memory DF for analysis CSV
# ------------------------------
DF: pl.DataFrame | None = None

# ------------------------------
# Base-Data: Trains (robust loader)
# ------------------------------
BASE_DIR = Path(__file__).parent
BASE_DATA_DIR = BASE_DIR / "base-data"

TRAINS_DF: pl.DataFrame | None = None
ROUTE_SEQUENCES: dict[str, list[str]] = {}  # Maps route_id to station sequence
ROUTE_STATIONS: dict[str, set[str]] = {}  # Maps route_id to set of all valid stations
ROUTE_ADJACENCY: dict[str, set[tuple[str, str]]] = {}  # Maps route_id to allowed station transitions
GEOFENCE_DATA: list[dict[str, Any]] = []
GEOFENCE_TOLERANCE_M = 100.0
GEOFENCE_RADIUS_MARGIN_M = 60.0
RESTRICTION_POINT_TOLERANCE_M = 120.0
SPEED_RESTRICTIONS: list[dict[str, Any]] = []
MPS_POINT_TOLERANCE_M = 500.0
ROUTE_ALIAS_OVERRIDES: dict[tuple[str, str], list[str]] = {
    ("MMR-CSMT", "UP"): ["JL-CSMT"],
    ("CSMT-MMR", "DN"): ["CSMT-JL"],
}

STATION_SEQUENCE_LOOKAHEAD_ROWS = 1500
STATION_SEQUENCE_MAX_STATIONS = 400
STATION_ADJACENCY_MIN_RUN = 4
STATION_YARD_TOLERANCE = 8

MPS_CONFIG: dict[str, float] = {
    # DN (CSMT → PUNE)
    "CSMT-KYN": 105.0,
    "KYN-KJT": 105.0,
    "KJT-PDI": 80.0,
    "PDI-LNL": 60.0,
    "LNL-PUNE": 110.0,
    # UP (PUNE → CSMT)
    "PUNE-LNL": 110.0,
    "LNL-PDI": 60.0,
    "PDI-KJT": 80.0,
    "KJT-KYN": 105.0,
    "KYN-CSMT": 105.0,
    # DN (CSMT → JL)
    "KYN-KSRA": 105.0,
    "KSRA-IGP": 60.0,
    "IGP-JL": 130.0,
    # UP (JL → CSMT)
    "JL-IGP": 130.0,
    "IGP-KSRA": 60.0,
    "KSRA-KYN": 105.0,
    "IGP-MMR": 120.0,
    "MMR-IGP": 120.0,
    # DN (CSMT → RN)
    "CSMT-DR": 105.0,
    "DR-TNA": 105.0,
    "TNA-DIVA": 105.0,
    "CSMT-DIVA": 105.0,
    "DIVA-PNVL": 110.0,
    "PNVL-ROHA": 105.0,
    "ROHA-CHI": 120.0,
    "CHI-RN": 120.0,
    
    # UP (RN → CSMT)
    "RN-CHI": 120.0,
    "CHI-ROHA": 120.0,
    
    "ROHA-PNVL": 105.0,
    "PNVL-DIVA": 110.0,
    "DIVA-TNA": 105.0,
    "TNA-DR": 105.0,
    "DR-CSMT": 105.0,
    "DIVA-CSMT": 105.0,
    # DN (LTT → PUNE)
    "LTT-KYN": 105.0,
    "LTT-DIVA": 105.0,
    "PNVL-KJT": 105.0,
    # UP (PUNE → LTT)
    "KYN-LTT": 105.0,
    "KJT-PNVL": 105.0,
    "DIVA-LTT": 105.0,
    # DN (PNVL → JL)
    "PNVL-DTVL": 110.0,
    "DTVL-KYN": 105.0,
    # UP (JL → PNVL)
    "KYN-DTVL": 105.0,
    "DTVL-PNVL": 110.0,
}

ROUTE_SECTION_MAP: dict[tuple[str, str], list[list[str]]] = {
    ("CSMT-PUNE", "DN"): [
        ["CSMT-KYN", "KYN-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
        ["CSMT-DIVA", "DIVA-PNVL", "PNVL-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
        
    ],
    ("CSMT-PUNE", "UP"): [
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-KYN", "KYN-CSMT"],
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-PNVL", "PNVL-DIVA", "DIVA-CSMT"],
    ],
    ("CSMT-JL", "DN"): [["CSMT-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-JL"]],
    ("JL-CSMT", "UP"): [["JL-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-CSMT"]],
    ("CSMT-IGP", "DN"): [["CSMT-KYN", "KYN-KSRA", "KSRA-IGP"]],
    ("IGP-CSMT", "UP"): [["IGP-KSRA", "KSRA-KYN", "KYN-CSMT"]],
    ("CSMT-RN", "DN"): [["CSMT-DR", "DR-TNA", "TNA-DIVA", "DIVA-PNVL", "PNVL-ROHA", "ROHA-CHI", "CHI-RN"]],
    ("RN-CSMT", "UP"): [["RN-CHI", "CHI-ROHA", "ROHA-PNVL", "PNVL-DIVA", "DIVA-TNA", "TNA-DR", "DR-CSMT"]],
    ("LTT-PUNE", "DN"): [
        ["LTT-KYN", "KYN-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
        ["LTT-DIVA", "DIVA-PNVL", "PNVL-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
    ],
    ("PUNE-LTT", "UP"): [
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-KYN", "KYN-LTT"],
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-PNVL", "PNVL-DIVA", "DIVA-LTT"],
    ],
    ("PNVL-JL", "DN"): [["PNVL-DTVL", "DTVL-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-JL"]],
    ("JL-PNVL", "UP"): [["JL-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-DTVL", "DTVL-PNVL"]],
    ("LTT-JL", "DN"): [["LTT-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-JL"]],
    ("JL-LTT", "UP"): [["JL-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-LTT"]],
    ("LTT-RN", "DN"): [["LTT-DIVA", "DIVA-PNVL", "PNVL-ROHA", "ROHA-CHI", "CHI-RN"]],
    ("RN-LTT", "UP"): [["RN-CHI", "CHI-ROHA", "ROHA-PNVL", "PNVL-DIVA", "DIVA-LTT"]],
    ("CSMT-MMR", "DN"): [["CSMT-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-MMR"]],
    ("MMR-CSMT", "UP"): [["MMR-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-CSMT"]],
}

def _find_base_csv(prefix: str) -> Path | None:
    """
    Find a CSV file in base-data/ whose name starts with `prefix` (case-insensitive).
    Returns the first match, else None.
    """
    if not BASE_DATA_DIR.exists():
        return None
    for p in BASE_DATA_DIR.iterdir():
        if p.is_file() and p.suffix.lower() == ".csv" and p.name.lower().startswith(prefix.lower()):
            return p
    return None


def _trim_expr(col: str) -> pl.Expr:
    """Regex-trim leading/trailing whitespace using replace_all (no .str.strip)."""
    return pl.col(col).cast(pl.Utf8).str.replace_all(r"^\s+|\s+$", "")


def _split_station_list(raw: Any) -> list[str]:
    """Split a comma-separated station list into uppercase tokens."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        items = raw
    else:
        items = str(raw).split(",")
    out: list[str] = []
    for item in items:
        token = str(item).strip().upper()
        if token:
            out.append(token)
    return out


def load_trains_base():
    """Load trains base-data into TRAINS_DF with a normalized key for matching."""
    global TRAINS_DF
    try:
        trains_path = _find_base_csv("train_with_from_to_stations")
        if trains_path is None:
            print("[WARN] trains CSV not found in base-data/. Expected file starting with 'train_with_from_to_stations'")
            TRAINS_DF = None
            return
        df = pl.read_csv(trains_path)
        # Normalize headers to lowercase & strip
        df = df.rename({c: c.strip().lower() for c in df.columns})

        required = {"train_number", "from_station", "to_station", "direction"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing required headers: {sorted(missing)}")

        # Build normalized keys without .str.strip()
        df = df.with_columns([
            _trim_expr("train_number").str.replace_all(r"\s+", "").str.to_uppercase().alias("train_number_norm"),
            _trim_expr("from_station").alias("from_station"),
            _trim_expr("to_station").alias("to_station"),
            _trim_expr("direction").str.to_uppercase().alias("direction"),
        ])
        TRAINS_DF = df
        print(f"[OK] Loaded trains base-data from {trains_path.name} with {len(df)} rows")
    except Exception as e:
        print(f"[ERROR] Failed to load trains base-data: {e}")
        TRAINS_DF = None


# Load at import
load_trains_base()


def load_geofences():
    """Load geofence reference data for station coordinates."""
    global GEOFENCE_DATA
    try:
        geo_path = _find_base_csv("geo")
        if geo_path is None:
            print("[WARN] geo locations CSV not found in base-data/")
            GEOFENCE_DATA = []
            return
        df = pl.read_csv(geo_path)
        df = df.rename({c: c.strip().lower() for c in df.columns})
        records: list[dict[str, Any]] = []
        for row in df.iter_rows(named=True):
            station = str(row.get("station_code") or "").strip().upper()
            if not station:
                continue
            records.append(
                {
                    "station_code": station,
                    "latitude": float(row.get("latitude") or 0.0),
                    "longitude": float(row.get("longitude") or 0.0),
                    "radius_m": float(row.get("radius_m") or 0.0),
                    "direction": str(row.get("direction") or "").strip().upper(),
                }
            )
        GEOFENCE_DATA = records
        print(f"[OK] Loaded {len(records)} geofence rows from {geo_path.name}")
    except Exception as e:
        print(f"[ERROR] Failed to load geofences: {e}")
        GEOFENCE_DATA = []


load_geofences()


def load_speed_restrictions():
    """Load sectional speed restrictions (with start/end coordinates)."""
    global SPEED_RESTRICTIONS
    try:
        sr_path = _find_base_csv("all_section_psr") or _find_base_csv("speed_restrictions")
        if sr_path is None:
            print("[WARN] speed restrictions CSV not found in base-data/")
            SPEED_RESTRICTIONS = []
            return
        df = pl.read_csv(sr_path)
        df = df.rename({c: c.strip().lower() for c in df.columns})
        cols = set(df.columns)
        required = {"from_lat", "from_lon", "to_lat", "to_lon", "speed_limit_kmph", "route"}
        missing = required - cols
        if missing:
            raise ValueError(f"speed restriction file missing columns: {sorted(missing)}")
        records = []
        for row in df.iter_rows(named=True):
            try:
                records.append(
                    {
                        "route": str(row.get("route") or "").strip().upper(),
                        "direction": str(row.get("direction") or "").strip().upper(),
                        "from_lat": float(row.get("from_lat") or 0.0),
                        "from_lon": float(row.get("from_lon") or 0.0),
                        "to_lat": float(row.get("to_lat") or 0.0),
                        "to_lon": float(row.get("to_lon") or 0.0),
                        "speed_limit": float(row.get("speed_limit_kmph") or 0.0),
                        "restriction_id": row.get("restriction_id"),
                        "description": row.get("description"),
                    }
                )
            except Exception:
                continue
        SPEED_RESTRICTIONS = records
        print(f"[OK] Loaded {len(records)} speed restriction rows from {sr_path.name}")
    except Exception as e:
        print(f"[ERROR] Failed to load speed restrictions: {e}")
        SPEED_RESTRICTIONS = []


load_speed_restrictions()


def load_route_sequences():
    """Load route station sequences from main_stations.csv."""
    global ROUTE_SEQUENCES
    try:
        routes_path = _find_base_csv("main_stations")
        if routes_path is None:
            print("[WARN] main_stations.csv not found in base-data/")
            ROUTE_SEQUENCES = {}
            return
        df = pl.read_csv(routes_path)
        df = df.rename({c: c.strip().lower() for c in df.columns})
        cols = set(df.columns)
        required = {"route_id", "major_stations"}
        missing = required - cols
        if missing:
            raise ValueError(f"main_stations file missing columns: {sorted(missing)}")

        for row in df.iter_rows(named=True):
            route_id = str(row.get("route_id") or "").strip().upper()
            stations_str = str(row.get("major_stations") or "").strip()
            if not route_id or not stations_str:
                continue
            # Parse comma-separated stations
            stations = [s.strip().upper() for s in stations_str.split(",") if s.strip()]
            if stations:
                ROUTE_SEQUENCES[route_id] = stations

        print(f"[OK] Loaded {len(ROUTE_SEQUENCES)} route sequences from {routes_path.name}")
    except Exception as e:
        print(f"[ERROR] Failed to load route sequences: {e}")
        ROUTE_SEQUENCES = {}


load_route_sequences()


def load_route_graph():
    """Load complete station graph from route_graph.csv into ROUTE_STATIONS."""
    global ROUTE_STATIONS, ROUTE_ADJACENCY
    try:
        graph_path = _find_base_csv("route_graph")
        if graph_path is None:
            print("[WARN] route_graph.csv not found in base-data/")
            ROUTE_STATIONS = {}
            ROUTE_ADJACENCY = {}
            return
        df = pl.read_csv(graph_path, truncate_ragged_lines=True)
        df = df.rename({c: c.strip().lower() for c in df.columns})

        route_stations: dict[str, set[str]] = {}
        route_edges: dict[str, set[tuple[str, str]]] = {}

        for row in df.iter_rows(named=True):
            section = str(row.get("section") or "").strip().upper()
            if not section:
                continue

            stations_set = route_stations.setdefault(section, set())
            edges_set = route_edges.setdefault(section, set())

            station = str(row.get("station") or "").strip().upper()
            if station:
                stations_set.add(station)

            next_nodes = _split_station_list(row.get("next_station"))
            optional_nodes = _split_station_list(row.get("optional_nodes"))
            for node in next_nodes + optional_nodes:
                stations_set.add(node)

            if station and next_nodes:
                chain = [station] + next_nodes
            else:
                chain = next_nodes

            for a, b in zip(chain, chain[1:]):
                if a and b:
                    edges_set.add((a, b))

        ROUTE_STATIONS = route_stations
        ROUTE_ADJACENCY = route_edges

        total_stations = sum(len(stations) for stations in route_stations.values())
        total_edges = sum(len(edges) for edges in route_edges.values())
        print(
            f"[OK] Loaded {len(route_stations)} route graphs with {total_stations} stations and {total_edges} edges from {graph_path.name}"
        )
    except Exception as e:
        print(f"[ERROR] Failed to load route graph: {e}")
        ROUTE_STATIONS = {}
        ROUTE_ADJACENCY = {}


load_route_graph()


# ------------------------------
# Health check
# ------------------------------
@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------
# Upload & Preview
# ------------------------------
@app.post("/load_csv")
async def load_csv(file: UploadFile):
    """Upload and load analysis CSV (large run file)."""
    global DF
    data = await file.read()
    if not data:
        DF = None
        return JSONResponse({"error": "empty file"}, status_code=400)
    buf = io.BytesIO(data)
    DF = pl.read_csv(
        buf,
        infer_schema_length=2000,
        null_values=["NULL", "Null", "null"],
        dtypes={
            "distFromSpeed": pl.Utf8,
            "distFromPrevLatLng": pl.Utf8,
            "BE Version": pl.Utf8,
            "GUI Version": pl.Utf8,
            "ODU Version": pl.Utf8,
            "DB Circle Count": pl.Utf8,
            "DB Polygon Count": pl.Utf8,
        },
    )
    DF = _repair_shifted_rows(DF)
    drop_cols = [
        "BE Version",
        "GUI Version",
        "ODU Version",
        "DB Circle Count",
        "DB Polygon Count",
    ]
    keep = [c for c in DF.columns if c not in drop_cols]
    DF = DF.select(keep)
    return {"rows": DF.height, "cols": DF.width, "columns": DF.columns}


@app.get("/preview")
def preview(n: int = 20):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    return {"data": DF.head(n).to_dicts()}


def _first_matching_column(columns: list[str], *keywords: str) -> str | None:
    """Return first column whose uppercase name contains all keywords."""
    up_keywords = [kw.upper() for kw in keywords]
    for col in columns:
        up_col = col.upper()
        if all(kw in up_col for kw in up_keywords):
            return col
    return None


def _norm_literal(value: Any) -> str:
    """Normalize literal strings similarly to DataFrame fields."""
    return re.sub(r"^\s+|\s+$", "", str(value)).upper()


def _geofences_for_station(station_code: str, direction: str | None) -> list[dict[str, Any]]:
    if not station_code:
        return []
    if not GEOFENCE_DATA:
        load_geofences()
    target = station_code.upper()
    dir_norm = direction.upper() if direction else None
    matches = [
        g for g in GEOFENCE_DATA
        if g["station_code"] == target and (not dir_norm or not g["direction"] or g["direction"] == dir_norm)
    ]
    if matches:
        return matches
    # fallback ignoring direction
    return [g for g in GEOFENCE_DATA if g["station_code"] == target]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute haversine distance in meters between two lat/lon points."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lam = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lam / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _geofence_limit(geo: dict[str, Any]) -> float:
    """Return dynamic tolerance for a geofence, honoring its radius plus margin."""
    radius = float(geo.get("radius_m") or 0.0)
    if radius <= 0:
        return GEOFENCE_TOLERANCE_M
    return max(GEOFENCE_TOLERANCE_M, radius + GEOFENCE_RADIUS_MARGIN_M)


def _within_geofence(lat: float | None, lon: float | None, geos: list[dict[str, Any]]) -> tuple[bool, float | None]:
    """
    Determine whether a point lies inside any geofence, using each entry's radius.
    Returns (is_inside, closest_distance) so callers can log/debug misses.
    """
    if lat is None or lon is None:
        return False, None
    closest = None
    for geo in geos:
        g_lat = geo.get("latitude")
        g_lon = geo.get("longitude")
        if g_lat is None or g_lon is None:
            continue
        dist = _haversine_m(lat, lon, g_lat, g_lon)
        if closest is None or dist < closest:
            closest = dist
        if dist <= _geofence_limit(geo):
            return True, dist
    return False, closest


def _station_coordinates(station_code: str, direction: str | None = None) -> tuple[float, float] | None:
    geos = _geofences_for_station(station_code, direction)
    if geos:
        g = geos[0]
        return (g.get("latitude"), g.get("longitude"))
    # fallback: any entry with this station
    for geo in GEOFENCE_DATA:
        if geo.get("station_code") == station_code.upper():
            return (geo.get("latitude"), geo.get("longitude"))
    return None


def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the compass bearing from point 1 to point 2 in degrees (0-360).
    0° = North, 90° = East, 180° = South, 270° = West
    """
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)

    x = math.sin(dlon_rad) * math.cos(lat2_rad)
    y = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)

    bearing_rad = math.atan2(x, y)
    bearing_deg = math.degrees(bearing_rad)
    return (bearing_deg + 360) % 360


def _validate_gps_trajectory(
    df: pl.DataFrame,
    start_idx: int,
    from_station: str,
    to_station: str,
    lat_col: str,
    lon_col: str,
) -> bool:
    """
    Validate that GPS trajectory after start_idx moves toward to_station.
    Returns True if movement direction matches route direction.
    """
    from_coords = _station_coordinates(from_station, None)
    to_coords = _station_coordinates(to_station, None)

    if not from_coords or not to_coords:
        return True  # Can't validate, assume OK

    expected_bearing = _calculate_bearing(from_coords[0], from_coords[1], to_coords[0], to_coords[1])

    # Get GPS points from start_idx to start_idx+100 (or less if file ends)
    check_window = min(100, df.height - start_idx)
    if check_window < 10:
        return True  # Too few points to validate

    subset = df.slice(start_idx, check_window)
    lats = subset[lat_col].cast(pl.Float64, strict=False).to_list()
    lons = subset[lon_col].cast(pl.Float64, strict=False).to_list()

    # Calculate actual bearings from consecutive valid GPS points
    bearings = []
    for i in range(len(lats) - 1):
        if lats[i] is not None and lons[i] is not None and lats[i+1] is not None and lons[i+1] is not None:
            if lats[i] != lats[i+1] or lons[i] != lons[i+1]:  # Movement detected
                bearing = _calculate_bearing(lats[i], lons[i], lats[i+1], lons[i+1])
                bearings.append(bearing)

    if not bearings:
        return True  # No movement data, assume OK

    # Average bearing of movement
    avg_bearing = sum(bearings) / len(bearings)

    # Check if average bearing is within ±90° of expected bearing
    bearing_diff = abs(avg_bearing - expected_bearing)
    if bearing_diff > 180:
        bearing_diff = 360 - bearing_diff

    return bearing_diff <= 90  # Allow ±90° tolerance


def _has_valid_adjacency_run(
    stations: list[str],
    adjacency_sets: list[set[tuple[str, str]]],
    origin_station: str | None,
) -> bool:
    """Check if the station list contains enough consecutive transitions found in the route graph."""
    if not stations or len(stations) < 2 or not adjacency_sets:
        return False
    origin = (origin_station or "").upper()
    yard_budget = STATION_YARD_TOLERANCE
    consecutive = 0
    prev = stations[0]
    transitions = 0

    for current in stations[1:]:
        if not current:
            prev = current
            continue
        transitions += 1
        edge_ok = any((prev, current) in edges for edges in adjacency_sets)
        if edge_ok:
            consecutive += 1
            if consecutive >= STATION_ADJACENCY_MIN_RUN:
                return True
        else:
            if yard_budget > 0 and (prev == origin or current == origin):
                yard_budget -= 1
            else:
                consecutive = 0

        prev = current
        if transitions >= STATION_SEQUENCE_MAX_STATIONS:
            break
    return False


def _validate_station_sequence(
    df: pl.DataFrame,
    start_idx: int,
    station_col: str,
    expected_sequence: list[str],
    min_matches: int = 3,
    route_key: str | None = None,
    direction: str | None = None,
    origin_station: str | None = None,
) -> bool:
    """
    Validate that stations after start_idx match the expected route sequence.
    Returns True if adjacency/sequence checks succeed within the lookahead window.
    """
    # Get station codes from start_idx onward (limited window)
    check_window = min(STATION_SEQUENCE_LOOKAHEAD_ROWS, df.height - start_idx)
    if check_window < 10:
        return True

    subset = df.slice(start_idx, check_window)
    if station_col not in subset.columns:
        return True  # Can't validate

    actual_sequence = _station_sequence_from_rows(subset, station_col)
    if not actual_sequence:
        return True  # No station data

    actual_sequence = actual_sequence[:STATION_SEQUENCE_MAX_STATIONS]
    adjacency_sets = _route_adjacency_sets(route_key, direction)
    if adjacency_sets and _has_valid_adjacency_run(actual_sequence, adjacency_sets, origin_station):
        return True

    if not expected_sequence or len(expected_sequence) < 2:
        return True  # No further sequence to validate

    # Check how many expected stations appear in order (within the same limited window)
    matches = 0
    expected_idx = 0

    for station in actual_sequence:
        if expected_idx < len(expected_sequence) and station == expected_sequence[expected_idx]:
            matches += 1
            expected_idx += 1
            if matches >= min_matches:
                return True

    return matches >= min_matches


def _find_station_row_idx(df: pl.DataFrame, station_col: str, value: str, first: bool) -> int | None:
    tmp = (
        df.with_row_count("_row_idx")
        .with_columns(_trim_expr(station_col).str.to_uppercase().alias("_station_norm"))
        .filter(pl.col("_station_norm") == value)
    )
    if tmp.height == 0:
        return None
    series = tmp["_row_idx"]
    idx = series.min() if first else series.max()
    return int(idx) if idx is not None else None


def _find_geofence_start_idx(
    df: pl.DataFrame,
    station_code: str,
    direction: str | None,
    to_station: str | None = None,
) -> int | None:
    """
    Find start index with intelligent validation:
    1. Primary: Station sequence matching (if station data quality >50%)
    2. Fallback: GPS trajectory direction validation
    """
    geos = _geofences_for_station(station_code, direction)
    if not geos:
        return None
    speed_col = _find_speed_column(df.columns)
    lat_col = _first_matching_column(df.columns, "LAT", "ITUDE") or _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON", "ITUDE") or _first_matching_column(df.columns, "LON")
    station_col = _first_matching_column(df.columns, "STATION", "CODE") or _first_matching_column(df.columns, "STATION")

    if not speed_col or not lat_col or not lon_col:
        return None

    # Get expected route sequence from main_stations.csv
    expected_sequence: list[str] = []
    route_key: str | None = None
    if to_station and direction:
        dir_norm = direction.strip().upper()
        # Try exact match first
        route_key = f"{station_code}-{to_station}".upper()
        if route_key in ROUTE_SEQUENCES:
            expected_sequence = ROUTE_SEQUENCES[route_key]
        else:
            # Try partial match: find route containing both stations in order
            for route_id, stations in ROUTE_SEQUENCES.items():
                if station_code.upper() in stations and to_station.upper() in stations:
                    from_idx = stations.index(station_code.upper())
                    to_idx = stations.index(to_station.upper())
                    if from_idx < to_idx:  # Correct direction
                        expected_sequence = stations[from_idx:to_idx + 1]
                        break

    # Check station data quality
    station_data_good = False
    if station_col and station_col in df.columns:
        total_rows = df.height
        non_null_count = df.filter(
            pl.col(station_col).is_not_null() &
            (pl.col(station_col).cast(pl.Utf8).str.len_bytes() > 0)
        ).height
        station_data_good = (non_null_count / max(total_rows, 1)) > 0.5

    augmented = (
        df.with_row_count("_row_idx")
        .with_columns([
            pl.col(speed_col).cast(pl.Float64, strict=False).alias("_speed_norm"),
            pl.col(speed_col).cast(pl.Float64, strict=False).shift(-1).alias("_speed_next"),
            pl.col(lat_col).cast(pl.Float64, strict=False).alias("_lat"),
            pl.col(lon_col).cast(pl.Float64, strict=False).alias("_lon"),
        ])
    )
    idxs = augmented["_row_idx"].to_list()
    speeds = augmented["_speed_norm"].to_list()
    speeds_next = augmented["_speed_next"].to_list()
    lats = augmented["_lat"].to_list()
    lons = augmented["_lon"].to_list()

    # Find ALL candidates, then validate each
    candidates = []
    for i in range(len(idxs)):
        sp = speeds[i]
        sp_next = speeds_next[i]
        if sp is None or sp_next is None:
            continue
        if sp < 1 and sp_next > 0:
            inside, _ = _within_geofence(lats[i], lons[i], geos)
            if inside:
                next_idx = idxs[i] + 1 if i + 1 < len(idxs) else idxs[i]
                candidates.append(int(next_idx))

    if not candidates:
        return None

    print(f"[DEBUG] Found {len(candidates)} candidates at {station_code}, expected_seq={expected_sequence[:5] if expected_sequence else None}, station_data_good={station_data_good}")

    # Route membership pre-filter: check if next 20 stations are ALL valid for this route
    if station_data_good and to_station and station_col and direction:
        # Build route key for membership check
        route_key = f"{station_code}-{to_station}".upper()

        # Find matching route in ROUTE_STATIONS (try exact match and aliases)
        valid_stations: set[str] | None = None
        if route_key in ROUTE_STATIONS:
            valid_stations = ROUTE_STATIONS[route_key]
        else:
            # Try aliases (e.g., CSMT-IGP might be in CSMT-JL route)
            for route_alias in _route_aliases(route_key, direction):
                if route_alias in ROUTE_STATIONS:
                    valid_stations = ROUTE_STATIONS[route_alias]
                    print(f"[DEBUG] Using route alias {route_alias} for {route_key}")
                    break

        if valid_stations:
            filtered_candidates = []

            for candidate_idx in candidates:
                # Get next 20 unique stations after candidate
                check_window = min(1000, df.height - candidate_idx)
                if check_window < 10:
                    filtered_candidates.append(candidate_idx)
                    continue

                subset = df.slice(candidate_idx, check_window)
                if station_col not in subset.columns:
                    filtered_candidates.append(candidate_idx)
                    continue

                # Extract first 20 unique stations
                first_stations = []
                prev = None
                for val in subset[station_col].to_list()[:1000]:
                    st = str(val).strip().upper() if val else ""
                    if st and st != prev and st != station_code.upper():
                        first_stations.append(st)
                        prev = st
                        if len(first_stations) >= 20:
                            break

                # Check if ALL stations are in the valid route stations set
                invalid_stations = [st for st in first_stations if st not in valid_stations]

                # Require at least 5 valid stations to be confident
                if len(first_stations) < 5:
                    print(f"[DEBUG] Candidate {candidate_idx}: route membership SKIP, insufficient stations ({len(first_stations)} < 5)")
                elif not invalid_stations:
                    # All stations are valid for this route
                    filtered_candidates.append(candidate_idx)
                    print(f"[DEBUG] Candidate {candidate_idx}: route membership PASS, next_stations={first_stations[:5]}")
                else:
                    # Found invalid stations (like ELSC), reject this candidate
                    print(f"[DEBUG] Candidate {candidate_idx}: route membership REJECT, invalid_stations={invalid_stations[:3]} not in route")

            candidates = filtered_candidates if filtered_candidates else candidates
            print(f"[DEBUG] After route membership filter: {len(candidates)} candidates remain")
        else:
            print(f"[DEBUG] No route graph found for {route_key}, skipping membership filter")

    # Validate each candidate
    for candidate_idx in candidates:
        # Primary: Station sequence validation (if data is good)
        if station_data_good and expected_sequence and station_col:
            is_valid = _validate_station_sequence(
                df,
                candidate_idx,
                station_col,
                expected_sequence,
                min_matches=3,
                route_key=route_key,
                direction=direction,
                origin_station=station_code,
            )
            print(f"[DEBUG] Candidate {candidate_idx}: station_sequence_valid={is_valid}")
            if is_valid:
                return candidate_idx
        # Fallback: GPS trajectory validation
        elif to_station and lat_col and lon_col:
            is_valid = _validate_gps_trajectory(df, candidate_idx, station_code, to_station, lat_col, lon_col)
            print(f"[DEBUG] Candidate {candidate_idx}: gps_trajectory_valid={is_valid}")
            if is_valid:
                return candidate_idx
        # No validation possible, accept first candidate
        elif not expected_sequence and not to_station:
            print(f"[DEBUG] Candidate {candidate_idx}: no validation possible, accepting")
            return candidate_idx

    # If no candidates passed validation, return first (legacy behavior)
    print(f"[DEBUG] No candidates passed validation, returning first: {candidates[0] if candidates else None}")
    return candidates[0] if candidates else None


def _find_geofence_end_idx(df: pl.DataFrame, station_code: str, direction: str | None, start_after: int | None = None) -> int | None:
    geos = _geofences_for_station(station_code, direction)
    if not geos:
        return None
    speed_col = _find_speed_column(df.columns)
    lat_col = _first_matching_column(df.columns, "LAT", "ITUDE") or _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON", "ITUDE") or _first_matching_column(df.columns, "LON")
    if not speed_col or not lat_col or not lon_col:
        return None
    augmented = (
        df.with_row_count("_row_idx")
        .with_columns([
            pl.col(speed_col).cast(pl.Float64, strict=False).alias("_speed_norm"),
            pl.col(lat_col).cast(pl.Float64, strict=False).alias("_lat"),
            pl.col(lon_col).cast(pl.Float64, strict=False).alias("_lon"),
        ])
    )
    idxs = augmented["_row_idx"].to_list()
    speeds = augmented["_speed_norm"].to_list()
    lats = augmented["_lat"].to_list()
    lons = augmented["_lon"].to_list()

    fallback_idx = None
    fallback_speed = None
    for i in range(len(idxs)):
        row_idx = idxs[i]

        # Skip rows before start_after
        if start_after is not None and row_idx <= start_after:
            continue

        sp = speeds[i]
        if sp is None:
            continue
        inside, _ = _within_geofence(lats[i], lons[i], geos)
        if not inside:
            continue
        if sp < 1:
            return int(row_idx)
        if fallback_idx is None or (fallback_speed is not None and sp < fallback_speed) or fallback_speed is None:
            fallback_idx = int(row_idx)
            fallback_speed = sp
    return fallback_idx


def _find_speed_column(columns: list[str]) -> str | None:
    for keys in [
        ("SPEED",),
        ("SPD",),
    ]:
        col = _first_matching_column(columns, *keys)
        if col:
            return col
    return None


def _find_time_column(columns: list[str]) -> str | None:
    for keys in [
        ("GPS", "TIME"),
        ("LOGGING", "TIME"),
        ("TIME",),
        ("TIMESTAMP",),
    ]:
        col = _first_matching_column(columns, *keys)
        if col:
            return col
    return None


def _parse_datetime_expr(col: str) -> pl.Expr:
    """Return expression that parses various timestamp formats."""
    exprs = [pl.col(col).str.strptime(pl.Datetime, strict=False)]
    custom_formats = [
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%y %H:%M:%S",
        "%d-%m-%Y %H:%M",
        "%d-%m-%y %H:%M",
    ]
    exprs.extend(pl.col(col).str.strptime(pl.Datetime, format=fmt, strict=False) for fmt in custom_formats)
    return pl.coalesce(exprs)


def _repair_shifted_rows(df: pl.DataFrame) -> pl.DataFrame:
    """
    Fix rows where GPS Time dropped and subsequent fields shifted left, causing
    station codes to appear in distFromSpeed and other columns to misalign.
    """
    gps_col = _first_matching_column(df.columns, "GPS", "TIME")
    logging_col = _first_matching_column(df.columns, "LOGGING", "TIME")
    lat_col = _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON")
    speed_col = _find_speed_column(df.columns)
    dist_prev_col = _first_matching_column(df.columns, "DIST", "PREV")
    dist_speed_col = _first_matching_column(df.columns, "DIST", "SPEED")
    station_col = _first_matching_column(df.columns, "STATION")
    if not all([gps_col, logging_col, lat_col, lon_col, speed_col, dist_prev_col, dist_speed_col, station_col]):
        return df

    station_is_empty = (
        pl.col(station_col).is_null()
        | (pl.col(station_col).cast(pl.Utf8).str.len_bytes().fill_null(0) == 0)
    )
    dist_has_letters = (
        pl.col(dist_speed_col).cast(pl.Utf8).str.contains(r"[A-Za-z]", literal=False).fill_null(False)
    )
    mask = station_is_empty & dist_has_letters
    if df.filter(mask).is_empty():
        return df

    return df.with_columns([
        pl.when(mask).then(pl.col(logging_col)).otherwise(pl.col(gps_col)).alias(gps_col),
        pl.when(mask).then(pl.col(gps_col)).otherwise(pl.col(lat_col)).alias(lat_col),
        pl.when(mask).then(pl.col(lat_col)).otherwise(pl.col(lon_col)).alias(lon_col),
        pl.when(mask).then(pl.col(lon_col)).otherwise(pl.col(speed_col)).alias(speed_col),
        pl.when(mask).then(pl.col(speed_col)).otherwise(pl.col(dist_prev_col)).alias(dist_prev_col),
        pl.when(mask).then(pl.col(dist_prev_col)).otherwise(pl.col(dist_speed_col)).alias(dist_speed_col),
        pl.when(mask).then(pl.col(dist_speed_col)).otherwise(pl.col(station_col)).alias(station_col),
    ])


def _find_station_stop_idx(df: pl.DataFrame, station_col: str, value: str, start_after: int | None = None) -> int | None:
    speed_col = _find_speed_column(df.columns)
    if not speed_col:
        return None
    tmp = (
        df.with_row_count("_row_idx")
        .with_columns([
            _trim_expr(station_col).str.to_uppercase().alias("_station_norm"),
            pl.col(speed_col).cast(pl.Float64, strict=False).alias("_speed_norm"),
        ])
        .filter(
            (pl.col("_station_norm") == value)
            & (pl.col("_speed_norm") < 1)
            & (
                pl.lit(True)
                if start_after is None
                else (pl.col("_row_idx") > start_after)
            )
        )
    )
    if tmp.height == 0:
        return None
    idx = tmp["_row_idx"].min()
    return int(idx) if idx is not None else None


# ------------------------------
# Filtering helpers (by headers only)
# ------------------------------
def apply_criteria(df: pl.DataFrame, crit: Dict[str, Any]) -> pl.DataFrame:
    out = df

    # station_code_contains -> try *station* or *code* column
    sc = crit.get("station_code_contains")
    if sc:
        col = _first_matching_column(out.columns, "STATION") or _first_matching_column(out.columns, "CODE")
        if col:
            out = out.filter(
                _trim_expr(col).str.to_uppercase().str.contains(_norm_literal(sc), literal=True)
            )

    # direction_equals
    deq = crit.get("direction_equals")
    dir_col = None
    if deq:
        dir_col = _first_matching_column(out.columns, "DIR") or _first_matching_column(out.columns, "DIRECTION")
        if dir_col:
            out = out.filter(_trim_expr(dir_col).str.to_uppercase() == _norm_literal(deq))

    # from/to station bounds with geofence-aware start selection
    station_col = (
        _first_matching_column(out.columns, "STATION", "CODE")
        or _first_matching_column(out.columns, "STATION")
    )
    from_eq = crit.get("from_station_equals")
    to_eq = crit.get("to_station_equals")
    norm_dir = _norm_literal(deq) if deq else None
    norm_from = _norm_literal(from_eq) if from_eq else None
    norm_to = _norm_literal(to_eq) if to_eq else None

    # If station names are available, drop any rows before the first occurrence of the origin station.
    if norm_from and station_col:
        first_station_idx = _find_station_row_idx(out, station_col, norm_from, first=True)
        if first_station_idx is not None and first_station_idx > 0:
            print(f"[DEBUG] Trimming rows before first {norm_from} occurrence at idx={first_station_idx}")
            out = out.slice(first_station_idx)

    from_idx = None
    to_idx = None
    if from_eq:
        from_idx = _find_geofence_start_idx(out, norm_from, norm_dir, to_station=norm_to)
        if from_idx is None and station_col:
            from_idx = _find_station_row_idx(out, station_col, norm_from, first=True)
    if to_eq:
        ge_end = _find_geofence_end_idx(out, norm_to, norm_dir, start_after=from_idx)
        station_stop = (
            _find_station_stop_idx(out, station_col, norm_to, start_after=from_idx)
            if station_col
            else None
        )
        print(f"[DEBUG] ge_end={ge_end}, station_stop={station_stop}")
        if ge_end is not None:
            to_idx = ge_end
            if station_stop is not None and station_stop >= ge_end:
                to_idx = station_stop
        elif station_col:
            to_idx = station_stop or _find_station_row_idx(out, station_col, norm_to, first=False)

    if from_idx is not None or to_idx is not None:
        print(f"[DEBUG] from_idx={from_idx}, to_idx={to_idx}")
        with_rows = out.with_row_count("_row_idx")
        lower = from_idx if from_idx is not None else 0
        upper = to_idx if to_idx is not None else with_rows.height - 1
        print(f"[DEBUG] Before swap: lower={lower}, upper={upper}")
        if upper < lower:
            lower, upper = upper, lower
            print(f"[DEBUG] After swap: lower={lower}, upper={upper}")
        out = with_rows.filter(
            (pl.col("_row_idx") >= lower) & (pl.col("_row_idx") <= upper)
        ).drop("_row_idx")

    # speed < value
    slt = crit.get("speed_lt")
    if slt is not None:
        speed_col = _find_speed_column(out.columns)
        if speed_col:
            out = out.with_columns(pl.col(speed_col).cast(pl.Float64, strict=False))
            out = out.filter(pl.col(speed_col) < float(slt))

    return out


@app.post("/analyze")
def analyze(criteria: Dict[str, Any] = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    filtered = apply_criteria(DF, criteria)
    return {"rows": filtered.height, "sample": filtered.head(50).to_dicts()}


# ------------------------------
# Excel Export
# ------------------------------
@app.post("/export")
def export(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    out = apply_criteria(DF, criteria)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        out.to_pandas().to_excel(writer, index=False, sheet_name="Filtered")
    buf.seek(0)
    fname = f"filtered_{int(time.time())}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


# ------------------------------
# Chart Data
# ------------------------------
@app.post("/chart_data")
def chart_data(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    out = apply_criteria(DF, criteria)
    if out.height == 0:
        return {"labels": [], "values": [], "yLabel": "Speed (km/h)"}

    ts_col = _find_time_column(out.columns)
    sp_col = _find_speed_column(out.columns)
    lat_col = _first_matching_column(out.columns, "LAT", "ITUDE") or _first_matching_column(out.columns, "LAT")
    lon_col = _first_matching_column(out.columns, "LON", "ITUDE") or _first_matching_column(out.columns, "LON")
    if not ts_col or not sp_col:
        return JSONResponse({"error": "required columns not found"}, status_code=400)

    route_key = _route_key(criteria.get("from_station_equals"), criteria.get("to_station_equals"))
    direction = criteria.get("direction_equals")
    relevant_restrictions = _matching_restrictions(route_key, direction)

    lat_expr = pl.col(lat_col).cast(pl.Float64, strict=False) if lat_col else pl.lit(None, dtype=pl.Float64)
    lon_expr = pl.col(lon_col).cast(pl.Float64, strict=False) if lon_col else pl.lit(None, dtype=pl.Float64)
    enriched = (
        out.with_columns(
            _parse_datetime_expr(ts_col).alias("_TS"),
            pl.col(sp_col).cast(pl.Float64, strict=False).alias("_SPD"),
            lat_expr.alias("_LAT_FLOAT"),
            lon_expr.alias("_LON_FLOAT"),
        )
        .drop_nulls(["_TS"])
        .with_columns(pl.col("_TS").dt.truncate("1m").alias("_MIN"))
    )

    restriction_limits = _restriction_limits_for_rows(enriched, relevant_restrictions)
    mps_limits = _section_limits_for_rows(enriched, route_key, direction)
    enriched = enriched.with_columns([
        pl.Series("_LIMIT", restriction_limits),
        pl.Series("_MPS_LIMIT", mps_limits),
    ])

    df2 = (
        enriched.group_by("_MIN")
        .agg([
            pl.col("_SPD").mean().alias("_AVG"),
            pl.col("_SPD").last().alias("_LAST"),
            pl.col("_LIMIT").min().alias("_LIMIT_MIN"),
            pl.col("_MPS_LIMIT").min().alias("_MPS"),
        ])
        .sort("_MIN")
    )
    labels = df2["_MIN"].dt.strftime("%Y-%m-%d %H:%M").to_list()
    values = [float(x) if x is not None else 0.0 for x in df2["_LAST"].to_list()]
    limit_values = [float(x) if x is not None else None for x in df2["_LIMIT_MIN"].to_list()]
    mps_values_raw = [float(x) if x is not None else None for x in df2["_MPS"].to_list()]
    mps_values = _forward_fill(mps_values_raw)
    segments = _build_restriction_segments(labels, limit_values)
    return {
        "labels": labels,
        "values": values,
        "yLabel": "Speed (km/h)",
        "restrictions": segments,
        "mps": mps_values,
    }


# ------------------------------
# Train Info (uses normalized key)
# ------------------------------
@app.get("/train_info")
def train_info(train_number: str = Query(..., description="Train number as printed")):
    global TRAINS_DF
    if TRAINS_DF is None:
        load_trains_base()
    if TRAINS_DF is None:
        return JSONResponse({"error": "trains base-data not loaded"}, status_code=500)

    tn_norm = re.sub(r"\s+", "", str(train_number)).upper()
    hits = TRAINS_DF.filter(pl.col("train_number_norm") == tn_norm)

    if hits.height == 0:
        return JSONResponse({"error": f"train {train_number} not found"}, status_code=404)

    row = hits.row(0, named=True)
    return {
        "train_number": row.get("train_number"),
        "train_name": row.get("train_name"),
        "from_station": row.get("from_station"),
        "to_station": row.get("to_station"),
        "departure_time": row.get("departure_time"),
        "arrival_time": row.get("arrival_time"),
        "direction": row.get("direction"),
    }


# ------------------------------
# Debug endpoint
# ------------------------------
@app.get("/debug/base_data")
def debug_base_data():
    status = "loaded" if TRAINS_DF is not None else "not loaded"
    cols = list(TRAINS_DF.columns) if TRAINS_DF is not None else []
    return {
        "base_data_dir": str(BASE_DATA_DIR),
        "trains_status": status,
        "columns": cols,
        "row_count": int(TRAINS_DF.height) if TRAINS_DF is not None else 0,
        "has_norm_col": ("train_number_norm" in cols) if TRAINS_DF is not None else False,
    }
def _route_key(from_station: str | None, to_station: str | None) -> str | None:
    if not from_station or not to_station:
        return None
    return f"{from_station.strip().upper()}-{to_station.strip().upper()}"


def _route_variants(route_key: str | None) -> list[str]:
    variants: list[str] = []
    if not route_key:
        return variants
    parts = route_key.split("-", 1)
    if len(parts) != 2:
        variants.append(route_key)
        return variants
    a, b = parts
    if a == b:
        variants.append(route_key)
        return variants
    variants.append(route_key)
    reversed_key = f"{b}-{a}"
    variants.append(reversed_key)
    return variants


def _stations_from_sections(sections: list[str]) -> list[str]:
    stations: list[str] = []
    for idx, sec in enumerate(sections):
        parts = sec.split("-")
        if len(parts) != 2:
            continue
        a, b = parts
        if idx == 0:
            stations.append(a)
        stations.append(b)
    return stations


def _route_aliases(route_key: str | None, direction: str | None) -> list[str]:
    aliases: list[str] = []
    def add(val: str | None):
        if val and val not in aliases:
            aliases.append(val)
    for variant in _route_variants(route_key):
        add(variant)
    if not route_key or not direction:
        return aliases
    dir_norm = direction.strip().upper()
    parts = route_key.split("-", 1)
    if len(parts) != 2:
        return aliases
    start, end = parts
    for (route_name, route_dir), sequences in ROUTE_SECTION_MAP.items():
        if route_dir != dir_norm:
            continue
        for seq in sequences:
            stations = _stations_from_sections(seq)
            if start in stations and end in stations:
                if stations.index(start) < stations.index(end):
                    add(route_name)
    override = ROUTE_ALIAS_OVERRIDES.get((route_key, dir_norm))
    if override:
        for rk in override:
            add(rk)
    return aliases


def _route_adjacency_sets(route_key: str | None, direction: str | None) -> list[set[tuple[str, str]]]:
    """
    Return adjacency sets (allowed station transitions) for the given route key/direction,
    including aliases.
    """
    if not ROUTE_ADJACENCY:
        return []
    sets: list[set[tuple[str, str]]] = []
    seen: set[str] = set()

    def add_key(key: str | None):
        if not key:
            return
        if key in seen:
            return
        edges = ROUTE_ADJACENCY.get(key)
        if edges:
            sets.append(edges)
            seen.add(key)

    add_key(route_key)
    for alias in _route_aliases(route_key, direction):
        add_key(alias)
    return sets


def _matching_restrictions(route_key: str | None, direction: str | None) -> list[dict[str, Any]]:
    if not route_key or not SPEED_RESTRICTIONS:
        return []
    dir_norm = direction.strip().upper() if direction else None
    variants = _route_aliases(route_key, direction)
    out = []
    for row in SPEED_RESTRICTIONS:
        if row["route"] not in variants:
            continue
        row_dir = row["direction"]
        if not row_dir or row_dir == "BOTH" or not dir_norm or row_dir == dir_norm:
            out.append(row)
    return out


def _find_point_index(lat_vals: list[float | None], lon_vals: list[float | None], target_lat: float, target_lon: float, start: int = 0) -> int | None:
    for idx in range(start, len(lat_vals)):
        lat = lat_vals[idx]
        lon = lon_vals[idx]
        if lat is None or lon is None:
            continue
        dist = _haversine_m(lat, lon, target_lat, target_lon)
        if dist <= RESTRICTION_POINT_TOLERANCE_M:
            return idx
    return None


def _find_station_point_index(lat_vals: list[float | None], lon_vals: list[float | None], target_lat: float, target_lon: float, start: int = 0) -> int | None:
    best_idx = None
    best_dist = None
    for idx in range(start, len(lat_vals)):
        lat = lat_vals[idx]
        lon = lon_vals[idx]
        if lat is None or lon is None:
            continue
        dist = _haversine_m(lat, lon, target_lat, target_lon)
        if dist <= MPS_POINT_TOLERANCE_M:
            return idx
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_idx = idx
    return best_idx


def _restriction_limits_for_rows(rows: pl.DataFrame, restrictions: list[dict[str, Any]]) -> list[float | None]:
    if not restrictions or "_LAT_FLOAT" not in rows.columns or "_LON_FLOAT" not in rows.columns:
        return [None] * rows.height
    lat_vals = rows["_LAT_FLOAT"].to_list()
    lon_vals = rows["_LON_FLOAT"].to_list()
    limits: list[float | None] = [None] * len(lat_vals)
    for r in restrictions:
        limit = float(r.get("speed_limit") or 0.0)
        if limit <= 0:
            continue
        start_idx = _find_point_index(lat_vals, lon_vals, r["from_lat"], r["from_lon"], 0)
        if start_idx is None:
            continue
        end_idx = _find_point_index(lat_vals, lon_vals, r["to_lat"], r["to_lon"], start_idx)
        if end_idx is None:
            continue
        for idx in range(start_idx, end_idx + 1):
            existing = limits[idx]
            if existing is None or limit < existing:
                limits[idx] = limit
    return limits


def _station_sequence_from_rows(rows: pl.DataFrame, station_col: str | None) -> list[str]:
    if station_col is None or station_col not in rows.columns:
        return []
    stations = [
        (str(val).strip().upper() if val is not None else "")
        for val in rows[station_col].to_list()
    ]
    seq: list[str] = []
    prev = None
    for st in stations:
        if not st:
            continue
        if st == prev:
            continue
        seq.append(st)
        prev = st
    return seq


def _score_route_match(expected_stations: list[str], actual_sequence: list[str]) -> float:
    """
    Score how well expected route stations match the actual telemetry sequence.

    Scoring:
    - Station found in correct order: +2.0 points
    - Station found but out of order: +0.5 points
    - Station missing from actual data: -1.0 point

    Returns score (higher is better).
    """
    if not expected_stations or not actual_sequence:
        return 0.0

    score = 0.0
    last_found_idx = -1

    for expected_station in expected_stations:
        if expected_station not in actual_sequence:
            # Station missing from actual data
            score -= 1.0
            continue

        # Find this station in actual sequence
        try:
            actual_idx = actual_sequence.index(expected_station, last_found_idx + 1)
            # Found in correct order (after previous station)
            score += 2.0
            last_found_idx = actual_idx
        except ValueError:
            # Station exists but appears before the last found station (out of order)
            # or doesn't exist after last_found_idx
            if expected_station in actual_sequence[:last_found_idx + 1]:
                # Out of order
                score += 0.5
            else:
                # Doesn't exist at all
                score -= 1.0

    return score


def _sections_for_route(route_key: str | None, direction: str | None, rows: pl.DataFrame) -> list[list[str]]:
    if not route_key or not direction:
        return []
    dir_norm = direction.strip().upper()
    sequences: list[list[str]] = []
    start = end = None
    if route_key and "-" in route_key:
        start, end = route_key.split("-", 1)

    # Extract actual station sequence from telemetry
    station_col = _first_matching_column(rows.columns, "STATION", "CODE") or _first_matching_column(rows.columns, "STATION")
    actual_sequence = _station_sequence_from_rows(rows, station_col)

    # Check data quality: if >50% of rows have NULL/empty stations, fallback to GPS matching
    station_data_quality_good = False
    if station_col and station_col in rows.columns:
        total_rows = rows.height
        non_null_count = rows.filter(
            pl.col(station_col).is_not_null() &
            (pl.col(station_col).cast(pl.Utf8).str.len_bytes() > 0)
        ).height
        station_data_quality_good = (non_null_count / max(total_rows, 1)) > 0.5

    for rk in _route_aliases(route_key, direction):
        key = (rk, dir_norm)
        if key not in ROUTE_SECTION_MAP:
            continue
        for seq in ROUTE_SECTION_MAP[key]:
            stations = _stations_from_sections(seq)
            if not stations:
                continue
            if start in stations and end in stations:
                i = stations.index(start)
                j = stations.index(end)
                if i < j:
                    sequences.append(seq[i:j])
            else:
                sequences.append(seq)

    # If multiple sequences found AND station data is good, use sequence-aware matching
    if len(sequences) > 1 and station_data_quality_good and actual_sequence:
        scored = []
        for seq in sequences:
            seq_stations = _stations_from_sections(seq)
            score = _score_route_match(seq_stations, actual_sequence)
            scored.append((score, seq))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score = scored[0][0]

        # Only filter if there's a clear winner (best score > 0)
        if best_score > 0:
            sequences = [seq for score, seq in scored if score == best_score]

    if sequences:
        return sequences
    parts = route_key.split("-")
    if len(parts) >= 2:
        stations = parts if dir_norm in ("DN", "") else list(reversed(parts))
        sequences.append([f"{stations[i]}-{stations[i+1]}" for i in range(len(stations) - 1)])
    return sequences


def _apply_section_sequence(lat_vals: list[float | None], lon_vals: list[float | None], sections: list[str], direction: str | None) -> list[float | None]:
    result: list[float | None] = [None] * len(lat_vals)
    cursor = 0
    for section in sections:
        if section not in MPS_CONFIG:
            continue
        parts = section.split("-")
        if len(parts) != 2:
            continue
        from_station, to_station = parts
        coords_from = _station_coordinates(from_station, direction)
        coords_to = _station_coordinates(to_station, direction)
        start_idx = _find_station_point_index(lat_vals, lon_vals, coords_from[0], coords_from[1], cursor) if coords_from else None
        if start_idx is None:
            continue
        end_idx = _find_station_point_index(lat_vals, lon_vals, coords_to[0], coords_to[1], start_idx or 0) if coords_to else None
        if end_idx is None:
            continue
        if start_idx > end_idx:
            start_idx, end_idx = end_idx, start_idx
        cursor = end_idx
        limit = MPS_CONFIG[section]
        for idx in range(start_idx, end_idx + 1):
            result[idx] = limit
    return result


def _section_limits_for_rows(rows: pl.DataFrame, route_key: str | None, direction: str | None) -> list[float | None]:
    sequences = _sections_for_route(route_key, direction, rows)
    if not sequences or "_LAT_FLOAT" not in rows.columns or "_LON_FLOAT" not in rows.columns:
        return [None] * rows.height
    lat_vals = rows["_LAT_FLOAT"].to_list()
    lon_vals = rows["_LON_FLOAT"].to_list()
    best: list[float | None] = [None] * len(lat_vals)
    best_length = -1
    for seq in sequences:
        seq_limits = _apply_section_sequence(lat_vals, lon_vals, seq, direction)
        length = sum(1 for v in seq_limits if v is not None)
        if length > best_length:
            best_length = length
            best = seq_limits
    return best


def _forward_fill(values: list[float | None]) -> list[float | None]:
    out: list[float | None] = []
    prev = None
    for val in values:
        if val is None:
            out.append(prev)
        else:
            prev = val
            out.append(val)
    return out


def _build_restriction_segments(labels: list[str], limits: list[float | None]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for idx, limit in enumerate(limits):
        if limit is None:
            if current:
                current["end"] = labels[idx - 1] if idx > 0 else labels[idx]
                segments.append(current)
                current = None
            continue
        limit = float(limit)
        if current is None:
            current = {"start": labels[idx], "limit": limit}
        elif abs(limit - current["limit"]) > 1e-6:
            current["end"] = labels[idx - 1]
            segments.append(current)
            current = {"start": labels[idx], "limit": limit}
    if current:
        current["end"] = labels[-1]
        segments.append(current)
    return segments
