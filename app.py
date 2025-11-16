from fastapi import FastAPI, UploadFile, Body, Query
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
import polars as pl
import pandas as pd
import io, time, re, math, sys
from typing import Dict, Any
from datetime import datetime
from pathlib import Path
import numpy as np

try:  # Optional heavy deps for PDF export
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from reportlab.lib import colors  # type: ignore
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak  # type: ignore
except ImportError:
    plt = None  # type: ignore[assignment]
    SimpleDocTemplate = None  # type: ignore[assignment]
    Paragraph = Spacer = Table = TableStyle = RLImage = PageBreak = None  # type: ignore[assignment]
    colors = None  # type: ignore[assignment]
    A4 = None  # type: ignore[assignment]
    getSampleStyleSheet = None  # type: ignore[assignment]

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
# Detect if running as PyInstaller EXE or as script
if getattr(sys, 'frozen', False):
    BASE_DIR = Path(sys.executable).parent  # EXE: use executable location
else:
    BASE_DIR = Path(__file__).parent  # Script: use script location

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
BRAKE_OFFSETS = [1000, 400, 300, 200, 100, 50, 20]
BRAKE_EVENT_TOLERANCE = 0.3
BRAKE_REQUIRED_DROP = 45.0
BRAKE_CHART_STEPS = sorted(set(BRAKE_OFFSETS + [0]), reverse=True)

# Section breakdowns for sectional charts
# Key = "FROM-TO", Value = list of stations for section boundaries
SECTION_BREAKDOWN = {
    "JL-CSMT": ["JL", "MMR", "IGP", "KSRA", "KYN", "CSMT"],
    "MMR-CSMT": ["MMR", "IGP", "KSRA", "KYN", "CSMT"],
    "IGP-CSMT": ["IGP", "KSRA", "KYN", "CSMT"],
    "PUNE-CSMT": ["PUNE", "LNL", "KJT", "KYN", "CSMT"],       # Via Kalyan
    "PUNE-CSMT-PNVL": ["PUNE", "LNL", "KJT", "PNVL", "CSMT"], # Via PNVL
    "RN-CSMT": ["RN", "CHI", "ROHA", "PNVL", "CSMT"],
    "ROHA-CSMT": ["ROHA", "PNVL", "CSMT"],
    "RN-BSR": ["RN", "ROHA", "PNVL", "BSR"],
    "ROHA-BSR": ["ROHA", "PNVL", "BSR"],
}

MAIL_STAFF: list[dict[str, Any]] = []
CLI_STAFF: list[dict[str, Any]] = []

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
    "IGP-MMR": 130.0,
    "MMR-IGP": 130.0,
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
    "PNVL-KJT": 110.0,
    # UP (PUNE → LTT)
    "KYN-LTT": 105.0,
    "KJT-PNVL": 110.0,
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


def load_mail_staff():
    """Load LP staff list from mail_staff.csv (root folder)."""
    global MAIL_STAFF
    try:
        path = BASE_DIR / "mail_staff.csv"
        if not path.exists():
            print("[WARN] mail_staff.csv not found; LP suggestions disabled")
            MAIL_STAFF = []
            return
        df = pl.read_csv(path)
        df = df.rename({c: c.strip().lower() for c in df.columns})
        required = {"cmsid", "employee name", "desg"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"mail_staff.csv missing columns: {sorted(missing)}")
        MAIL_STAFF = [
            {
                "cmsid": str(row.get("cmsid") or "").strip().upper(),
                "name": str(row.get("employee name") or "").strip(),
                "designation": str(row.get("desg") or "").strip(),
            }
            for row in df.iter_rows(named=True)
            if row.get("employee name")
        ]
        print(f"[OK] Loaded {len(MAIL_STAFF)} LP staff entries from {path.name}")
    except Exception as exc:
        print(f"[ERROR] Failed to load mail_staff.csv: {exc}")
        MAIL_STAFF = []


def load_cli_staff():
    """Load CLI/analyst roster for autocomplete suggestions."""
    global CLI_STAFF
    try:
        path = BASE_DIR / "cli data for upload - Sheet1.csv"
        if not path.exists():
            print("[WARN] CLI data CSV not found; CLI suggestions disabled")
            CLI_STAFF = []
            return
        df = pl.read_csv(path)
        df = df.rename({c: c.strip().lower() for c in df.columns})
        required = {"cli_cms_id", "cli_name", "current_office_code"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"CLI data CSV missing columns: {sorted(missing)}")
        CLI_STAFF = [
            {
                "cmsid": str(row.get("cli_cms_id") or "").strip().upper(),
                "name": str(row.get("cli_name") or "").strip(),
                "office": str(row.get("current_office_code") or "").strip().upper(),
            }
            for row in df.iter_rows(named=True)
            if row.get("cli_name")
        ]
        print(f"[OK] Loaded {len(CLI_STAFF)} CLI records from {path.name}")
    except Exception as exc:
        print(f"[ERROR] Failed to load CLI staff CSV: {exc}")
        CLI_STAFF = []


# def load_main_stations():
#     """Load main_stations.csv for route-based sectional charts."""
#     # NOT USED - Using SECTION_BREAKDOWN config instead
#     pass


load_mail_staff()
load_cli_staff()
# load_main_stations()  # Not needed - using SECTION_BREAKDOWN instead


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


def _find_distance_column(columns: list[str]) -> str | None:
    candidates = [
        ("DIST", "PREV"),
        ("DIST", "LAT"),
        ("DIST", "SPEED"),
        ("DIST",),
    ]
    for keys in candidates:
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


def _stringify_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


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
def _detect_halt_markers_from_enriched(enriched_df: pl.DataFrame, original_columns: list[str]) -> list[dict[str, Any]]:
    """
    Detect halts with 500m spacing for station labels on speed profile chart.
    Works with enriched DataFrame that has _TS, _SPD columns.
    Returns list of {timestamp, station} for each halt.
    """
    # Get station column from original columns
    station_col = _first_matching_column(original_columns, "STATION", "CODE") or _first_matching_column(original_columns, "STATION")
    # Look for distance column - matches distFromPrevLatLng, distFromSpeed, or DISTANCE
    dist_col = _first_matching_column(original_columns, "DISTANCE") or \
               _first_matching_column(original_columns, "dist", "From") or \
               _first_matching_column(original_columns, "dist")

    print(f"[DEBUG HALT] Station column: {station_col}, Distance column: {dist_col}")
    print(f"[DEBUG HALT] Enriched DF columns: {enriched_df.columns}")

    if not dist_col:
        print("[DEBUG HALT] No distance column found, returning empty")
        return []

    # Extract data from enriched DataFrame
    speeds = enriched_df["_SPD"].to_list()
    timestamps = enriched_df["_TS"].to_list()

    # Get stations if column exists
    if station_col and station_col in enriched_df.columns:
        stations = enriched_df[station_col].cast(pl.Utf8).to_list()
    else:
        stations = [None] * len(speeds)

    # Get distance steps from original column
    if dist_col in enriched_df.columns:
        dist_steps = enriched_df[dist_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    else:
        return []

    # Calculate cumulative distance
    cumulative: list[float] = []
    running = 0.0
    for step in dist_steps:
        running += max(0.0, step or 0.0)
        cumulative.append(running)

    # Find first movement
    first_move_idx = next((i for i, sp in enumerate(speeds) if sp and sp > 0.5), None)
    print(f"[DEBUG HALT] First movement index: {first_move_idx}, Total rows: {len(speeds)}")
    if first_move_idx is None:
        print("[DEBUG HALT] No movement detected, returning empty")
        return []

    # Detect halts with 500m spacing
    halts: list[dict[str, Any]] = []
    halts_before_filter: list[dict[str, Any]] = []
    last_halt_distance: float | None = None

    # Add starting station (before first movement) if it has a station code
    if first_move_idx > 0:
        start_idx = 0
        start_station = stations[start_idx]
        display_start = str(start_station).strip() if start_station and start_station != "None" else None

        if display_start:
            halts.append({
                "timestamp": timestamps[start_idx],
                "station": display_start,
            })
            last_halt_distance = cumulative[start_idx]
            print(f"[DEBUG HALT] Added starting station: {display_start} at row {start_idx}")

    for idx in range(first_move_idx + 1, len(speeds)):
        prev_speed = speeds[idx - 1]
        current_speed = speeds[idx]

        # Halt detected (speed drops to <=0.5)
        if current_speed <= 0.5 and prev_speed > 0.5:
            halt_distance = cumulative[idx]
            halt_station = stations[idx]
            display_station = str(halt_station).strip() if halt_station and halt_station != "None" else None

            halts_before_filter.append({
                "distance": halt_distance,
                "station": display_station,
            })

            # Apply 500m spacing rule
            if last_halt_distance is not None and (halt_distance - last_halt_distance) < 500.0:
                continue

            if display_station:
                halts.append({
                    "timestamp": timestamps[idx],
                    "station": display_station,
                })
                last_halt_distance = halt_distance

    print(f"[DEBUG HALT] Found {len(halts_before_filter)} raw halts: {[(h['distance'], h['station']) for h in halts_before_filter[:10]]}")
    print(f"[DEBUG HALT] After 500m filter + station check: {len(halts)} halts")

    return halts


def _map_halts_to_chart(halt_markers: list[dict[str, Any]], chart_timestamps: list[Any]) -> list[str | None]:
    """
    Map halt station names to chart x-axis labels.
    Returns list parallel to chart_timestamps with station names or None.
    """
    result: list[str | None] = [None] * len(chart_timestamps)

    if not halt_markers:
        return result

    for halt in halt_markers:
        halt_ts = halt["timestamp"]
        station = halt["station"]

        if halt_ts is None:
            continue

        # Find closest chart timestamp
        best_idx = None
        min_diff_seconds = None

        for idx, chart_ts in enumerate(chart_timestamps):
            if chart_ts is None:
                continue

            try:
                # Both should be datetime objects from Polars
                diff_seconds = abs((halt_ts - chart_ts).total_seconds())

                if min_diff_seconds is None or diff_seconds < min_diff_seconds:
                    min_diff_seconds = diff_seconds
                    best_idx = idx
            except (AttributeError, TypeError):
                # Skip if timestamp comparison fails
                continue

        # Assign station to closest chart point (within 2 minutes tolerance)
        if best_idx is not None and min_diff_seconds is not None and min_diff_seconds < 120:
            result[best_idx] = station

    return result


def _build_chart_payload(dataset: pl.DataFrame, criteria: dict) -> Dict[str, Any]:
    if dataset.height == 0:
        return {"labels": [], "values": [], "yLabel": "Speed (km/h)", "restrictions": [], "mps": []}

    ts_col = _find_time_column(dataset.columns)
    sp_col = _find_speed_column(dataset.columns)
    lat_col = _first_matching_column(dataset.columns, "LAT", "ITUDE") or _first_matching_column(dataset.columns, "LAT")
    lon_col = _first_matching_column(dataset.columns, "LON", "ITUDE") or _first_matching_column(dataset.columns, "LON")
    if not ts_col or not sp_col:
        raise ValueError("required columns not found")

    route_key = _route_key(criteria.get("from_station_equals"), criteria.get("to_station_equals"))
    direction = criteria.get("direction_equals")
    relevant_restrictions = _matching_restrictions(route_key, direction)

    lat_expr = pl.col(lat_col).cast(pl.Float64, strict=False) if lat_col else pl.lit(None, dtype=pl.Float64)
    lon_expr = pl.col(lon_col).cast(pl.Float64, strict=False) if lon_col else pl.lit(None, dtype=pl.Float64)
    enriched = (
        dataset.with_columns(
            _parse_datetime_expr(ts_col).alias("_TS"),
            pl.col(sp_col).cast(pl.Float64, strict=False).alias("_SPD"),
            lat_expr.alias("_LAT_FLOAT"),
            lon_expr.alias("_LON_FLOAT"),
        )
        .drop_nulls(["_TS"])
        .with_columns(pl.col("_TS").dt.truncate("1m").alias("_MIN"))
    )

    coach_type = criteria.get("coach_type")
    restriction_limits = _restriction_limits_for_rows(enriched, relevant_restrictions, coach_type)
    mps_limits = _section_limits_for_rows(enriched, route_key, direction, coach_type)
    enriched = enriched.with_columns([
        pl.Series("_LIMIT", restriction_limits),
        pl.Series("_MPS_LIMIT", mps_limits),
    ])

    # Detect halts with 500m spacing for station labels on chart (before aggregation)
    halt_markers = _detect_halt_markers_from_enriched(enriched, dataset.columns)
    print(f"[DEBUG] Detected {len(halt_markers)} halt markers with stations: {[h['station'] for h in halt_markers]}")

    df2 = (
        enriched.group_by("_MIN")
        .agg([
            pl.col("_SPD").mean().alias("_AVG"),
            pl.col("_SPD").last().alias("_LAST"),
            pl.col("_LIMIT").min().alias("_LIMIT_MIN"),
            pl.col("_MPS_LIMIT").min().alias("_MPS"),
            pl.col("_TS").first().alias("_FIRST_TS"),
        ])
        .sort("_MIN")
    )
    labels = df2["_MIN"].dt.strftime("%Y-%m-%d %H:%M").to_list()
    values = [float(x) if x is not None else 0.0 for x in df2["_LAST"].to_list()]
    limit_values = [float(x) if x is not None else None for x in df2["_LIMIT_MIN"].to_list()]
    mps_values_raw = [float(x) if x is not None else None for x in df2["_MPS"].to_list()]
    mps_values = _forward_fill(mps_values_raw)
    segments = _build_restriction_segments(labels, limit_values)

    # Map halts to chart labels (minute-aggregated)
    chart_timestamps = df2["_FIRST_TS"].to_list()
    station_labels = _map_halts_to_chart(halt_markers, chart_timestamps)
    print(f"[DEBUG] Mapped {sum(1 for s in station_labels if s)} stations to {len(station_labels)} chart points")

    return {
        "labels": labels,
        "values": values,
        "yLabel": "Speed (km/h)",
        "restrictions": segments,
        "mps": mps_values,
        "limit_values": limit_values,
        "station_labels": station_labels,
    }


@app.post("/chart_data")
def chart_data(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    out = apply_criteria(DF, criteria)
    try:
        payload = _build_chart_payload(out, criteria)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return payload

@app.post("/braking_profile")
def braking_profile(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    filtered = apply_criteria(DF, criteria)
    halts = _braking_profile(filtered, BRAKE_OFFSETS)
    return {"offsets": BRAKE_OFFSETS, "halts": halts}


@app.post("/brake_tests")
def brake_tests(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    filtered = apply_criteria(DF, criteria)
    start_station = criteria.get("from_station_equals")
    direction = criteria.get("direction_equals")
    summary = _brake_tests(filtered, start_station, direction)
    return summary


def _first_datetime(dataset: pl.DataFrame) -> datetime | None:
    ts_col = _find_time_column(dataset.columns)
    if not ts_col:
        return None
    series = (
        dataset.select(_parse_datetime_expr(ts_col).alias("_TS"))
        .drop_nulls()
        .get_column("_TS")
    )
    return series[0] if len(series) else None


def _last_datetime(dataset: pl.DataFrame) -> datetime | None:
    ts_col = _find_time_column(dataset.columns)
    if not ts_col:
        return None
    series = (
        dataset.select(_parse_datetime_expr(ts_col).alias("_TS"))
        .drop_nulls()
        .get_column("_TS")
    )
    return series[-1] if len(series) else None


# OLD FUNCTIONS - Not used anymore, replaced by SECTION_BREAKDOWN approach
# def _get_journey_sections(...): pass
# def _find_section_boundary(...): pass


def _get_section_breakdown(from_station: str, to_station: str) -> list[str] | None:
    """
    Get section breakdown for a route, handling LTT/DR replacement and auto-reverse.
    Returns list of station codes for section boundaries, or None if not found.
    """
    from_norm = _norm_literal(from_station)
    to_norm = _norm_literal(to_station)

    # Try exact match first
    route_key = f"{from_norm}-{to_norm}"
    breakdown = SECTION_BREAKDOWN.get(route_key)

    if breakdown:
        # Replace CSMT with LTT/DR if actual end station is LTT/DR
        if to_norm in ["LTT", "DR"] and "CSMT" in breakdown:
            breakdown = [to_norm if s == "CSMT" else s for s in breakdown]
        return breakdown

    # Try reverse route
    reverse_key = f"{to_norm}-{from_norm}"
    reverse_breakdown = SECTION_BREAKDOWN.get(reverse_key)

    if reverse_breakdown:
        # Reverse the breakdown and replace CSMT with LTT/DR if needed
        breakdown = list(reversed(reverse_breakdown))
        if from_norm in ["LTT", "DR"] and "CSMT" in breakdown:
            breakdown = [from_norm if s == "CSMT" else s for s in breakdown]
        return breakdown

    return None


def _generate_sectional_charts(dataset: pl.DataFrame, criteria: dict) -> list[io.BytesIO]:
    """
    Generate sectional speed profile charts for the journey.
    Uses actual halt stations detected in the data, filtered to configured breakdown.
    Returns list of chart image buffers (2 charts per page).
    """
    from_station = criteria.get("from_station_equals")
    to_station = criteria.get("to_station_equals")

    if not from_station or not to_station:
        print("[SECTION] Missing from/to station in criteria")
        return []

    # Get section breakdown for this route
    breakdown = _get_section_breakdown(from_station, to_station)

    if not breakdown:
        print(f"[SECTION] No section breakdown configured for {from_station} → {to_station}")
        return []

    print(f"[SECTION] Using breakdown: {' → '.join(breakdown)}")

    # Find station column and prepare data
    ts_col = _find_time_column(dataset.columns)
    sp_col = _find_speed_column(dataset.columns)
    station_col = _first_matching_column(dataset.columns, "station", "code") or \
                  _first_matching_column(dataset.columns, "station") or \
                  _first_matching_column(dataset.columns, "LOCATION", "CODE")

    if not ts_col or not sp_col or not station_col:
        print(f"[SECTION] Required columns not found (ts={ts_col}, sp={sp_col}, station={station_col})")
        return []

    # Create enriched dataset with parsed timestamps
    enriched = dataset.with_columns([
        _parse_datetime_expr(ts_col).alias("_TS"),
        pl.col(sp_col).cast(pl.Float64, strict=False).alias("_SPD"),
        pl.col(station_col).cast(pl.Utf8).alias("_STATION"),
    ])

    # Build sections directly from breakdown configuration
    # For each pair (breakdown[i] → breakdown[i+1]), find timestamps in the data
    sections = []

    for i in range(len(breakdown) - 1):
        from_station = breakdown[i]
        to_station = breakdown[i + 1]

        # Find first occurrence of from_station in the data
        from_rows = enriched.filter(pl.col("_STATION") == from_station)
        if from_rows.height == 0:
            print(f"[SECTION] Station {from_station} not found in data, skipping section {from_station} → {to_station}")
            continue

        # For starting station (i=0), use first occurrence
        # For others, use last occurrence (in case of multiple halts)
        if i == 0:
            from_ts = from_rows["_TS"][0]
        else:
            from_ts = from_rows["_TS"][-1]

        # Find last occurrence of to_station
        to_rows = enriched.filter(pl.col("_STATION") == to_station)
        if to_rows.height == 0:
            print(f"[SECTION] Station {to_station} not found in data, skipping section {from_station} → {to_station}")
            continue

        to_ts = to_rows["_TS"][-1]

        # Validate timestamp range
        if from_ts is None or to_ts is None:
            print(f"[SECTION] Null timestamps for {from_station} → {to_station}, skipping")
            continue

        if from_ts >= to_ts:
            print(f"[SECTION] Invalid timestamp range for {from_station} → {to_station} ({from_ts} >= {to_ts}), skipping")
            continue

        sections.append({
            'from_station': from_station,
            'to_station': to_station,
            'from_timestamp': from_ts,
            'to_timestamp': to_ts,
        })
        print(f"[SECTION] Added section {from_station} → {to_station} ({from_ts} to {to_ts})")

    if not sections:
        print("[SECTION] No valid sections found")
        return []

    print(f"[SECTION] Generated {len(sections)} sections")

    chart_images = []

    for section in sections:
        section_from = section['from_station']
        section_to = section['to_station']
        from_ts = section['from_timestamp']
        to_ts = section['to_timestamp']

        # Filter data by timestamp range (more reliable than row indices)
        section_data = enriched.filter(
            (pl.col("_TS") >= from_ts) & (pl.col("_TS") <= to_ts)
        )

        if section_data.height == 0:
            print(f"[SECTION] No data for {section_from} → {section_to}, skipping")
            continue

        # Build chart payload for this section
        try:
            payload = _build_chart_payload(section_data, criteria)
            payload["section_title"] = f"{section_from} → {section_to}"

            # Render chart
            chart_img = _render_speed_chart_image(payload)
            if chart_img:
                chart_images.append(chart_img)
                print(f"[SECTION] ✓ Generated chart for {section_from} → {section_to}")
        except Exception as e:
            print(f"[SECTION] Error generating chart for {section_from} → {section_to}: {e}")
            continue

    print(f"[SECTION] Generated {len(chart_images)} sectional charts")
    return chart_images


def _build_summary_details(dataset: pl.DataFrame, criteria: dict) -> dict[str, Any]:
    first_dt = _first_datetime(dataset)
    last_dt = _last_datetime(dataset)
    working_date = first_dt.strftime("%d/%m/%Y") if isinstance(first_dt, datetime) else "-"
    analysis_date = datetime.utcnow().strftime("%d/%m/%Y")
    start_time_str = first_dt.strftime("%H:%M") if isinstance(first_dt, datetime) else "-"
    end_time_str = last_dt.strftime("%H:%M") if isinstance(last_dt, datetime) else "-"
    return {
        "working_date": working_date,
        "analysis_date": analysis_date,
        "train_number": criteria.get("train_number"),
        "loco_number": criteria.get("loco_number"),
        "coach_type": criteria.get("coach_type"),
        "lp_name": criteria.get("lp_name"),
        "ncli_name": criteria.get("ncli_name"),
        "analyst_name": criteria.get("analyst_name"),
        "from_station": criteria.get("from_station_equals"),
        "to_station": criteria.get("to_station_equals"),
        "direction": criteria.get("direction_equals"),
        "row_count": dataset.height,
        "start_time": start_time_str,
        "end_time": end_time_str,
    }


def _render_speed_chart_image(payload: dict[str, Any]) -> io.BytesIO | None:
    if plt is None:
        raise RuntimeError("matplotlib is required for PDF export. Please install it.")
    labels = payload.get("labels") or []
    values = payload.get("values") or []
    if not labels or not values:
        return None
    x = list(range(len(labels)))

    # Add section title if present
    section_title = payload.get("section_title")
    if section_title:
        fig, ax = plt.subplots(figsize=(8, 2.8))
        fig.suptitle(section_title, fontsize=10, fontweight='bold', y=0.98)
    else:
        fig, ax = plt.subplots(figsize=(8, 3))
    ax.plot(x, values, color="#0c6cf2", linewidth=1.6)
    limit_vals = payload.get("limit_values") or []
    if len(limit_vals) == len(x) and any(v is not None for v in limit_vals):
        limit_series = [v if v is not None else float("nan") for v in limit_vals]
        ax.fill_between(x, 0, limit_series, color="salmon", alpha=0.2, step="mid", label="PSR")
        ax.plot(x, limit_series, color="#d9534f", linewidth=1.2, linestyle="--")
    mps = payload.get("mps") or []
    if any(v is not None for v in mps):
        ax.plot(x, [v if v is not None else float("nan") for v in mps], label="MPS", color="#ffa500", linewidth=1.2)
    ax.set_ylabel("Speed (km/h)")
    ax.set_xlabel("Time")

    # Get station labels if available and add vertical markers
    station_labels = payload.get("station_labels") or []
    for i, station in enumerate(station_labels):
        if station:
            # Draw vertical dashed line at halt
            ax.axvline(x=i, color='gray', linestyle='--', linewidth=0.8, alpha=0.5)
            # Add station label at top of chart
            y_max = max(values) if values else 100
            ax.text(i, y_max * 0.95, station, rotation=90, va='top', ha='right',
                   fontsize=7, color='#333', bbox=dict(boxstyle='round,pad=0.3',
                   facecolor='white', edgecolor='none', alpha=0.7))

    # Create x-axis labels (time only)
    tick_count = min(6, len(labels))
    if tick_count > 0:
        step = max(1, len(labels) // tick_count)
        tick_positions = x[::step]
        tick_labels = [labels[i].split(" ")[-1] for i in range(0, len(labels), step)]

        ax.set_xticks(tick_positions)
        ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=7)
    ax.grid(True, linestyle="--", alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="PNG", dpi=150)
    plt.close(fig)
    buf.seek(0)
    return buf


def _render_brake_curve_images(curve_data: list[dict[str, Any]]) -> list[io.BytesIO]:
    """
    Render smooth braking curves using full telemetry data.
    Creates multiple charts (6 halts per chart) for PDF.

    Args:
        curve_data: Output from _braking_profile_full_curve() with curve_data field

    Returns:
        List of PNG image buffers (one per 6 halts)
    """
    if plt is None:
        raise RuntimeError("matplotlib is required for PDF export. Please install it.")
    if not curve_data:
        return []

    images: list[io.BytesIO] = []

    # Process in batches of 6 halts
    for batch_start in range(0, len(curve_data), 6):
        batch = curve_data[batch_start:batch_start + 6]

        fig, ax = plt.subplots(figsize=(8, 2.5))  # Shorter for 2-per-page layout
        color_map = plt.cm.tab10(np.linspace(0, 1, len(batch)))

        for idx, halt in enumerate(batch):
            color = tuple(color_map[idx])
            curve = halt.get("curve_data", {})
            distances = curve.get("distances", [])
            speeds = curve.get("speeds", [])

            # Filter out null speeds
            valid_pairs = [(d, s) for d, s in zip(distances, speeds) if s is not None]

            if len(valid_pairs) >= 2:
                d, s = zip(*valid_pairs)
                label = halt.get("station") or f"Halt {halt.get('sequence')}"
                ax.plot(d, s, color=color, linewidth=1.8, label=label, alpha=0.85)

        # X-axis: 100m intervals from 1000m to 0m
        ax.set_xticks([1000, 900, 800, 700, 600, 500, 400, 300, 200, 100, 0])
        ax.set_xticklabels(['1000', '900', '800', '700', '600', '500', '400', '300', '200', '100', '0'])
        ax.set_xlabel("Distance before halt (m)", fontsize=9)
        ax.set_ylabel("Speed (km/h)", fontsize=9)
        ax.set_xlim(1050, -50)  # Right-to-left (1000m → 0m)
        ax.grid(True, linestyle="--", alpha=0.3)
        ax.legend(fontsize=7, loc='upper right')
        ax.tick_params(labelsize=8)
        fig.tight_layout()

        # Save to buffer
        buf = io.BytesIO()
        fig.savefig(buf, format="PNG", dpi=100)
        plt.close(fig)
        buf.seek(0)
        images.append(buf)

    return images


def _render_pdf_report(
    summary: dict[str, Any],
    speed_chart: io.BytesIO | None,
    brake_charts: list[io.BytesIO],
    halts: list[dict[str, Any]],
    brake_tests: dict[str, Any],
    sectional_charts: list[io.BytesIO] | None = None,
) -> io.BytesIO:
    if SimpleDocTemplate is None:
        raise RuntimeError("reportlab is required for PDF export. Please install it.")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("CR • RTIS Analysis Report", styles["Title"]))
    story.append(Spacer(1, 12))

    summary_rows = [
        ["Date of Working", summary.get("working_date") or "-"],
        ["Date of Analysis", summary.get("analysis_date") or "-"],
        ["Section", f"{summary.get('from_station') or '-'} → {summary.get('to_station') or '-'}"],
        ["Train Number", summary.get("train_number") or "-"],
        ["Loco Number", summary.get("loco_number") or "-"],
        ["Coach Type", summary.get("coach_type") or "-"],
        ["LP", summary.get("lp_name") or "-"],
        ["NCLI", summary.get("ncli_name") or "-"],
        ["Analyzed By", summary.get("analyst_name") or "-"],
        ["Rows Analyzed", str(summary.get("row_count") or 0)],
        ["Start Time", summary.get("start_time") or "-"],
        ["End Time", summary.get("end_time") or "-"],
    ]
    summary_table = Table(summary_rows, colWidths=[180, 360])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 16))

    if speed_chart:
        story.append(Paragraph("Speed Profile (Full Journey)", styles["Heading2"]))
        speed_chart.seek(0)
        story.append(RLImage(speed_chart, width=doc.width, height=200))
        story.append(Spacer(1, 16))

    # Add sectional speed profile charts (2 per page)
    if sectional_charts:
        story.append(Paragraph("Sectional Speed Profiles", styles["Heading2"]))
        story.append(Spacer(1, 8))

        for idx, chart_img in enumerate(sectional_charts):
            # Add page break after every 2 charts (but not before first chart)
            if idx > 0 and idx % 2 == 0:
                story.append(PageBreak())
            elif idx > 0:
                story.append(Spacer(1, 12))  # Small space between charts on same page

            chart_img.seek(0)
            story.append(RLImage(chart_img, width=doc.width, height=185))

        story.append(Spacer(1, 16))

    # Add braking curve charts (2 per page)
    if brake_charts:
        story.append(PageBreak())
        total_halts = len(halts)
        for idx, chart_img in enumerate(brake_charts):
            # Add page break after every 2 charts (but not before first chart)
            if idx > 0 and idx % 2 == 0:
                story.append(PageBreak())
            elif idx > 0:
                story.append(Spacer(1, 12))  # Small space between charts on same page

            # Chart title with halt range
            halt_start = idx * 6 + 1
            halt_end = min((idx + 1) * 6, total_halts)
            title = f"Braking Curves (Halts {halt_start}-{halt_end})" if total_halts > 6 else "Braking Curves"
            story.append(Paragraph(title, styles["Heading3"]))

            chart_img.seek(0)
            story.append(RLImage(chart_img, width=doc.width, height=155))

        story.append(Spacer(1, 16))

    if halts:
        story.append(Paragraph("Braking Pattern Table", styles["Heading2"]))
        header = ["Halt"] + [f"{offset} m" for offset in BRAKE_OFFSETS] + ["0 m"]
        data = [header]
        for halt in halts[:8]:
            row = [halt.get("station") or f"Halt {halt.get('sequence')}"]
            for offset in BRAKE_OFFSETS:
                reading = (halt.get("speeds") or {}).get(str(offset))
                if reading and isinstance(reading.get("speed"), (int, float)):
                    row.append(f"{reading['speed']:.1f}")
                else:
                    row.append("—")
            row.append("0.0")
            data.append(row)
        col_count = len(header)
        brake_table = Table(
            data,
            colWidths=[120] + [ (doc.width - 120) / (col_count - 1) ] * (col_count - 1),
        )
        brake_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(brake_table)
        story.append(Spacer(1, 16))

    if brake_tests:
        story.append(Paragraph("Brake Tests", styles["Heading2"]))
        rows = [["Test", "Start Speed (km/h)", "Dropped To (km/h)", "Status"]]
        for key, label in (("feel", "Brake Feel"), ("power", "Brake Power")):
            data = brake_tests.get(key) or {}
            rows.append([
                label,
                f"{data.get('start_speed'):.1f}" if isinstance(data.get("start_speed"), (int, float)) else "—",
                f"{data.get('end_speed'):.1f}" if isinstance(data.get("end_speed"), (int, float)) else "—",
                data.get("status", "NOT RUN"),
            ])
        tests_table = Table(rows, colWidths=[140, 120, 120, 120])
        tests_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(tests_table)

    doc.build(story)
    buffer.seek(0)
    return buffer


@app.post("/export_pdf")
def export_pdf(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    filtered = apply_criteria(DF, criteria)
    if filtered.height == 0:
        return JSONResponse({"error": "no data matches the selected criteria"}, status_code=400)
    try:
        chart_payload = _build_chart_payload(filtered, criteria)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    # Use unified function for both table and chart (same filtering)
    unified_data = _braking_profile_full_curve(filtered, BRAKE_OFFSETS)

    brake_tests = _brake_tests(filtered, criteria.get("from_station_equals"), criteria.get("direction_equals"))
    summary = _build_summary_details(filtered, criteria)
    try:
        speed_chart = _render_speed_chart_image(chart_payload)
        brake_charts = _render_brake_curve_images(unified_data)  # Returns list of images
        sectional_charts = _generate_sectional_charts(filtered, criteria)  # Generate sectional speed profiles
        pdf_buffer = _render_pdf_report(summary, speed_chart, brake_charts, unified_data, brake_tests, sectional_charts)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    filename = f"rtis_report_{int(time.time())}.pdf"
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.get("/lookup/staff")
def staff_lookup():
    """Return LP + CLI rosters for UI autocomplete."""
    if not MAIL_STAFF:
        load_mail_staff()
    if not CLI_STAFF:
        load_cli_staff()
    return {"lp": MAIL_STAFF, "cli": CLI_STAFF}
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


def _restriction_limits_for_rows(rows: pl.DataFrame, restrictions: list[dict[str, Any]], coach_type: str | None = None) -> list[float | None]:
    """
    Apply permanent speed restrictions (PSR) from all_section_psr.csv.
    Coach type adjustments are handled in MPS, not here.
    """
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


def _apply_section_sequence(lat_vals: list[float | None], lon_vals: list[float | None], sections: list[str], direction: str | None, coach_type: str | None = None) -> list[float | None]:
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

        # Adjust MPS based on coach type for specific sections
        # IGP-MMR, MMR-IGP, IGP-JL, JL-IGP: 130 km/h (LHB) → 110 km/h (ICF)
        if coach_type == "ICF" and limit == 130.0:
            if section in ["IGP-MMR", "MMR-IGP", "IGP-JL", "JL-IGP"]:
                limit = 110.0

        # CHI-RN, RN-CHI, ROHA-CHI, CHI-ROHA: 120 km/h (LHB) → 110 km/h (ICF)
        if coach_type == "ICF" and limit == 120.0:
            if section in ["CHI-RN", "RN-CHI", "ROHA-CHI", "CHI-ROHA"]:
                limit = 110.0

        for idx in range(start_idx, end_idx + 1):
            result[idx] = limit
    return result


def _section_limits_for_rows(rows: pl.DataFrame, route_key: str | None, direction: str | None, coach_type: str | None = None) -> list[float | None]:
    sequences = _sections_for_route(route_key, direction, rows)
    if not sequences or "_LAT_FLOAT" not in rows.columns or "_LON_FLOAT" not in rows.columns:
        return [None] * rows.height
    lat_vals = rows["_LAT_FLOAT"].to_list()
    lon_vals = rows["_LON_FLOAT"].to_list()
    best: list[float | None] = [None] * len(lat_vals)
    best_length = -1
    for seq in sequences:
        seq_limits = _apply_section_sequence(lat_vals, lon_vals, seq, direction, coach_type)
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


def _braking_profile(df: pl.DataFrame, offsets: list[int]) -> list[dict[str, Any]]:
    if df.is_empty():
        return []
    speed_col = _find_speed_column(df.columns)
    dist_col = _find_distance_column(df.columns)
    if not speed_col or not dist_col:
        return []
    speeds = (
        df[speed_col]
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .to_list()
    )
    dist_steps = (
        df[dist_col]
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .to_list()
    )
    dist_steps = [max(0.0, x or 0.0) for x in dist_steps]
    cumulative: list[float] = []
    running = 0.0
    for step in dist_steps:
        running += step
        cumulative.append(running)

    station_col = _first_matching_column(df.columns, "STATION", "CODE") or _first_matching_column(df.columns, "STATION")
    if station_col:
        stations = [str(val).strip() if val is not None else None for val in df[station_col].to_list()]
    else:
        stations = [None] * len(speeds)
    time_col = _find_time_column(df.columns)
    times = df[time_col].to_list() if time_col else [None] * len(speeds)
    times = [_stringify_time(t) for t in times]

    first_move_idx = next((i for i, sp in enumerate(speeds) if sp and sp > 0.5), None)
    if first_move_idx is None:
        return []

    halts: list[int] = []
    last_halt_distance: float | None = None
    for idx in range(first_move_idx + 1, len(speeds)):
        prev_speed = speeds[idx - 1]
        current_speed = speeds[idx]
        if current_speed <= 0.5 and prev_speed > 0.5:
            halt_distance = cumulative[idx]
            if last_halt_distance is not None and (halt_distance - last_halt_distance) < 200.0:
                continue
            halts.append(idx)
            last_halt_distance = halt_distance

    results: list[dict[str, Any]] = []
    for seq, halt_idx in enumerate(halts, start=1):
        halt_dist = cumulative[halt_idx]
        halt_station = stations[halt_idx]
        display_station = str(halt_station).strip() if halt_station else f"{halt_dist:.0f} m"
        dist_to_halt: list[float] = [0.0] * (halt_idx + 1)
        running_back = 0.0
        for idx in range(halt_idx - 1, -1, -1):
            step = dist_steps[idx + 1] if idx + 1 < len(dist_steps) else 0.0
            running_back += step
            dist_to_halt[idx] = running_back
        halt_entry = {
            "sequence": seq,
            "index": halt_idx,
            "station": display_station,
            "logging_time": times[halt_idx],
            "distance_m": halt_dist,
            "speeds": {},
        }
        for offset in offsets:
            target = float(offset)
            chosen_idx = halt_idx
            probe = halt_idx
            while probe >= 0 and dist_to_halt[probe] <= target:
                chosen_idx = probe
                probe -= 1
            if chosen_idx < 0:
                chosen_idx = 0
            diff = abs(target - dist_to_halt[chosen_idx])
            halt_entry["speeds"][str(offset)] = {
                "speed": speeds[chosen_idx],
                "time": times[chosen_idx],
                "station": stations[chosen_idx],
                "delta_m": diff,
            }
        results.append(halt_entry)
    return results


def _braking_profile_full_curve(df: pl.DataFrame, offsets: list[int]) -> list[dict[str, Any]]:
    """
    Extract braking data with full curve points for smooth PDF charts.

    Filtering rules:
    - Minimum 200m spacing between halts
    - Must have >= 1000m approach data

    Returns unified structure with:
    - Discrete offsets (for PDF table)
    - Full curve data (for smooth PDF charts)
    """
    if df.is_empty():
        return []
    speed_col = _find_speed_column(df.columns)
    dist_col = _find_distance_column(df.columns)
    if not speed_col or not dist_col:
        return []

    speeds = (
        df[speed_col]
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .to_list()
    )
    dist_steps = (
        df[dist_col]
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .to_list()
    )
    dist_steps = [max(0.0, x or 0.0) for x in dist_steps]
    cumulative: list[float] = []
    running = 0.0
    for step in dist_steps:
        running += step
        cumulative.append(running)

    station_col = _first_matching_column(df.columns, "STATION", "CODE") or _first_matching_column(df.columns, "STATION")
    if station_col:
        stations = [str(val).strip() if val is not None else None for val in df[station_col].to_list()]
    else:
        stations = [None] * len(speeds)
    time_col = _find_time_column(df.columns)
    times = df[time_col].to_list() if time_col else [None] * len(speeds)
    times = [_stringify_time(t) for t in times]

    first_move_idx = next((i for i, sp in enumerate(speeds) if sp and sp > 0.5), None)
    if first_move_idx is None:
        return []

    # Find all halts with 200m spacing
    halts: list[int] = []
    last_halt_distance: float | None = None
    for idx in range(first_move_idx + 1, len(speeds)):
        prev_speed = speeds[idx - 1]
        current_speed = speeds[idx]
        if current_speed <= 0.5 and prev_speed > 0.5:
            halt_distance = cumulative[idx]
            if last_halt_distance is not None and (halt_distance - last_halt_distance) < 200.0:
                continue
            halts.append(idx)
            last_halt_distance = halt_distance

    results: list[dict[str, Any]] = []
    for seq, halt_idx in enumerate(halts, start=1):
        halt_dist = cumulative[halt_idx]
        halt_station = stations[halt_idx]
        display_station = str(halt_station).strip() if halt_station else f"{halt_dist:.0f} m"

        # Calculate distance to halt for all points
        dist_to_halt: list[float] = [0.0] * (halt_idx + 1)
        running_back = 0.0
        for idx in range(halt_idx - 1, -1, -1):
            step = dist_steps[idx + 1] if idx + 1 < len(dist_steps) else 0.0
            running_back += step
            dist_to_halt[idx] = running_back

        # Check if we have at least 1000m of approach data
        max_approach = dist_to_halt[0] if dist_to_halt else 0.0
        if max_approach < 1000.0:
            # Skip this halt - insufficient approach data
            continue

        # Extract discrete offsets for table
        speeds_dict = {}
        for offset in offsets:
            target = float(offset)
            chosen_idx = halt_idx
            probe = halt_idx
            while probe >= 0 and dist_to_halt[probe] <= target:
                chosen_idx = probe
                probe -= 1
            if chosen_idx < 0:
                chosen_idx = 0
            diff = abs(target - dist_to_halt[chosen_idx])
            speeds_dict[str(offset)] = {
                "speed": speeds[chosen_idx],
                "time": times[chosen_idx],
                "station": stations[chosen_idx],
                "delta_m": diff,
            }

        # Extract ALL points within 1000m for smooth curve
        curve_distances: list[float] = []
        curve_speeds: list[float | None] = []

        for idx in range(halt_idx + 1):
            d = dist_to_halt[idx]
            if d <= 1000.0:
                curve_distances.append(d)
                curve_speeds.append(speeds[idx])

        # Reverse so distance goes from 1000m → 0m (not 0m → 1000m)
        curve_distances.reverse()
        curve_speeds.reverse()

        halt_entry = {
            "sequence": seq,
            "index": halt_idx,
            "station": display_station,
            "logging_time": times[halt_idx],
            "distance_m": halt_dist,
            "speeds": speeds_dict,  # For table
            "curve_data": {  # For smooth chart
                "distances": curve_distances,
                "speeds": curve_speeds
            }
        }
        results.append(halt_entry)

    return results


def _detect_brake_event(
    speeds: list[float],
    mask: list[bool],
    min_speed: float,
    max_speed: float,
    drop_percent: float,
) -> dict[str, Any]:
    n = len(speeds)
    for idx in range(n - 1):
        if not mask[idx]:
            continue
        start_speed = speeds[idx]
        next_speed = speeds[idx + 1]
        if not (min_speed < start_speed < max_speed):
            continue
        if next_speed >= start_speed - BRAKE_EVENT_TOLERANCE:
            continue
        min_val = next_speed
        end_idx = idx + 1
        probe = idx + 1
        while probe < n and mask[probe]:
            current = speeds[probe]
            if current < min_val:
                min_val = current
                end_idx = probe
            ahead = probe + 1
            if ahead >= n or not mask[ahead]:
                break
            if speeds[ahead] > current + BRAKE_EVENT_TOLERANCE:
                break
            probe += 1
        drop = ((start_speed - min_val) / start_speed) * 100 if start_speed else 0.0
        if drop >= drop_percent:
            return {
                "status": "PASS",
                "start_index": idx,
                "end_index": end_idx,
                "start_speed": start_speed,
                "end_speed": min_val,
                "drop_percent": drop,
            }
    return {
        "status": "FAIL",
        "start_index": -1,
        "end_index": -1,
        "start_speed": None,
        "end_speed": None,
        "drop_percent": None,
    }


def _brake_tests(df: pl.DataFrame, start_station: str | None, direction: str | None) -> dict[str, Any]:
    if df.is_empty() or not start_station:
        return {
            "feel": {"status": "NOT RUN"},
            "power": {"status": "NOT RUN"},
        }
    speed_col = _find_speed_column(df.columns)
    if not speed_col:
        return {
            "feel": {"status": "NOT RUN"},
            "power": {"status": "NOT RUN"},
        }
    speeds = (
        df[speed_col]
        .cast(pl.Float64, strict=False)
        .fill_null(0.0)
        .to_list()
    )
    station_col = _first_matching_column(df.columns, "STATION", "CODE") or _first_matching_column(df.columns, "STATION")
    stations = df[station_col].cast(pl.Utf8).to_list() if station_col else [None] * len(speeds)
    times_col = _find_time_column(df.columns)
    times = df[times_col].to_list() if times_col else [None] * len(speeds)
    mask = [True] * len(speeds)

    def summarize(result: dict[str, Any]) -> dict[str, Any]:
        start_idx = result.get("start_index", -1)
        end_idx = result.get("end_index", -1)
        payload = {
            "status": result.get("status", "FAIL"),
            "start_speed": result.get("start_speed"),
            "end_speed": result.get("end_speed"),
            "drop_percent": result.get("drop_percent"),
        }
        if start_idx >= 0 and start_idx < len(times):
            payload["start_time"] = _stringify_time(times[start_idx])
        if end_idx >= 0 and end_idx < len(times):
            payload["end_time"] = _stringify_time(times[end_idx])
        return payload

    start_norm = _norm_literal(start_station)
    feel_min, feel_max = (7.0, 16.0) if start_norm == "PUNE" else (10.0, 16.0)

    # Brake Power Test criteria based on start station
    if start_norm == "MMR":
        power_min, power_max = 45.0, 100.0
    elif start_norm == "IGP":
        power_min, power_max = 35.0, 60.0
    else:
        power_min, power_max = 45.0, 70.0

    feel_raw = _detect_brake_event(speeds, mask, feel_min, feel_max, BRAKE_REQUIRED_DROP)
    power_raw = _detect_brake_event(speeds, mask, power_min, power_max, BRAKE_REQUIRED_DROP)
    return {
        "feel": summarize(feel_raw),
        "power": summarize(power_raw),
    }


# ------------------------------
# Run Server
# ------------------------------
if __name__ == "__main__":
    import uvicorn
    print("\n[SERVER] Starting uvicorn on http://localhost:8765")
    print("[SERVER] Press Ctrl+C to stop")
    print("[SERVER] Open browser: http://localhost:8765/ui/\n")
    uvicorn.run(app, host="0.0.0.0", port=8765)
