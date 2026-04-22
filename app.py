from fastapi import FastAPI, UploadFile, Body, Query, Header
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi import Request, HTTPException
from auth import get_current_user
from db_config import get_db_connection
import mysql.connector
import polars as pl
import pandas as pd
import io, time, re, math, sys, threading, uuid
from typing import Dict, Any, List, Tuple
from datetime import datetime
from pathlib import Path
import numpy as np
import os

__version__ = "1.2.0"

try:  # Optional heavy deps for PDF export
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from reportlab.lib import colors  # type: ignore
    from reportlab.lib.pagesizes import A4  # type: ignore
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle  # type: ignore
    from reportlab.lib.units import inch  # type: ignore
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage, PageBreak  # type: ignore
    from reportlab.graphics.shapes import Drawing, Line  # type: ignore
except ImportError:
    plt = None  # type: ignore[assignment]
    SimpleDocTemplate = None  # type: ignore[assignment]
    Paragraph = Spacer = Table = TableStyle = RLImage = PageBreak = None  # type: ignore[assignment]
    colors = None  # type: ignore[assignment]
    A4 = None  # type: ignore[assignment]
    getSampleStyleSheet = None  # type: ignore[assignment]
    inch = None  # type: ignore[assignment]
    Drawing = Line = None  # type: ignore[assignment]

# ------------------------------
# App & Static UI
# ------------------------------
# app = FastAPI()
ROOT_PATH = os.getenv("ROOT_PATH", "")
app = FastAPI(
    title="CR RTIS Analysis API",
    root_path=ROOT_PATH
)
app.mount("/ui", StaticFiles(directory="ui", html=True), name="ui")

# Mount reports folder for serving LP PDFs
REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(os.path.join(REPORTS_DIR, "lp"), exist_ok=True)
app.mount("/reports", StaticFiles(directory=REPORTS_DIR), name="reports")

@app.get("/")
def root():
    # return RedirectResponse(url="/ui/")
    return RedirectResponse(url="/spm/rtis/ui/")

# ------------------------------
# In-memory run storage (prevents cross-user clobbering)
# ------------------------------
DF: pl.DataFrame | None = None  # legacy single-DF placeholder

# Runs are keyed by run_id to allow multiple simultaneous uploads even if users share credentials.
# Each run stores owning user_id for access control. TTL avoids unbounded memory growth.
# DataFrames are stored as temp files to reduce memory usage.
RUNS: Dict[str, Dict[str, Any]] = {}
RUNS_LOCK = threading.Lock()
RUN_TTL_SECONDS = 60 * 60 * 1  # 1 hour (reduced from 6 to limit memory)
MAX_RUNS = 20  # Maximum concurrent runs to prevent unbounded growth

# Temp directory for storing DataFrames (disk instead of RAM)
RUNS_TEMP_DIR = "/tmp/rtis_runs"
os.makedirs(RUNS_TEMP_DIR, exist_ok=True)

# Idempotency cache per user (keyed by Idempotency-Key header)
IDEMPOTENCY_CACHE: Dict[Tuple[int, str], Dict[str, Any]] = {}


def _cleanup_orphan_files() -> None:
    """Remove temp files older than TTL (handles crash scenarios)."""
    now = time.time()
    try:
        for filename in os.listdir(RUNS_TEMP_DIR):
            file_path = os.path.join(RUNS_TEMP_DIR, filename)
            if os.path.isfile(file_path):
                file_age = now - os.path.getmtime(file_path)
                if file_age > RUN_TTL_SECONDS:
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass
    except Exception:
        pass


def _purge_expired_runs() -> None:
    """Purge expired runs from memory AND delete their temp files."""
    now = time.time()
    with RUNS_LOCK:
        expired = [rid for rid, run in RUNS.items() if now - run.get("uploaded_at", 0) > RUN_TTL_SECONDS]
        for rid in expired:
            run = RUNS.pop(rid, None)
            # Delete temp file if exists
            if run:
                file_path = run.get("file_path")
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        expired_keys = [
            key for key, meta in IDEMPOTENCY_CACHE.items()
            if now - meta.get("uploaded_at", 0) > RUN_TTL_SECONDS
        ]
        for key in expired_keys:
            IDEMPOTENCY_CACHE.pop(key, None)

    # Also clean orphan files
    _cleanup_orphan_files()


def _set_user_run(run_id: str, user_id: int, df: pl.DataFrame | None, files: List[dict[str, Any]]) -> None:
    """Store DF to temp file instead of memory."""
    _purge_expired_runs()

    file_path = None
    if df is not None:
        file_path = os.path.join(RUNS_TEMP_DIR, f"{run_id}.parquet")
        df.write_parquet(file_path)

    with RUNS_LOCK:
        # Evict oldest if at max capacity
        if len(RUNS) >= MAX_RUNS:
            oldest_rid = min(RUNS.items(), key=lambda x: x[1].get("uploaded_at", 0))[0]
            old_run = RUNS.pop(oldest_rid, None)
            if old_run:
                old_path = old_run.get("file_path")
                if old_path and os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass

        RUNS[run_id] = {
            "file_path": file_path,  # Store path, NOT the DataFrame
            "uploaded_at": time.time(),
            "files": files,
            "user_id": user_id,
            "run_id": run_id,
        }


def _get_user_run(request: Request, run_id: str | None) -> Tuple[dict, pl.DataFrame]:
    """Return (user, df) for the current request, loading DF from temp file."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not run_id:
        raise HTTPException(status_code=400, detail="run_id is required; call /load_csv first")

    _purge_expired_runs()
    with RUNS_LOCK:
        run = RUNS.get(run_id)

    if not run:
        raise HTTPException(status_code=400, detail="no data loaded for this run_id")

    # Enforce ownership
    owner_id = run.get("user_id")
    if owner_id is not None and owner_id != user.get("id"):
        raise HTTPException(status_code=403, detail="run_id does not belong to this user")

    file_path = run.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=400, detail="no data loaded for this run_id")

    # Load DataFrame from temp file on demand
    df = pl.read_parquet(file_path)

    return user, df


# Clean orphan files on startup
_cleanup_orphan_files()

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
BRAKE_OFFSETS = [1000, 300, 200, 100, 50, 20]
# Offsets for violations analysis (includes ghat-specific distances)
VIOLATIONS_OFFSETS = BRAKE_OFFSETS + [900, 800]
# Ghat distance thresholds: default 900m, specific stations use 800m
GHAT_DEFAULT_DISTANCE = 900
GHAT_STATION_DISTANCES = {"KAD": 800, "NNCN": 800}  # T5 stations
BRAKE_EVENT_TOLERANCE = 0.3
BRAKE_REQUIRED_DROP = 45.0
BRAKE_CHART_STEPS = sorted(set(BRAKE_OFFSETS + [0]), reverse=True)

# Section breakdowns for sectional charts
# Key = "FROM-TO", Value = list of stations for section boundaries
SECTION_BREAKDOWN = {
    "JL-CSMT": ["JL", "MMR", "IGP", "KSRA", "KYN", "DR", "CSMT"],
    "MMR-CSMT": ["MMR", "IGP", "KSRA", "KYN", "DR", "CSMT"],
    "IGP-CSMT": ["IGP", "KSRA", "KYN", "DR", "CSMT"],
    "PUNE-CSMT": ["PUNE", "LNL", "KJT", "KYN", "DR", "CSMT"],       # Via Kalyan
    "PUNE-CSMT-PNVL": ["PUNE", "LNL", "KJT", "PNVL", "DR", "CSMT"], # Via PNVL
    "RN-CSMT": ["RN", "CHI", "ROHA", "PNVL", "CSMT"],
    "ROHA-CSMT": ["ROHA", "PNVL", "CSMT"],
    "RN-BSR": ["RN", "ROHA", "PNVL", "BSR"],
    "ROHA-BSR": ["ROHA", "PNVL", "BSR"],
    # LTT routes
    "LTT-PUNE": ["LTT", "KYN", "KJT", "LNL", "PUNE"],         # Via Kalyan
    "LTT-PUNE-PNVL": ["LTT", "DIVA", "PNVL", "KJT", "LNL", "PUNE"], # Via PNVL
    "PUNE-LTT": ["PUNE", "LNL", "KJT", "KYN", "LTT"],         # Via Kalyan
    "PUNE-LTT-PNVL": ["PUNE", "LNL", "KJT", "PNVL", "DIVA", "LTT"], # Via PNVL
    "LTT-JL": ["LTT", "KYN", "KSRA", "IGP", "JL"],
    "JL-LTT": ["JL", "IGP", "KSRA", "KYN", "LTT"],
    "LTT-MMR": ["LTT", "KYN", "KSRA", "IGP", "MMR"],
    "MMR-LTT": ["MMR", "IGP", "KSRA", "KYN", "LTT"],
    "LTT-IGP": ["LTT", "KYN", "KSRA", "IGP"],
    "LTT-RN": ["LTT", "DIVA", "PNVL", "ROHA", "CHI", "RN"],
    "RN-LTT": ["RN", "CHI", "ROHA", "PNVL", "DIVA", "LTT"],
    # MEMU routes (BSR-DIVA)
    "DIVA-BSR": ["DIVA", "KOPR", "BSR"],
    "BSR-DIVA": ["BSR", "KOPR", "DIVA"],
}

MAIL_STAFF: list[dict[str, Any]] = []
CLI_STAFF: list[dict[str, Any]] = []

MPS_CONFIG: dict[str, float] = {
    # DN (CSMT → PUNE)
    "DR-KYN": 105.0,
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
    "KYN-DR": 105.0,
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
    "PNVL-ROHA": 110.0,
    "ROHA-CHI": 120.0,
    "CHI-RN": 120.0,
    
    # UP (RN → CSMT)
    "RN-CHI": 120.0,
    "CHI-ROHA": 120.0,
    
    "ROHA-PNVL": 110.0,
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
    # DIVA-BSR (MEMU sections)
    "DIVA-KOPR": 105.0,
    "KOPR-DIVA": 105.0,
    "KOPR-BSR": 110.0,
    "BSR-KOPR": 110.0,
}

ROUTE_SECTION_MAP: dict[tuple[str, str], list[list[str]]] = {
    ("CSMT-PUNE", "DN"): [
        ["CSMT-DR", "DR-KYN", "KYN-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
        ["CSMT-DIVA", "DIVA-PNVL", "PNVL-KJT", "KJT-PDI", "PDI-LNL", "LNL-PUNE"],
    ],
    ("PUNE-CSMT", "UP"): [
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-KYN", "KYN-DR", "DR-CSMT"],
        ["PUNE-LNL", "LNL-PDI", "PDI-KJT", "KJT-PNVL", "PNVL-DIVA", "DIVA-CSMT"],
    ],
    ("CSMT-JL", "DN"): [["CSMT-DR", "DR-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-JL"]],
    ("JL-CSMT", "UP"): [["JL-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-DR", "DR-CSMT"]],
    ("CSMT-IGP", "DN"): [["CSMT-DR", "DR-KYN", "KYN-KSRA", "KSRA-IGP"]],
    ("IGP-CSMT", "UP"): [["IGP-KSRA", "KSRA-KYN", "KYN-DR", "DR-CSMT"]],
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
    ("LTT-IGP", "DN"): [["LTT-KYN", "KYN-KSRA", "KSRA-IGP"]],
    ("JL-LTT", "UP"): [["JL-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-LTT"]],
    ("LTT-RN", "DN"): [["LTT-DIVA", "DIVA-PNVL", "PNVL-ROHA", "ROHA-CHI", "CHI-RN"]],
    ("RN-LTT", "UP"): [["RN-CHI", "CHI-ROHA", "ROHA-PNVL", "PNVL-DIVA", "DIVA-LTT"]],
    ("CSMT-MMR", "DN"): [["CSMT-DR", "DR-KYN", "KYN-KSRA", "KSRA-IGP", "IGP-MMR"]],
    ("MMR-CSMT", "UP"): [["MMR-IGP", "IGP-KSRA", "KSRA-KYN", "KYN-DR", "DR-CSMT"]],
    # MEMU routes (BSR-DIVA)
    ("DIVA-BSR", "DN"): [["DIVA-KOPR", "KOPR-BSR"]],
    ("BSR-DIVA", "UP"): [["BSR-KOPR", "KOPR-DIVA"]],
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

@app.get("/api/current-user")
async def api_current_user(request: Request):
    user = request.state.user  # set by your auth middleware/dependency
    return {"ok": True, "user": user}


@app.get("/rtis")
def rtis_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="http://localhost:3000/")  # or your bbtro login/landing
    return RedirectResponse(url="/spm/rtis/ui/")
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
async def load_csv(
    request: Request,
    files: List[UploadFile],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Upload and load analysis CSV (large run file). Supports multiple files for overnight journeys."""
    global DF

    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    # Generate run_id up front so it can be reused for idempotent retries
    run_id = f"run_{uuid.uuid4().hex[:12]}"

    # Fast path: return cached response if idempotency key matches an existing upload for this user
    cache_key = (user.get("id"), idempotency_key) if idempotency_key else None
    if cache_key:
        _purge_expired_runs()
        with RUNS_LOCK:
            cached = IDEMPOTENCY_CACHE.get(cache_key)
        if cached:
            return cached["response"]

    if not files or len(files) == 0:
        _set_user_run(run_id, user.get("id"), None, [])
        DF = None
        return JSONResponse({"error": "no files uploaded"}, status_code=400)

    dataframes = []
    file_info = []

    # Read each file and extract first timestamp for sorting
    for file in files:
        data = await file.read()
        if not data:
            continue

        buf = io.BytesIO(data)
        df = pl.read_csv(
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

        # Extract first timestamp for sorting
        time_col = None
        for col in df.columns:
            col_lower = col.lower()
            if "time" in col_lower and ("gps" in col_lower or "logging" in col_lower or col_lower == "time"):
                time_col = col
                break

        first_timestamp = None
        if time_col and df.height > 0:
            first_row = df[time_col][0]
            if first_row:
                first_timestamp = str(first_row)

        file_info.append({
            "filename": file.filename,
            "df": df,
            "first_timestamp": first_timestamp,
            "rows": df.height
        })

    if not file_info:
        _set_user_run(run_id, user.get("id"), None, [])
        DF = None
        return JSONResponse({"error": "no valid data in uploaded files"}, status_code=400)

    # Sort files by timestamp (earliest first)
    file_info.sort(key=lambda x: x["first_timestamp"] if x["first_timestamp"] else "")

    # Check column compatibility if multiple files
    if len(file_info) > 1:
        first_cols = set(file_info[0]["df"].columns)
        for i, info in enumerate(file_info[1:], 1):
            current_cols = set(info["df"].columns)
            if first_cols != current_cols:
                missing = first_cols - current_cols
                extra = current_cols - first_cols
                error_msg = f"Column mismatch between files. "
                if missing:
                    error_msg += f"File '{info['filename']}' missing columns: {', '.join(missing)}. "
                if extra:
                    error_msg += f"File '{info['filename']}' has extra columns: {', '.join(extra)}."
                return JSONResponse({"error": error_msg}, status_code=400)

    # Concatenate DataFrames
    if len(file_info) == 1:
        df = file_info[0]["df"]
    else:
        df = pl.concat([info["df"] for info in file_info], how="vertical")

    # Apply standard processing
    df = _repair_shifted_rows(df)
    drop_cols = [
        "BE Version",
        "GUI Version",
        "ODU Version",
        "DB Circle Count",
        "DB Polygon Count",
    ]
    keep = [c for c in df.columns if c not in drop_cols]
    df = df.select(keep)

    # Store per-user run to avoid cross-user clobbering
    _set_user_run(run_id, user.get("id"), df, file_info)
    DF = df  # legacy fallback; remove after full refactor

    response = {
        "run_id": run_id,
        "rows": df.height,
        "cols": df.width,
        "columns": df.columns,
        "files_merged": len(file_info),
        "file_details": [{"filename": info["filename"], "rows": info["rows"]} for info in file_info]
    }
    if cache_key:
        with RUNS_LOCK:
            IDEMPOTENCY_CACHE[cache_key] = {"response": response, "uploaded_at": time.time()}

    return response


def _resolve_run_id(run_id_param: str | None, body: Dict[str, Any] | None = None) -> str:
    rid = run_id_param or (body or {}).get("run_id")
    if not rid:
        raise HTTPException(status_code=400, detail="run_id is required; call /load_csv first")
    return str(rid)


@app.get("/preview")
def preview(request: Request, n: int = 20, run_id: str | None = Query(None)):
    try:
        _, df = _get_user_run(request, _resolve_run_id(run_id))
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return {"data": df.head(n).to_dicts()}


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
    to_station_norm = to_station.strip().upper() if to_station else None
    MIN_START_SPEED = 0.5  # ignore GPS noise spikes when building departure candidates

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
        if sp < 1 and sp_next > MIN_START_SPEED:
            inside, _ = _within_geofence(lats[i], lons[i], geos)
            if inside:
                next_idx = idxs[i] + 1 if i + 1 < len(idxs) else idxs[i]
                candidates.append(int(next_idx))

    if not candidates:
        return None

    # Hard cap to avoid O(n^2) behavior on noisy yard data (keeps latest movements)
    MAX_CANDIDATES = 300
    if len(candidates) > MAX_CANDIDATES:
        original_len = len(candidates)
        candidates = candidates[-MAX_CANDIDATES:]
        print(f"[DEBUG] Candidate cap applied, keeping last {MAX_CANDIDATES} of {original_len}")

    print(f"[DEBUG] Found {len(candidates)} raw candidates at {station_code}")

    # Sustained departure filter: reject candidates that are just shunting movements
    # A real departure maintains speed >0 and eventually exits the origin station
    sustained_candidates = []
    for candidate_idx in candidates:
        # Find the index in our arrays (candidate_idx is df row index)
        arr_idx = None
        for idx, row_idx in enumerate(idxs):
            if row_idx == candidate_idx:
                arr_idx = idx
                break
        if arr_idx is None:
            sustained_candidates.append(candidate_idx)
            continue

        # Check next 120 seconds of data for sustained departure
        window_size = min(120, len(idxs) - arr_idx)
        if window_size < 30:
            sustained_candidates.append(candidate_idx)
            continue

        # Look for: (1) sustained speed > 0, (2) exit from origin geofence
        # Also check that speed actually starts increasing from candidate point (not GPS noise)
        speed_returned_to_zero = False
        exited_geofence = False
        max_speed_in_window = 0

        # Get the initial speed at candidate point - must be real movement, not GPS noise
        initial_speed = speeds[arr_idx] if arr_idx < len(speeds) and speeds[arr_idx] is not None else 0
        # Reject candidates with very low initial speed (GPS noise)
        if initial_speed < 0.5:
            print(f"[DEBUG] Candidate {candidate_idx}: sustained departure REJECT (GPS_noise, initial_speed={initial_speed})")
            continue

        for j in range(arr_idx, arr_idx + window_size):
            sp = speeds[j]

            if sp is not None:
                max_speed_in_window = max(max_speed_in_window, sp)

                # If speed returns to 0 after going above 3, it's shunting
                if sp < 0.5 and max_speed_in_window > 3:
                    speed_returned_to_zero = True
                    break

            # Check if exited the origin station geofence
            lat, lon = lats[j], lons[j]
            if lat is not None and lon is not None:
                inside, _ = _within_geofence(lat, lon, geos)
                if not inside:
                    exited_geofence = True
                    break

        # Accept if train exited geofence OR maintained speed (didn't return to 0)
        if exited_geofence:
            sustained_candidates.append(candidate_idx)
            print(f"[DEBUG] Candidate {candidate_idx}: sustained departure PASS (exited geofence, initial_speed={initial_speed})")
        elif not speed_returned_to_zero and max_speed_in_window >= 5:
            sustained_candidates.append(candidate_idx)
            print(f"[DEBUG] Candidate {candidate_idx}: sustained departure PASS (maintained speed, max={max_speed_in_window})")
        else:
            print(f"[DEBUG] Candidate {candidate_idx}: sustained departure REJECT (shunting, max_speed={max_speed_in_window}, returned_to_zero={speed_returned_to_zero})")

    candidates = sustained_candidates if sustained_candidates else candidates
    print(f"[DEBUG] After sustained departure filter: {len(candidates)} candidates at {station_code}, expected_seq={expected_sequence[:5] if expected_sequence else None}, station_data_good={station_data_good}")

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

    # Precompute destination presence once (avoid per-candidate frame scans)
    dest_last_idx = None
    if to_station_norm and station_col:
        dest_last_idx = _find_station_row_idx(df, station_col, to_station_norm, first=False)
    dest_ge_end_idx = None
    if to_station_norm:
        dest_ge_end_idx = _find_geofence_end_idx(df, to_station_norm, direction, start_after=None)

    # Helper: ensure destination exists after the candidate (prevents picking reverse-leg starts)
    def _destination_exists_after(candidate_idx: int) -> bool:
        if not to_station_norm:
            return True
        if dest_ge_end_idx is not None and dest_ge_end_idx > candidate_idx:
            return True
        if dest_last_idx is not None and dest_last_idx > candidate_idx:
            return True
        return False

    # Validate each candidate - collect all valid ones, then return the LAST
    # (for terminal-starting trains like DR, the actual departure is usually the last one
    # as earlier candidates may be from different journeys in the same CSV)
    valid_candidates = []
    dest_candidates = []

    for candidate_idx in candidates:
        if not _destination_exists_after(candidate_idx):
            print(f"[DEBUG] Candidate {candidate_idx}: destination {to_station_norm} not found after candidate, skipping")
            continue
        dest_candidates.append(candidate_idx)
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
                valid_candidates.append(candidate_idx)
        # Fallback: GPS trajectory validation
        elif to_station and lat_col and lon_col:
            is_valid = _validate_gps_trajectory(df, candidate_idx, station_code, to_station, lat_col, lon_col)
            print(f"[DEBUG] Candidate {candidate_idx}: gps_trajectory_valid={is_valid}")
            if is_valid:
                valid_candidates.append(candidate_idx)
        # No validation possible, add to valid list
        elif not expected_sequence and not to_station:
            print(f"[DEBUG] Candidate {candidate_idx}: no validation possible, accepting")
            valid_candidates.append(candidate_idx)

    # Return the LAST valid candidate (for terminal-starting trains, this is the actual departure)
    if valid_candidates:
        print(f"[DEBUG] Found {len(valid_candidates)} valid candidates, returning last: {valid_candidates[-1]}")
        return valid_candidates[-1]

    if dest_candidates:
        print(f"[DEBUG] No validated candidates, but {len(dest_candidates)} have destination ahead; returning last: {dest_candidates[-1]}")
        return dest_candidates[-1]

    # If no candidates passed validation, return last from all candidates
    print(f"[DEBUG] No candidates passed validation, returning last: {candidates[-1] if candidates else None}")
    return candidates[-1] if candidates else None


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
def analyze(request: Request, criteria: Dict[str, Any] = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    filtered = apply_criteria(df, criteria)
    return {"rows": filtered.height, "sample": filtered.head(50).to_dicts()}


# ------------------------------
# Excel Export
# ------------------------------
@app.post("/export")
def export(request: Request, criteria: dict = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    out = apply_criteria(df, criteria)
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
def chart_data(request: Request, criteria: dict = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    out = apply_criteria(df, criteria)
    try:
        payload = _build_chart_payload(out, criteria)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return payload

@app.post("/braking_profile")
def braking_profile(request: Request, criteria: dict = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    filtered = apply_criteria(df, criteria)
    analysis_offsets = list(BRAKE_OFFSETS)
    extras: list[int] = []
    if 500 not in analysis_offsets:
        extras.append(500)
    if 250 not in analysis_offsets:
        extras.append(250)
    if extras:
        analysis_offsets = sorted(set(analysis_offsets + extras), reverse=True)
    halts = _braking_profile(filtered, analysis_offsets)
    decel_table = _halt_deceleration_metrics(
        halts,
        start_station=criteria.get("from_station_equals"),
        end_station=criteria.get("to_station_equals"),
    )
    extras_to_strip: list[str] = []
    if 500 not in BRAKE_OFFSETS:
        extras_to_strip.append("500")
    if 250 not in BRAKE_OFFSETS:
        extras_to_strip.append("250")
    if extras_to_strip:
        for entry in halts:
            entry_speeds = entry.get("speeds", {})
            for key in extras_to_strip:
                entry_speeds.pop(key, None)

    # Compute zone-based 1000m threshold for each halt (sync with PDF export logic)
    from_station = (criteria.get("from_station_equals") or "").strip().upper()
    to_station = (criteria.get("to_station_equals") or "").strip().upper()
    direction = (criteria.get("direction_equals") or "").strip().upper()
    boundary_type = ROUTE_BOUNDARY_MAP.get(to_station) or ROUTE_BOUNDARY_MAP.get(from_station)
    boundary_stations = ZONE_BOUNDARY_STATIONS.get(boundary_type, []) if boundary_type else []
    boundary_row_idx = _find_boundary_row_index(filtered, boundary_stations) if boundary_stations else None
    boundary_halt_idx = _find_boundary_halt_index(halts, boundary_row_idx)

    # Compute which halts are in ghat sections (steep gradient, UP direction only)
    ghat_halt_indices = _compute_ghat_section_halts(filtered, halts, direction)

    train_number = (criteria.get("train_number") or "").strip()
    for idx, halt in enumerate(halts):
        in_ghat = idx in ghat_halt_indices
        halt["threshold_1000m"] = _get_1000m_threshold(
            idx, boundary_halt_idx, from_station, to_station, direction, train_number, in_ghat
        )
        halt["threshold_400m"] = _get_400m_threshold(
            idx, boundary_halt_idx, from_station, direction, train_number
        )
        halt["in_ghat_section"] = in_ghat  # Include for UI debugging/display

    return {
        "offsets": BRAKE_OFFSETS,
        "halts": halts,
        "deceleration": decel_table,
    }


@app.post("/brake_tests")
def brake_tests(request: Request, criteria: dict = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    filtered = apply_criteria(df, criteria)
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


def _get_section_breakdown(from_station: str, to_station: str, dataset: pl.DataFrame) -> list[str] | None:
    """
    Get section breakdown for a route, handling LTT/DR replacement, auto-reverse,
    intelligent variant selection (via KYN vs via PNVL), and partial routes.
    Returns list of station codes for section boundaries, or None if not found.
    """
    from_norm = _norm_literal(from_station)
    to_norm = _norm_literal(to_station)

    # Find all matching breakdowns (including variants and partial routes)
    candidates = []

    # Try exact match and variants
    route_key = f"{from_norm}-{to_norm}"
    for key, breakdown in SECTION_BREAKDOWN.items():
        if key == route_key or key.startswith(f"{route_key}-"):
            candidates.append((key, breakdown, False, None, None))  # (key, breakdown, is_reversed, from_idx, to_idx)

    # Try reverse route and its variants
    reverse_key = f"{to_norm}-{from_norm}"
    for key, breakdown in SECTION_BREAKDOWN.items():
        if key == reverse_key or key.startswith(f"{reverse_key}-"):
            candidates.append((key, breakdown, True, None, None))  # (key, breakdown, is_reversed, from_idx, to_idx)

    # If no exact matches, search for breakdowns that contain both stations (partial routes)
    if not candidates:
        for key, breakdown_orig in SECTION_BREAKDOWN.items():
            # Check forward direction
            if from_norm in breakdown_orig and to_norm in breakdown_orig:
                from_idx = breakdown_orig.index(from_norm)
                to_idx = breakdown_orig.index(to_norm)
                if from_idx < to_idx:
                    candidates.append((key, breakdown_orig, False, from_idx, to_idx))

            # Check reverse direction
            if from_norm in breakdown_orig and to_norm in breakdown_orig:
                from_idx = breakdown_orig.index(from_norm)
                to_idx = breakdown_orig.index(to_norm)
                if from_idx > to_idx:
                    # Reverse case: to_station appears before from_station in breakdown
                    candidates.append((key, breakdown_orig, True, to_idx, from_idx))

    if not candidates:
        return None

    # If only one candidate, use it
    if len(candidates) == 1:
        key, breakdown, is_reversed, from_idx, to_idx = candidates[0]

        # Extract partial route if indices provided
        if from_idx is not None and to_idx is not None:
            breakdown = breakdown[from_idx:to_idx + 1]  # +1 to include to_station
            print(f"[SECTION] Extracted partial route from {key}: {' → '.join(breakdown)}")

        if is_reversed:
            breakdown = list(reversed(breakdown))
        # Replace CSMT with LTT/DR if needed
        if to_norm in ["LTT", "DR"] and "CSMT" in breakdown:
            breakdown = [to_norm if s == "CSMT" else s for s in breakdown]
        elif from_norm in ["LTT", "DR"] and "CSMT" in breakdown and is_reversed:
            breakdown = [from_norm if s == "CSMT" else s for s in breakdown]
        return breakdown

    # Multiple candidates - score them based on actual station sequence
    station_col = _first_matching_column(dataset.columns, "STATION", "CODE") or \
                  _first_matching_column(dataset.columns, "STATION")
    actual_sequence = _station_sequence_from_rows(dataset, station_col)

    if not actual_sequence:
        # No station data, just return first candidate
        print(f"[SECTION] No station data available, using first breakdown candidate")
        key, breakdown, is_reversed, from_idx, to_idx = candidates[0]

        # Extract partial route if indices provided
        if from_idx is not None and to_idx is not None:
            breakdown = breakdown[from_idx:to_idx + 1]

        if is_reversed:
            breakdown = list(reversed(breakdown))
        if to_norm in ["LTT", "DR"] and "CSMT" in breakdown:
            breakdown = [to_norm if s == "CSMT" else s for s in breakdown]
        elif from_norm in ["LTT", "DR"] and "CSMT" in breakdown and is_reversed:
            breakdown = [from_norm if s == "CSMT" else s for s in breakdown]
        return breakdown

    # Score each candidate
    scored = []
    for key, breakdown_orig, is_reversed, from_idx, to_idx in candidates:
        breakdown = list(breakdown_orig)

        # Extract partial route if indices provided
        if from_idx is not None and to_idx is not None:
            breakdown = breakdown[from_idx:to_idx + 1]

        if is_reversed:
            breakdown = list(reversed(breakdown))
        # Replace CSMT with LTT/DR if needed
        if to_norm in ["LTT", "DR"] and "CSMT" in breakdown:
            breakdown = [to_norm if s == "CSMT" else s for s in breakdown]
        elif from_norm in ["LTT", "DR"] and "CSMT" in breakdown and is_reversed:
            breakdown = [from_norm if s == "CSMT" else s for s in breakdown]

        score = _score_route_match(breakdown, actual_sequence)
        partial_label = f" (partial: {' → '.join(breakdown)})" if from_idx is not None else ""
        scored.append((score, breakdown, key, partial_label))
        print(f"[SECTION] Breakdown candidate {key}{partial_label}: score={score:.1f}")

    # Sort by score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_breakdown, best_key, best_label = scored[0]

    print(f"[SECTION] Selected breakdown {best_key}{best_label} with score {best_score:.1f}")
    return best_breakdown


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

    # Get section breakdown for this route (with intelligent variant selection)
    breakdown = _get_section_breakdown(from_station, to_station, dataset)

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
        "alp_name": criteria.get("alp_name"),
        "ncli_name": criteria.get("ncli_name"),
        "ncli_alp_name": criteria.get("ncli_alp_name"),
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
    boundary_row_idx: int | None = None,
    ghat_halt_indices: set[int] | None = None,
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
        ["ALP", summary.get("alp_name") or "-"],
        ["NCLI (LP)", summary.get("ncli_name") or "-"],
        ["NCLI (ALP)", summary.get("ncli_alp_name") or "-"],
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
        # Deduplicate halts occurring at the same station within 600 meters
        # Track original index for ghat section lookup
        filtered_halts = []
        original_indices = []  # Maps filtered index -> original index in halts
        last_station = None
        last_distance = None
        for orig_idx, halt in enumerate(halts):
            station = (halt.get("station") or "").strip().upper()
            distance = float(halt.get("distance_m") or 0.0)
            if station and station == last_station and last_distance is not None:
                if abs(distance - last_distance) < 600.0:
                    continue  # Skip very close halts in same station
            filtered_halts.append(halt)
            original_indices.append(orig_idx)
            last_station = station
            last_distance = distance

        # Determine zone boundary for 1000m threshold
        from_station = summary.get("from_station")
        to_station = summary.get("to_station")
        direction = summary.get("direction")
        train_number = summary.get("train_number")

        # Find boundary halt index using the pre-computed boundary row index
        boundary_halt_idx = _find_boundary_halt_index(filtered_halts, boundary_row_idx)

        story.append(Paragraph("Braking Pattern Table", styles["Heading2"]))
        header = ["Halt"] + [f"{400 if offset == 300 else offset} m" for offset in BRAKE_OFFSETS] + ["0 m"]
        data = [header]

        # Track cells that need warning background or red text
        warning_cells = []  # List of (row, col) tuples for red background
        warn_text_cells = []  # List of (row, col) tuples for red text only
        yellow_warning_cells = []  # List of (row, col) tuples for yellow background (20m: 10-15 km/h)

        has_ghat_section = False  # Track if any halt is in ghat section for footnote
        for row_idx, halt in enumerate(filtered_halts, start=1):  # start=1 because row 0 is header
            halt_list_idx = row_idx - 1  # Convert to 0-based index for zone detection
            orig_halt_idx = original_indices[halt_list_idx]  # Original index for ghat section lookup
            in_ghat = ghat_halt_indices is not None and orig_halt_idx in ghat_halt_indices
            if in_ghat:
                has_ghat_section = True
            # Add ▲ marker for ghat section stations
            station_name = halt.get("station") or f"Halt {halt.get('sequence')}"
            if in_ghat:
                station_name = f"{station_name} ▲"
            row = [station_name]
            for col_idx, offset in enumerate(BRAKE_OFFSETS, start=1):  # start=1 because col 0 is halt name
                reading = (halt.get("speeds") or {}).get(str(offset))
                if reading and isinstance(reading.get("speed"), (int, float)):
                    speed = reading['speed']
                    row.append(f"{speed:.1f}")

                    # Apply conditional formatting rules
                    if offset == 100:
                        if speed > 25:
                            warning_cells.append((col_idx, row_idx))  # Red background
                        elif speed >= 20:
                            yellow_warning_cells.append((col_idx, row_idx))  # Yellow background
                    elif offset == 20:
                        if speed > 15:
                            warning_cells.append((col_idx, row_idx))  # Red background
                        elif speed >= 10:
                            yellow_warning_cells.append((col_idx, row_idx))  # Yellow background
                    elif offset == 1000:
                        # Zone-based threshold: 70 for suburban, 60/90 for mainline, 40 for ghat section
                        threshold_1000m = _get_1000m_threshold(
                            halt_list_idx, boundary_halt_idx, from_station, to_station, direction, train_number, in_ghat
                        )
                        if speed >= threshold_1000m + 1:
                            warn_text_cells.append((col_idx, row_idx))
                    elif offset == 300:
                        # 400m threshold only applies in Zone A (suburban) - uses 300m speed data
                        threshold_400m = _get_400m_threshold(
                            halt_list_idx, boundary_halt_idx, from_station, direction, train_number
                        )
                        if threshold_400m is not None and speed >= threshold_400m + 1:
                            warn_text_cells.append((col_idx, row_idx))
                else:
                    row.append("—")
            row.append("0.0")
            data.append(row)

        col_count = len(header)
        brake_table = Table(
            data,
            colWidths=[120] + [ (doc.width - 120) / (col_count - 1) ] * (col_count - 1),
        )

        # Build table style with conditional cell backgrounds
        table_style = [
            ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]

        # Add red background for critical warning cells (100m > 25, 20m > 15)
        warning_color = colors.Color(1, 0.898, 0.898)  # #ffe5e5 in RGB
        red_text = colors.Color(0.843, 0.149, 0.239)  # #d7263d
        for col, row in warning_cells:
            table_style.append(("BACKGROUND", (col, row), (col, row), warning_color))
            table_style.append(("TEXTCOLOR", (col, row), (col, row), red_text))

        # Add yellow background for caution cells (20m: 10-15 km/h)
        yellow_color = colors.Color(1, 0.976, 0.769)  # #fff9c4 light yellow
        for col, row in yellow_warning_cells:
            table_style.append(("BACKGROUND", (col, row), (col, row), yellow_color))

        # Add red text only for moderate warnings (1000m > 70/60 based on zone)
        for col, row in warn_text_cells:
            table_style.append(("TEXTCOLOR", (col, row), (col, row), red_text))

        brake_table.setStyle(TableStyle(table_style))
        story.append(brake_table)

        # Add footnote if any halt is in ghat section
        if has_ghat_section:
            footnote_style = styles["Normal"].clone("footnote")
            footnote_style.fontSize = 8
            footnote_style.textColor = colors.Color(0.4, 0.4, 0.4)
            footnote_style.fontName = "Helvetica-Oblique"
            story.append(Paragraph("▲ Ghat section (steep gradient descent) — 40 km/h limit at 1000m", footnote_style))

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

    # Add footer on last page
    story.append(Spacer(1, 20))

    # Horizontal line
    line_drawing = Drawing(6*inch, 1)
    line_drawing.add(Line(0, 0, 6*inch, 0, strokeColor=colors.grey, strokeWidth=0.5))
    story.append(line_drawing)

    story.append(Spacer(1, 6))

    # Footer text in small font
    footer_style = getSampleStyleSheet()["Normal"]
    footer_style.fontSize = 8
    footer_style.alignment = 1  # Center alignment
    footer_style.textColor = colors.grey
    footer_text = "Developed by Jayakumar M D MMan CSMT under the guidelines of Sr DEE TRSO Shri Hemant Jindal"
    story.append(Paragraph(footer_text, footer_style))

    doc.build(story)
    buffer.seek(0)
    return buffer


def _generate_pdf_filename(filtered_df: pl.DataFrame, criteria: dict) -> str:
    """
    Generate PDF filename in format: DriverName-TrainNumber-LocoNumber-Date.pdf
    Example: BP Mittal-16345-30310-22/11/25.pdf (slashes replaced with safe chars)
    """
    # Extract date of working from first row
    date_str = ""
    if filtered_df.height > 0:
        # Find time column
        time_col = None
        for col in filtered_df.columns:
            col_lower = col.lower()
            if "time" in col_lower and ("gps" in col_lower or "logging" in col_lower or col_lower == "time"):
                time_col = col
                break

        if time_col:
            first_timestamp = filtered_df[time_col][0]
            if first_timestamp:
                # Parse timestamp and format as DDMMYY
                timestamp_str = str(first_timestamp)
                # Handle formats like "2025-11-15 22:30:00" or "15/11/2025 22:30:00"
                try:
                    # Try parsing common formats
                    from datetime import datetime
                    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y"]:
                        try:
                            dt = datetime.strptime(timestamp_str.split('.')[0].strip(), fmt)
                            date_str = dt.strftime("%d%m%y")
                            break
                        except ValueError:
                            continue
                except Exception:
                    pass

    # Get train number
    train_num = str(criteria.get("train_number", "")).strip() or "unknown"

    # Get loco number
    loco_num = str(criteria.get("loco_number", "")).strip() or "unknown"

    # Driver name (LP)
    raw_driver = str(criteria.get("lp_name", "")).strip()
    driver_name = raw_driver if raw_driver else "Unknown Driver"
    driver_name = re.sub(r"[^A-Za-z0-9 _-]", "", driver_name).strip()
    if not driver_name:
        driver_name = "Unknown Driver"

    # Fallback to current date if extraction failed
    if not date_str:
        date_str = datetime.now().strftime("%d-%m-%y")
    else:
        # if already digits (ddmmyy) reformat to dd-mm-yy
        if re.fullmatch(r"\d{6}", date_str):
            date_str = f"{date_str[:2]}-{date_str[2:4]}-{date_str[4:]}"
    safe_date = date_str.replace("/", "-")
    filename = f"{driver_name}-{train_num}-{loco_num}-{safe_date}.pdf"
    return filename


@app.post("/export_pdf")
def export_pdf(request: Request, criteria: dict = Body(...), run_id: str | None = Query(None)):
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    filtered = apply_criteria(df, criteria)
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

    # Compute boundary row index for zone-based 1000m threshold
    to_station = (criteria.get("to_station_equals") or "").strip().upper()
    from_station = (criteria.get("from_station_equals") or "").strip().upper()
    direction = (criteria.get("direction_equals") or "").strip().upper()
    boundary_type = ROUTE_BOUNDARY_MAP.get(to_station) or ROUTE_BOUNDARY_MAP.get(from_station)
    boundary_stations = ZONE_BOUNDARY_STATIONS.get(boundary_type, []) if boundary_type else []
    boundary_row_idx = _find_boundary_row_index(filtered, boundary_stations) if boundary_stations else None

    # Compute ghat section halts for steep gradient threshold
    ghat_halt_indices = _compute_ghat_section_halts(filtered, unified_data, direction)

    try:
        speed_chart = _render_speed_chart_image(chart_payload)
        brake_charts = _render_brake_curve_images(unified_data)  # Returns list of images
        sectional_charts = _generate_sectional_charts(filtered, criteria)  # Generate sectional speed profiles
        pdf_buffer = _render_pdf_report(summary, speed_chart, brake_charts, unified_data, brake_tests, sectional_charts, boundary_row_idx, ghat_halt_indices)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    # Generate dynamic filename: DDMMYY-TRAINNUMBER-LPNAME.pdf
    filename = _generate_pdf_filename(filtered, criteria)

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


@app.get("/stations")
def get_all_stations():
    """Get all stations from route graph for dropdown population."""
    stations = set()

    # From ROUTE_SEQUENCES
    for route_id, station_list in ROUTE_SEQUENCES.items():
        stations.update(station_list)

    # From ROUTE_STATIONS
    for route_id, station_set in ROUTE_STATIONS.items():
        stations.update(station_set)

    return {
        "stations": sorted(list(stations))
    }


@app.post("/validate_route")
def validate_route(request: Request, criteria: Dict[str, Any] = Body(...), run_id: str | None = Query(None)):
    """
    Validate route selection after geofence slicing.
    Provides immediate feedback before analysis.
    Does NOT validate entire CSV direction (preserves multi-direction CSV handling).
    """
    try:
        run_id_val = _resolve_run_id(run_id, criteria)
        _, df = _get_user_run(request, run_id_val)
    except HTTPException as exc:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    from_station = criteria.get("from_station_equals")
    to_station = criteria.get("to_station_equals")
    direction = criteria.get("direction_equals")

    if not from_station or not to_station:
        return JSONResponse({
            "valid": False,
            "errors": ["from_station and to_station are required"]
        }, status_code=400)

    # Apply geofence slicing (current smart logic - handles multi-direction CSVs)
    try:
        sliced = apply_criteria(df, criteria)
    except Exception as e:
        return JSONResponse({
            "valid": False,
            "errors": [f"Error applying criteria: {str(e)}"]
        }, status_code=400)

    warnings = []
    errors = []

    # Check 1: Data exists after slicing?
    if sliced.height == 0:
        return JSONResponse({
            "valid": False,
            "errors": [
                f"No data found for {from_station} → {to_station}.",
                "The route may not exist in this CSV file.",
                "Please verify the route selection or try Manual Route Override."
            ],
            "sliced_rows": 0,
            "route_exists": False
        }, status_code=200)

    # Check 2: Route exists in ROUTE_SECTION_MAP?
    route_key = _route_key(from_station, to_station)
    route_exists = False
    for dir_suffix in ["UP", "DN"]:
        if (route_key, dir_suffix) in ROUTE_SECTION_MAP:
            route_exists = True
            break

    if not route_exists:
        warnings.append(
            f"Route {route_key} not configured in system. "
            f"MPS and PSR overlays will not be available. "
            f"Raw speed data and braking analysis will still work."
        )

    # Check 3: Station sequence validation (optional)
    route_sequences_key = f"{from_station}-{to_station}"
    if route_sequences_key in ROUTE_SEQUENCES:
        expected_seq = ROUTE_SEQUENCES[route_sequences_key]
        station_col = _first_matching_column(sliced.columns, "STATION", "CODE") or \
                      _first_matching_column(sliced.columns, "STATION")

        if station_col and station_col in sliced.columns:
            actual_stations = [
                str(s).strip().upper()
                for s in sliced[station_col].unique().to_list()
                if s and str(s).strip() and str(s).strip().upper() != "NONE"
            ]

            # Calculate coverage
            matched = sum(1 for s in actual_stations if s in expected_seq)
            coverage = matched / len(expected_seq) if expected_seq else 0

            if coverage < 0.3:
                errors.append(
                    f"Low station coverage ({coverage*100:.0f}%). "
                    f"Data may not match the selected route."
                )
            elif coverage < 0.7:
                warnings.append(
                    f"Partial station coverage ({coverage*100:.0f}%). "
                    f"This may be a partial journey or some stations are missing from data."
                )

    # Check 4: Direction reversal and destination check (critical)
    station_col = _first_matching_column(sliced.columns, "STATION", "CODE") or \
                  _first_matching_column(sliced.columns, "STATION")

    if station_col and station_col in sliced.columns:
        stations_list = sliced[station_col].to_list()

        # Find first and last non-null stations
        first_station = None
        last_station = None
        all_stations = set()

        for s in stations_list:
            if s and str(s).strip() and str(s).strip().upper() != "NONE":
                station_upper = str(s).strip().upper()
                all_stations.add(station_upper)
                if first_station is None:
                    first_station = station_upper
                last_station = station_upper

        if first_station and last_station:
            expected_from = from_station.strip().upper()
            expected_to = to_station.strip().upper()

            # Check for complete reversal
            if first_station == expected_to and last_station == expected_from:
                return JSONResponse({
                    "valid": False,
                    "errors": [
                        f"Direction REVERSED!",
                        f"Data goes {first_station} → {last_station}",
                        f"But selected route is {expected_from} → {expected_to}",
                        f"Please swap from/to stations in Manual Route Override."
                    ],
                    "sliced_rows": sliced.height,
                    "route_exists": route_exists,
                    "reversed": True
                }, status_code=200)

            # Check if destination station exists in data
            if expected_to not in all_stations:
                return JSONResponse({
                    "valid": False,
                    "errors": [
                        f"Destination station {expected_to} not found in data!",
                        f"Data goes from {first_station} to {last_station}",
                        f"But you selected route {expected_from} → {expected_to}",
                        f"The CSV file may contain wrong route data.",
                        f"Please verify the CSV file or use Manual Route Override to select the correct route."
                    ],
                    "sliced_rows": sliced.height,
                    "route_exists": route_exists
                }, status_code=200)

            # Check if starting station exists in data
            if expected_from not in all_stations:
                return JSONResponse({
                    "valid": False,
                    "errors": [
                        f"Starting station {expected_from} not found in data!",
                        f"Data goes from {first_station} to {last_station}",
                        f"But you selected route {expected_from} → {expected_to}",
                        f"The CSV file may contain wrong route data.",
                        f"Please verify the CSV file or use Manual Route Override to select the correct route."
                    ],
                    "sliced_rows": sliced.height,
                    "route_exists": route_exists
                }, status_code=200)

    return {
        "valid": len(errors) == 0,
        "warnings": warnings,
        "errors": errors,
        "route_exists": route_exists,
        "sliced_rows": sliced.height,
        "route_key": route_key
    }


# ------------------------------
# Check if Analysis Exists (Duplicate Detection)
# ------------------------------
@app.post("/api/check-analysis-exists")
async def check_analysis_exists(request: Request, data: Dict[str, Any] = Body(...)):
    """
    Check if an analysis already exists for the same LP, date, train, and route.
    Used to prevent duplicates and warn users before analyzing.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            {"error": "Authentication required", "exists": False},
            status_code=401
        )

    try:
        lp_hrms_id = data.get("lp_hrms_id")
        working_date = data.get("working_date")
        train_number = data.get("train_number")
        from_station = data.get("from_station")
        to_station = data.get("to_station")

        # If missing key fields, cannot check
        if not all([lp_hrms_id, working_date, train_number, from_station, to_station]):
            return {"exists": False}

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        sql = """
            SELECT
                id, user_id, lp_name, lp_hrms_id,
                train_number, from_station, to_station,
                analyst_name, created_at, pdf_filename,
                working_date
            FROM div_rtis_analyses
            WHERE lp_hrms_id = %s
              AND working_date = %s
              AND train_number = %s
              AND from_station = %s
              AND to_station = %s
            ORDER BY created_at DESC
            LIMIT 1
        """

        cursor.execute(sql, (lp_hrms_id, working_date, train_number, from_station, to_station))
        result = cursor.fetchone()
        cursor.close()
        conn.close()

        if result:
            # Format datetime for display
            created_at = result['created_at'].strftime('%d-%b-%Y %H:%M') if result['created_at'] else ''
            working_date_formatted = result['working_date'].strftime('%d-%b-%Y') if result['working_date'] else ''

            return {
                "exists": True,
                "analysis": {
                    "id": result['id'],
                    "lp_name": result['lp_name'],
                    "lp_hrms_id": result['lp_hrms_id'],
                    "train_number": result['train_number'],
                    "route": f"{result['from_station']} → {result['to_station']}",
                    "analyst_name": result['analyst_name'] or 'Unknown',
                    "created_at": created_at,
                    "working_date": working_date_formatted,
                    "pdf_filename": result['pdf_filename'],
                    "has_pdf": bool(result['pdf_filename'])
                }
            }
        else:
            return {"exists": False}

    except Exception as e:
        print(f"[ERROR] check_analysis_exists: {e}")
        return {"exists": False, "error": str(e)}


# ------------------------------
# Save Analysis to Database
# ------------------------------
@app.post("/api/save-analysis")
async def save_analysis(request: Request, data: Dict[str, Any] = Body(...)):
    print("####SAVE_ANALYSIS#### CALLED")
    """
    Save analysis record to div_rtis_analyses table.
    Requires authenticated user session.

    Expected data structure:
    {
        "criteria": {...},           # Analysis criteria (from getCriteria())
        "csv_filename": "...",       # Uploaded CSV filename
        "csv_file_size": 123456,     # File size in bytes
        "pdf_filename": "...",       # Generated PDF filename (optional)
        "results": {                 # Analysis summary metrics
            "total_distance": 123.45,
            "total_duration": 7200,
            "max_speed": 110.5,
            "avg_speed": 65.2,
            "halt_count": 5,
            "braking_events_count": 12
        },
        "notes": "...",              # Optional user notes
        "metadata": {...}            # Optional additional JSON data
    }
    """
    # Authenticate user
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            {"error": "Authentication required", "success": False},
            status_code=401
        )

    try:
        # Extract data
        criteria = data.get("criteria", {})
        csv_filename = data.get("csv_filename", "")
        csv_file_size = data.get("csv_file_size")
        pdf_filename = data.get("pdf_filename")
        results = data.get("results", {})
        notes = data.get("notes")
        metadata = data.get("metadata")

        # Convert working_date from DD-MM-YYYY to YYYY-MM-DD for MySQL
        working_date_raw = criteria.get("working_date")
        working_date_sql = None
        if working_date_raw:
            try:
                # Try DD-MM-YYYY format first
                parsed = datetime.strptime(working_date_raw, "%d-%m-%Y")
                working_date_sql = parsed.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    # Try DD/MM/YYYY format
                    parsed = datetime.strptime(working_date_raw, "%d/%m/%Y")
                    working_date_sql = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    # Already in correct format or other format, use as-is
                    working_date_sql = working_date_raw

        # Validate required fields
        if not csv_filename:
            return JSONResponse(
                {"error": "csv_filename is required", "success": False},
                status_code=400
            )

        train_number = (criteria.get("train_number") or "").strip()
        loco_number = (criteria.get("loco_number") or "").strip()

        if not train_number or not loco_number:
            return JSONResponse(
                {"error": "Train number and Loco number are required", "success": False},
                status_code=400
            )

        # Get database connection
        conn = get_db_connection()
        cursor = conn.cursor()

        # Compute BFT/BPT status from brake tests (per-run DF)
        bft_status = 'NOT RUN'
        bpt_status = 'NOT RUN'
        user_df = None
        try:
            print(f"####BRAKING#### data.get('run_id')={data.get('run_id')}")
            run_id_val = _resolve_run_id(None, data)
            print(f"####BRAKING#### run_id_val={run_id_val}")
            _, user_df = _get_user_run(request, run_id_val)
            print(f"####BRAKING#### Got user_df with {user_df.height if user_df is not None else 0} rows")
        except HTTPException as e:
            print(f"####BRAKING#### HTTPException getting user_df: {e.detail}")
        except Exception as e:
            print(f"####BRAKING#### Exception getting user_df: {e}")

        if user_df is not None and not user_df.is_empty():
            try:
                filtered = apply_criteria(user_df, criteria)
                if filtered.height > 0:
                    start_station = criteria.get("from_station_equals")
                    direction = criteria.get("direction_equals")
                    brake_tests = _brake_tests(filtered, start_station, direction)
                    if brake_tests:
                        feel_status = brake_tests.get('feel', {}).get('status', 'NOT RUN')
                        power_status = brake_tests.get('power', {}).get('status', 'NOT RUN')
                        if feel_status == 'PASS':
                            bft_status = 'PASS'
                        elif feel_status == 'FAIL':
                            bft_status = 'FAIL'
                        if power_status == 'PASS':
                            bpt_status = 'PASS'
                        elif power_status == 'FAIL':
                            bpt_status = 'FAIL'
            except Exception as bte:
                print(f"[WARN] Failed to compute brake test status: {bte}")

        # Prepare SQL insert (includes ALP fields and BFT/BPT status)
        sql = """
            INSERT INTO div_rtis_analyses (
                user_id, lp_hrms_id, lp_name, ncli_id, ncli_name,
                alp_hrms_id, alp_name, ncli_alp_name,
                analyst_id, analyst_name, analysis_date, working_date,
                train_number, from_station, to_station, direction, route,
                loco_number, coach_type, load_type, brake_position,
                csv_filename, csv_file_size, pdf_filename,
                total_distance, total_duration, max_speed, avg_speed,
                halt_count, braking_events_count, notes, metadata,
                bft_status, bpt_status
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s
            )
        """

        # Prepare values
        import json
        values = (
            user["id"],
            criteria.get("lp_hrms_id"),
            criteria.get("lp_name"),
            criteria.get("cli_id"),  # ncli_id
            criteria.get("ncli_name"),
            criteria.get("alp_hrms_id"),
            criteria.get("alp_name"),
            criteria.get("ncli_alp_name"),
            criteria.get("analyst_cli_id"),  # analyst_id
            criteria.get("analyst_name"),
            datetime.now().date(),  # analysis_date
            working_date_sql,  # Converted to YYYY-MM-DD format
            criteria.get("train_number"),
            criteria.get("from_station_equals"),
            criteria.get("to_station_equals"),
            criteria.get("direction_equals"),
            criteria.get("route"),  # Will be in metadata if not in criteria
            criteria.get("loco_number"),
            criteria.get("coach_type"),
            criteria.get("load_type"),
            criteria.get("brake_position"),
            csv_filename,
            csv_file_size,
            pdf_filename,
            results.get("total_distance"),
            results.get("total_duration"),
            results.get("max_speed"),
            results.get("avg_speed"),
            results.get("halt_count", 0),
            results.get("braking_events_count", 0),
            notes,
            json.dumps(metadata) if metadata else None,
            bft_status,
            bpt_status
        )

        # Execute insert
        cursor.execute(sql, values)
        conn.commit()

        # Get the inserted ID
        analysis_id = cursor.lastrowid

        # Detect and save violations if DF is loaded (per-run)
        violations_saved = 0
        if user_df is not None and not user_df.is_empty():
            try:
                # Compute braking profile
                filtered = apply_criteria(user_df, criteria)
                if filtered.height > 0:
                    halts = _braking_profile(filtered, VIOLATIONS_OFFSETS)
                    violations = _detect_violations(filtered, halts, criteria)

                    # Save violations to database
                    if violations:
                        # Get working_date from CSV data
                        working_date = _first_datetime(filtered)
                        working_date_str = working_date.date() if isinstance(working_date, datetime) else None

                        violation_sql = """
                            INSERT INTO div_rtis_violations (
                                analysis_id, working_date, train_number, loco_number,
                                lp_name, lp_hrms_id, ncli_name,
                                alp_name, alp_hrms_id, ncli_alp_name,
                                violation_type, speed, threshold, halt_station, zone, violation_time
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """

                        for v in violations:
                            cursor.execute(violation_sql, (
                                analysis_id,
                                working_date_str,
                                criteria.get("train_number"),
                                criteria.get("loco_number"),
                                criteria.get("lp_name"),
                                criteria.get("lp_hrms_id"),
                                criteria.get("ncli_name"),
                                criteria.get("alp_name"),
                                criteria.get("alp_hrms_id"),
                                criteria.get("ncli_alp_name"),
                                v["violation_type"],
                                v["speed"],
                                v["threshold"],
                                v["halt_station"],
                                v["zone"],
                                v.get("halt_time"),
                            ))
                            violations_saved += 1

                        conn.commit()
            except Exception as ve:
                print(f"[WARN] Failed to save violations: {ve}")

        # Save braking pattern data for nominated halts
        braking_halts_saved = 0
        print(f"####BRAKING#### user_df is None: {user_df is None}, empty: {user_df.is_empty() if user_df is not None else 'N/A'}")
        if user_df is not None and not user_df.is_empty():
            try:
                filtered = apply_criteria(user_df, criteria)
                print(f"####BRAKING#### filtered rows={filtered.height}")
                if filtered.height > 0:
                    direction = criteria.get("direction_equals", "UP")
                    print(f"####BRAKING#### Direction={direction}, calling _save_braking_run_data")
                    braking_halts_saved = _save_braking_run_data(conn, analysis_id, criteria, filtered, direction)
                    print(f"####BRAKING#### Halts saved: {braking_halts_saved}")
            except Exception as be:
                import traceback
                print(f"####BRAKING#### EXCEPTION: {be}")
                traceback.print_exc()
        else:
            print("####BRAKING#### Skipped - user_df is None or empty")

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "Analysis saved successfully",
            "analysis_id": analysis_id,
            "violations_saved": violations_saved,
            "braking_halts_saved": braking_halts_saved
        }

    except mysql.connector.IntegrityError as e:
        # Duplicate key error (error code 1062)
        if e.errno == 1062:
            return JSONResponse(
                {
                    "error": "Analysis already exists for this trip (same date, train, route, and loco). Use update instead.",
                    "success": False,
                    "duplicate": True
                },
                status_code=409
            )
        return JSONResponse(
            {"error": f"Database integrity error: {str(e)}", "success": False},
            status_code=400
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to save analysis: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# Update Existing Analysis
# ------------------------------
@app.put("/api/update-analysis/{analysis_id}")
async def update_analysis(analysis_id: int, request: Request, data: Dict[str, Any] = Body(...)):
    """
    Update an existing analysis record (used when user wants to update/replace).
    Only allows updating if user is authenticated.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse(
            {"error": "Authentication required", "success": False},
            status_code=401
        )

    try:
        criteria = data.get("criteria", {})
        csv_filename = data.get("csv_filename", "")
        csv_file_size = data.get("csv_file_size")
        pdf_filename = data.get("pdf_filename")
        results = data.get("results", {})
        notes = data.get("notes")
        metadata = data.get("metadata")

        # Convert working_date from DD-MM-YYYY to YYYY-MM-DD for MySQL
        working_date_raw = criteria.get("working_date")
        working_date_sql = None
        if working_date_raw:
            try:
                parsed = datetime.strptime(working_date_raw, "%d-%m-%Y")
                working_date_sql = parsed.strftime("%Y-%m-%d")
            except ValueError:
                try:
                    parsed = datetime.strptime(working_date_raw, "%d/%m/%Y")
                    working_date_sql = parsed.strftime("%Y-%m-%d")
                except ValueError:
                    working_date_sql = working_date_raw

        if not csv_filename:
            return JSONResponse(
                {"error": "csv_filename is required", "success": False},
                status_code=400
            )

        train_number = (criteria.get("train_number") or "").strip()
        loco_number = (criteria.get("loco_number") or "").strip()

        if not train_number or not loco_number:
            return JSONResponse(
                {"error": "Train number and Loco number are required", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor()

        # Update SQL
        sql = """
            UPDATE div_rtis_analyses SET
                user_id = %s,
                lp_hrms_id = %s,
                lp_name = %s,
                ncli_id = %s,
                ncli_name = %s,
                analyst_id = %s,
                analyst_name = %s,
                analysis_date = %s,
                working_date = %s,
                train_number = %s,
                from_station = %s,
                to_station = %s,
                direction = %s,
                route = %s,
                loco_number = %s,
                coach_type = %s,
                load_type = %s,
                brake_position = %s,
                csv_filename = %s,
                csv_file_size = %s,
                pdf_filename = %s,
                total_distance = %s,
                total_duration = %s,
                max_speed = %s,
                avg_speed = %s,
                halt_count = %s,
                braking_events_count = %s,
                notes = %s,
                metadata = %s,
                updated_at = NOW()
            WHERE id = %s
        """

        import json
        values = (
            user["id"],
            criteria.get("lp_hrms_id"),
            criteria.get("lp_name"),
            criteria.get("cli_id"),
            criteria.get("ncli_name"),
            criteria.get("analyst_cli_id"),
            criteria.get("analyst_name"),
            datetime.now().date(),
            working_date_sql,  # Converted to YYYY-MM-DD format
            criteria.get("train_number"),
            criteria.get("from_station_equals"),
            criteria.get("to_station_equals"),
            criteria.get("direction_equals"),
            criteria.get("route"),
            criteria.get("loco_number"),
            criteria.get("coach_type"),
            criteria.get("load_type"),
            criteria.get("brake_position"),
            csv_filename,
            csv_file_size,
            pdf_filename,
            results.get("total_distance"),
            results.get("total_duration"),
            results.get("max_speed"),
            results.get("avg_speed"),
            results.get("halt_count", 0),
            results.get("braking_events_count", 0),
            notes,
            json.dumps(metadata) if metadata else None,
            analysis_id
        )

        cursor.execute(sql, values)
        conn.commit()

        rows_affected = cursor.rowcount
        cursor.close()
        conn.close()

        if rows_affected > 0:
            return {
                "success": True,
                "message": "Analysis updated successfully",
                "analysis_id": analysis_id,
                "updated": True
            }
        else:
            return JSONResponse(
                {"error": f"Analysis ID {analysis_id} not found", "success": False},
                status_code=404
            )

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to update analysis: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# ALP Search & Resolve endpoints
# ------------------------------
@app.get("/alp-search")
async def alp_search(q: str = Query(default=""), depot: str = Query(default="")):
    """Search ALP/Sr.ALP staff by name (designation_id IN (1, 2))
    Optional depot parameter: KYN or PNVL to filter by other depots
    """
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        search_term = f"%{q.strip()}%" if q.strip() else "%"

        # Build office filter based on depot parameter
        if depot and depot.upper() in ('KYN', 'PNVL'):
            depot_upper = depot.upper()
            office_filter = f"""
                AND (
                    s.current_office_code = '{depot_upper}-ML'
                    OR s.current_cms_id LIKE '{depot_upper}%'
                )
            """
        else:
            office_filter = "AND s.current_office_code = 'CSMT-ML'"

        query = f"""
            SELECT
                s.hrms_id,
                s.name,
                s.current_cms_id,
                s.current_cli_id,
                c.cli_name,
                d.designation_name
            FROM div_staff_master s
            LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
            LEFT JOIN designations d ON s.designation_id = d.id
            WHERE s.designation_id IN (1, 2)
              AND s.status = 'Active'
              {office_filter}
              AND s.name LIKE %s
            ORDER BY s.name
            LIMIT 50
        """

        cursor.execute(query, (search_term,))
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"success": True, "data": results}

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/alp/{hrms_id}")
async def get_alp_by_hrms(hrms_id: str):
    """Get ALP details by HRMS ID including CLI info"""
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT
                s.hrms_id,
                s.name,
                s.current_cms_id,
                s.current_cli_id,
                c.cli_name,
                d.designation_name
            FROM div_staff_master s
            LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
            LEFT JOIN designations d ON s.designation_id = d.id
            WHERE s.hrms_id = %s
              AND s.designation_id IN (1, 2)
            LIMIT 1
        """

        cursor.execute(query, (hrms_id,))
        result = cursor.fetchone()

        cursor.close()
        conn.close()

        if not result:
            return JSONResponse({"success": False, "error": "ALP not found"}, status_code=404)

        return {"success": True, "data": result}

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ------------------------------
# LP Search & Resolve endpoints
# ------------------------------
@app.get("/lp-search")
async def lp_search(q: str = Query(default=""), depot: str = Query(default="")):
    """Search LP/Sr.LP staff by name (designation_id IN (5, 6, 7, 8))
    Optional depot parameter: KYN or PNVL to filter by other depots
    """
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        search_term = f"%{q.strip()}%" if q.strip() else "%"

        # Build office filter based on depot parameter
        if depot and depot.upper() in ('KYN', 'PNVL'):
            depot_upper = depot.upper()
            office_filter = f"""
                AND (
                    s.current_office_code = '{depot_upper}-ML'
                    OR s.current_cms_id LIKE '{depot_upper}%'
                )
            """
        else:
            office_filter = "AND s.current_office_code = 'CSMT-ML'"

        query = f"""
            SELECT
                s.hrms_id,
                s.name,
                s.current_cms_id,
                s.current_cli_id,
                c.cli_name,
                d.designation_name
            FROM div_staff_master s
            LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
            LEFT JOIN designations d ON s.designation_id = d.id
            WHERE s.designation_id IN (5, 6, 7)
              AND s.status = 'Active'
              {office_filter}
              AND s.name LIKE %s
            ORDER BY s.name
            LIMIT 50
        """

        cursor.execute(query, (search_term,))
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"success": True, "data": results}

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/lp/{hrms_id}")
async def get_lp_by_hrms(hrms_id: str):
    """Get LP details by HRMS ID including CLI info"""
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        query = """
            SELECT
                s.hrms_id,
                s.name,
                s.current_cms_id,
                s.current_cli_id,
                c.cli_name,
                d.designation_name
            FROM div_staff_master s
            LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
            LEFT JOIN designations d ON s.designation_id = d.id
            WHERE s.hrms_id = %s
              AND s.designation_id IN (5, 6, 7)
            LIMIT 1
        """

        cursor.execute(query, (hrms_id,))
        result = cursor.fetchone()

        cursor.close()
        conn.close()

        if not result:
            return JSONResponse({"success": False, "error": "LP not found"}, status_code=404)

        return {"success": True, "data": result}

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ------------------------------
# Daily Violations Report API
# ------------------------------
@app.get("/api/daily-violations")
async def get_daily_violations(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    date_type: str = Query(default="analysis", description="Date type: analysis (created_at) or working (working_date)"),
    violation_type: str = Query(default="all", description="Type: all, 1000m_zone_b, 400m_zone_a, ghat")
):
    """
    Get daily violations report for a specific date.

    Args:
        date: Date in YYYY-MM-DD format
        date_type: 'analysis' (default) filters by created_at, 'working' filters by working_date
        violation_type: Filter by violation type

    Returns violations grouped by type:
    - 1000m_zone_b: Zone B (mainline) violations at 1000m (>60 or >90 for 222xx)
    - 400m_zone_a: Zone A (suburban) violations at 400m (>30 or >40 for 222xx)
    - ghat: Ghat section violations at 1000m (>40, UP direction only)
    """
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        # Build query based on date type and violation type filter
        date_column = "created_at" if date_type == "analysis" else "working_date"
        type_filter = ""
        params = [date]

        if violation_type != "all":
            type_filter = "AND violation_type = %s"
            params.append(violation_type)

        query = f"""
            SELECT
                id,
                analysis_id,
                working_date,
                train_number,
                loco_number,
                lp_name,
                lp_hrms_id,
                ncli_name,
                alp_name,
                alp_hrms_id,
                ncli_alp_name,
                violation_type,
                speed,
                threshold,
                halt_station,
                zone,
                created_at
            FROM div_rtis_violations
            WHERE DATE({date_column}) = %s
            {type_filter}
            ORDER BY violation_type, train_number, halt_station
        """

        cursor.execute(query, params)
        results = cursor.fetchall()

        # Group by violation type for easier UI rendering
        grouped = {
            "1000m_zone_b": [],
            "400m_zone_a": [],
            "ghat": []
        }

        for row in results:
            vtype = row.get("violation_type")
            if vtype in grouped:
                # Convert date objects to strings for JSON serialization
                if row.get("working_date"):
                    row["working_date"] = row["working_date"].strftime("%d.%m.%Y") if hasattr(row["working_date"], "strftime") else str(row["working_date"])
                if row.get("created_at"):
                    row["created_at"] = row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"])
                grouped[vtype].append(row)

        cursor.close()
        conn.close()

        return {
            "success": True,
            "date": date,
            "data": grouped,
            "counts": {
                "1000m_zone_b": len(grouped["1000m_zone_b"]),
                "400m_zone_a": len(grouped["400m_zone_a"]),
                "ghat": len(grouped["ghat"]),
                "total": len(results)
            }
        }

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.get("/api/violations-export")
async def export_violations_pdf(
    date: str = Query(..., description="Date in YYYY-MM-DD format"),
    date_type: str = Query(default="analysis", description="Date type: analysis (created_at) or working (working_date)"),
    violation_type: str = Query(default="all", description="Type: all, 1000m_zone_b, 400m_zone_a, ghat")
):
    """Export daily violations report as PDF"""
    if SimpleDocTemplate is None:
        return JSONResponse({"error": "PDF export requires reportlab"}, status_code=500)

    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        # Build query based on date type and violation type filter
        date_column = "created_at" if date_type == "analysis" else "working_date"
        type_filter = ""
        params = [date]

        if violation_type != "all":
            type_filter = "AND violation_type = %s"
            params.append(violation_type)

        query = f"""
            SELECT * FROM div_rtis_violations
            WHERE DATE({date_column}) = %s
            {type_filter}
            ORDER BY violation_type, train_number
        """

        cursor.execute(query, params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        # Group by violation type
        grouped = {"1000m_zone_b": [], "400m_zone_a": [], "ghat": []}
        for row in results:
            vtype = row.get("violation_type")
            if vtype in grouped:
                grouped[vtype].append(row)

        # Generate PDF
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=36, bottomMargin=36, leftMargin=36, rightMargin=36)
        styles = getSampleStyleSheet()
        story = []

        # Format date as DD/MM/YYYY for display
        try:
            from datetime import datetime as dt
            parsed_date = dt.strptime(date, "%Y-%m-%d")
            display_date = parsed_date.strftime("%d/%m/%Y")
        except:
            display_date = date

        # Title
        story.append(Paragraph(f"Daily Speed Violations Report - {display_date}", styles["Title"]))
        story.append(Spacer(1, 12))

        # Define table headers and section titles
        section_info = {
            "1000m_zone_b": ("Speed Violations at 1000m (Zone B - Mainline)", "60/90 km/h threshold"),
            "400m_zone_a": ("Speed Violations at 400m (Zone A - Suburban)", "30/40 km/h threshold"),
            "ghat": ("Ghat Section Violations (Steep Gradient)", "40 km/h threshold"),
        }

        headers = ["SR", "DATE", "TR NO", "LOCO", "LP NAME", "NCLI", "ALP NAME", "NCLI-ALP", "SPEED", "HALT"]

        # Style for name cells with CMS ID below
        name_style = ParagraphStyle('NameCell', fontSize=7, leading=8)
        cms_style = ParagraphStyle('CMSCell', fontSize=5, leading=6, textColor=colors.grey)
        # Style for NCLI cells with text wrapping
        ncli_style = ParagraphStyle('NCLICell', fontSize=7, leading=8, wordWrap='CJK')

        for vtype, (title, subtitle) in section_info.items():
            violations = grouped.get(vtype, [])

            story.append(Paragraph(title, styles["Heading2"]))
            story.append(Paragraph(f"<i>{subtitle}</i>", styles["Normal"]))
            story.append(Spacer(1, 6))

            if not violations:
                story.append(Paragraph("<i>No violations recorded</i>", styles["Normal"]))
            else:
                data = [headers]
                for idx, v in enumerate(violations, 1):
                    wd = v.get("working_date")
                    if hasattr(wd, "strftime"):
                        wd = wd.strftime("%d.%m")

                    # Format LP name with HRMS ID below
                    lp_name = str(v.get("lp_name") or "")
                    lp_hrms = str(v.get("lp_hrms_id") or "")
                    lp_cell = Paragraph(f"{lp_name}<br/><font size=5 color='grey'>{lp_hrms}</font>", name_style) if lp_name else ""

                    # Format ALP name with HRMS ID below
                    alp_name = str(v.get("alp_name") or "")
                    alp_hrms = str(v.get("alp_hrms_id") or "")
                    alp_cell = Paragraph(f"{alp_name}<br/><font size=5 color='grey'>{alp_hrms}</font>", name_style) if alp_name else ""

                    # Format NCLI cells with text wrapping
                    ncli_lp = v.get("ncli_name") or ""
                    ncli_lp_cell = Paragraph(str(ncli_lp), ncli_style) if ncli_lp else ""
                    ncli_alp = v.get("ncli_alp_name") or ""
                    ncli_alp_cell = Paragraph(str(ncli_alp), ncli_style) if ncli_alp else ""

                    data.append([
                        str(idx),
                        str(wd or ""),
                        str(v.get("train_number") or ""),
                        str(v.get("loco_number") or ""),
                        lp_cell,
                        ncli_lp_cell,
                        alp_cell,
                        ncli_alp_cell,
                        f"{v.get('speed', 0):.1f}",
                        str(v.get("halt_station") or ""),
                    ])

                # Adjusted column widths: NCLI columns wider (70), SPEED/HALT narrower (28/30)
                table = Table(data, colWidths=[18, 35, 35, 38, 85, 70, 85, 70, 28, 30])
                table.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.4, 0.6)),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("ALIGN", (0, 0), (0, -1), "CENTER"),
                    ("ALIGN", (8, 0), (8, -1), "CENTER"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 2),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(table)

            story.append(Spacer(1, 16))

        doc.build(story)
        buffer.seek(0)

        filename = f"violations_{date}.pdf"
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ------------------------------
# Violations Analytics API
# ------------------------------
@app.get("/api/violations-analytics")
async def get_violations_analytics(
    from_date: str = Query(..., description="Start date YYYY-MM-DD"),
    to_date: str = Query(..., description="End date YYYY-MM-DD"),
    report_type: str = Query(..., description="Report type"),
    violation_type: str = Query(default="all"),
    staff_search: str = Query(default=""),
    min_count: int = Query(default=3)
):
    """
    Comprehensive violations analytics API supporting multiple report types.
    """
    try:
        conn = get_db_connection()
        if not conn:
            return JSONResponse({"success": False, "error": "Database connection failed"}, status_code=500)

        cursor = conn.cursor(dictionary=True)

        # Get summary counts first
        summary_query = """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN violation_type = '1000m_zone_b' THEN 1 ELSE 0 END) as zone_b,
                SUM(CASE WHEN violation_type = '400m_zone_a' THEN 1 ELSE 0 END) as zone_a,
                SUM(CASE WHEN violation_type = 'ghat' THEN 1 ELSE 0 END) as ghat
            FROM div_rtis_violations
            WHERE DATE(working_date) BETWEEN %s AND %s
        """
        cursor.execute(summary_query, (from_date, to_date))
        summary = cursor.fetchone() or {"total": 0, "zone_b": 0, "zone_a": 0, "ghat": 0}

        data = None

        # Build type filter
        type_filter = ""
        if violation_type != "all":
            type_filter = f"AND violation_type = '{violation_type}'"

        # Handle different report types
        if report_type == "date-range":
            query = f"""
                SELECT id, working_date, train_number, lp_name, lp_hrms_id, ncli_name,
                       violation_type, speed, threshold, halt_station
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s {type_filter}
                ORDER BY working_date DESC, train_number
            """
            cursor.execute(query, (from_date, to_date))
            results = cursor.fetchall()
            data = []
            for r in results:
                if r.get("working_date") and hasattr(r["working_date"], "strftime"):
                    r["working_date"] = r["working_date"].strftime("%d.%m.%Y")
                data.append(r)

        elif report_type == "trend":
            query = """
                SELECT DATE(working_date) as date, COUNT(*) as count
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                GROUP BY DATE(working_date)
                ORDER BY date
            """
            cursor.execute(query, (from_date, to_date))
            results = cursor.fetchall()
            data = [{"date": r["date"].strftime("%d/%m") if hasattr(r["date"], "strftime") else str(r["date"]), "count": r["count"]} for r in results]

        elif report_type == "time-period":
            query = """
                SELECT
                    CASE
                        WHEN HOUR(violation_time) BETWEEN 0 AND 5 THEN '00:00-06:00'
                        WHEN HOUR(violation_time) BETWEEN 6 AND 11 THEN '06:00-12:00'
                        WHEN HOUR(violation_time) BETWEEN 12 AND 17 THEN '12:00-18:00'
                        ELSE '18:00-24:00'
                    END AS time_period,
                    COUNT(*) AS count,
                    AVG(speed - threshold) AS avg_excess
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND violation_time IS NOT NULL
                GROUP BY time_period
                ORDER BY FIELD(time_period, '00:00-06:00', '06:00-12:00', '12:00-18:00', '18:00-24:00')
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "staff-history":
            if not staff_search:
                data = {"staff": None, "violations": []}
            else:
                # Find staff
                staff_query = """
                    SELECT DISTINCT lp_name as name, lp_hrms_id as hrms_id, ncli_name as designation
                    FROM div_rtis_violations
                    WHERE (lp_hrms_id LIKE %s OR lp_name LIKE %s)
                    LIMIT 1
                """
                search_term = f"%{staff_search}%"
                cursor.execute(staff_query, (search_term, search_term))
                staff = cursor.fetchone()

                if staff:
                    violations_query = """
                        SELECT working_date, train_number, violation_type, speed, threshold, halt_station
                        FROM div_rtis_violations
                        WHERE lp_hrms_id = %s OR lp_name LIKE %s
                        ORDER BY working_date DESC
                    """
                    cursor.execute(violations_query, (staff.get("hrms_id"), f"%{staff_search}%"))
                    violations = cursor.fetchall()
                    for v in violations:
                        if v.get("working_date") and hasattr(v["working_date"], "strftime"):
                            v["working_date"] = v["working_date"].strftime("%d.%m.%Y")
                    data = {"staff": staff, "violations": violations}
                else:
                    data = {"staff": None, "violations": []}

        elif report_type == "lp-ranking":
            query = """
                SELECT lp_name, lp_hrms_id, ncli_name, COUNT(*) as count,
                       AVG(speed - threshold) as avg_excess
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND lp_name IS NOT NULL AND lp_name != ''
                GROUP BY lp_hrms_id, lp_name, ncli_name
                ORDER BY count DESC
                LIMIT 50
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "alp-ranking":
            query = """
                SELECT alp_name, alp_hrms_id, ncli_alp_name as ncli_name, COUNT(*) as count,
                       AVG(speed - threshold) as avg_excess
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND alp_name IS NOT NULL AND alp_name != ''
                GROUP BY alp_hrms_id, alp_name, ncli_alp_name
                ORDER BY count DESC
                LIMIT 50
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "lp-least":
            query = """
                SELECT lp_name, lp_hrms_id, ncli_name, COUNT(*) as count,
                       AVG(speed - threshold) as avg_excess
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND lp_name IS NOT NULL AND lp_name != ''
                GROUP BY lp_hrms_id, lp_name, ncli_name
                HAVING count >= %s
                ORDER BY count ASC
                LIMIT 50
            """
            cursor.execute(query, (from_date, to_date, min_count))
            data = cursor.fetchall()

        elif report_type == "repeat-offenders":
            query = """
                SELECT lp_name, lp_hrms_id, ncli_name, COUNT(*) as count,
                       COUNT(DISTINCT DATE(working_date)) as days_with_violations,
                       AVG(speed - threshold) as avg_excess
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND lp_name IS NOT NULL AND lp_name != ''
                GROUP BY lp_hrms_id, lp_name, ncli_name
                HAVING count >= %s
                ORDER BY count DESC
                LIMIT 50
            """
            cursor.execute(query, (from_date, to_date, min_count))
            data = cursor.fetchall()

        elif report_type == "ncli-lp":
            query = """
                SELECT ncli_name, COUNT(*) as count,
                       COUNT(DISTINCT lp_hrms_id) as staff_count
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND ncli_name IS NOT NULL AND ncli_name != ''
                GROUP BY ncli_name
                ORDER BY count DESC
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "ncli-alp":
            query = """
                SELECT ncli_alp_name as ncli_name, COUNT(*) as count,
                       COUNT(DISTINCT alp_hrms_id) as staff_count
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                  AND ncli_alp_name IS NOT NULL AND ncli_alp_name != ''
                GROUP BY ncli_alp_name
                ORDER BY count DESC
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "station-wise":
            query = f"""
                SELECT halt_station, COUNT(*) as count,
                       SUM(CASE WHEN violation_type = '1000m_zone_b' THEN 1 ELSE 0 END) as zone_b,
                       SUM(CASE WHEN violation_type = '400m_zone_a' THEN 1 ELSE 0 END) as zone_a,
                       SUM(CASE WHEN violation_type = 'ghat' THEN 1 ELSE 0 END) as ghat
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s {type_filter}
                  AND halt_station IS NOT NULL AND halt_station != ''
                GROUP BY halt_station
                ORDER BY count DESC
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type == "route-wise":
            # We need to get route info from the analyses table via analysis_id
            query = f"""
                SELECT CONCAT(a.from_station, ' - ', a.to_station) as route,
                       COUNT(*) as count,
                       SUM(CASE WHEN v.violation_type = '1000m_zone_b' THEN 1 ELSE 0 END) as zone_b,
                       SUM(CASE WHEN v.violation_type = '400m_zone_a' THEN 1 ELSE 0 END) as zone_a,
                       SUM(CASE WHEN v.violation_type = 'ghat' THEN 1 ELSE 0 END) as ghat
                FROM div_rtis_violations v
                LEFT JOIN div_rtis_analyses a ON v.analysis_id = a.id
                WHERE DATE(v.working_date) BETWEEN %s AND %s {type_filter}
                GROUP BY a.from_station, a.to_station
                ORDER BY count DESC
            """
            cursor.execute(query, (from_date, to_date))
            data = cursor.fetchall()

        elif report_type in ["zone-comparison", "type-breakdown"]:
            data = {
                "zone_b": summary.get("zone_b") or 0,
                "zone_a": summary.get("zone_a") or 0,
                "ghat": summary.get("ghat") or 0
            }

        elif report_type == "severity":
            query = """
                SELECT working_date, train_number, lp_name, speed, threshold, halt_station
                FROM div_rtis_violations
                WHERE DATE(working_date) BETWEEN %s AND %s
                ORDER BY (speed - threshold) DESC
                LIMIT 100
            """
            cursor.execute(query, (from_date, to_date))
            results = cursor.fetchall()
            data = []
            for r in results:
                if r.get("working_date") and hasattr(r["working_date"], "strftime"):
                    r["working_date"] = r["working_date"].strftime("%d.%m.%Y")
                data.append(r)

        else:
            data = []

        cursor.close()
        conn.close()

        return {
            "success": True,
            "report_type": report_type,
            "from_date": from_date,
            "to_date": to_date,
            "summary": summary,
            "data": data
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ------------------------------
# Dashboard Stats endpoint
# ------------------------------
@app.get("/api/rtis-stats")
async def get_rtis_stats(request: Request):
    """Get RTIS analysis statistics for dashboard cards"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get yesterday's count
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM div_rtis_analyses
            WHERE DATE(analysis_date) = CURDATE() - INTERVAL 1 DAY
        """)
        yesterday_result = cursor.fetchone()
        yesterday_count = yesterday_result['count'] if yesterday_result else 0

        # Get total count
        cursor.execute("SELECT COUNT(*) as count FROM div_rtis_analyses")
        total_result = cursor.fetchone()
        total_count = total_result['count'] if total_result else 0

        cursor.close()
        conn.close()

        return {
            "success": True,
            "yesterday": yesterday_count,
            "total": total_count
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch stats: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# RTIS Report Data Endpoints
# ------------------------------
@app.get("/api/reports/kpi-stats")
async def get_kpi_stats(
    request: Request,
    from_date: str = Query(None),
    to_date: str = Query(None),
    designation: str = Query("ALL")
):
    """Get KPI statistics for report dashboard"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build date filter
        date_filter = ""
        params = []
        if from_date and to_date:
            date_filter = "AND a.analysis_date BETWEEN %s AND %s"
            params = [from_date, to_date]

        # Build designation filter
        # Default: Show only LPG, LPP, LPM (exclude ALP, Sr.ALP)
        # Show ALP/Sr.ALP only when explicitly selected
        designation_filter = ""
        designation_ids = []
        if designation != "ALL":
            # Map designation names to IDs (1=ALP, 2=Sr.ALP, 5=LPG, 6=LPP, 7=LPM)
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                designation_ids = [designation_map[designation]]
                designation_filter = "AND sm.designation_id = %s"
                params.append(designation_map[designation])
        else:
            # Default: only LPG, LPP, LPM (IDs: 5,6,7)
            designation_filter = "AND sm.designation_id IN (5,6,7)"

        # Total analyses
        query = f"""
            SELECT COUNT(DISTINCT a.id) as count
            FROM div_rtis_analyses a
            LEFT JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            WHERE 1=1 {date_filter} {designation_filter}
        """
        cursor.execute(query, params)
        total_result = cursor.fetchone()
        total_count = total_result['count'] if total_result else 0

        # This month analyses
        month_params = []
        if designation != "ALL" and designation_ids:
            month_params = [designation_ids[0]]
        month_query = f"""
            SELECT COUNT(DISTINCT a.id) as count
            FROM div_rtis_analyses a
            LEFT JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            WHERE MONTH(a.analysis_date) = MONTH(CURDATE())
              AND YEAR(a.analysis_date) = YEAR(CURDATE())
              {designation_filter}
        """
        cursor.execute(month_query, month_params)
        month_result = cursor.fetchone()
        month_count = month_result['count'] if month_result else 0

        # Not analyzed in last 3 months (90 days)
        # Default: Only LPG, LPP, LPM (5,6,7). Show ALP/Sr.ALP only when explicitly selected
        three_month_desig_filter = ""
        three_month_params = []
        if designation != "ALL" and designation_ids:
            # Specific designation selected
            three_month_desig_filter = "AND sm.designation_id = %s"
            three_month_params = [designation_ids[0]]
        else:
            # Default: only LPG, LPP, LPM
            three_month_desig_filter = "AND sm.designation_id IN (5,6,7)"

        three_month_query = f"""
            SELECT COUNT(DISTINCT sm.hrms_id) as count
            FROM div_staff_master sm
            LEFT JOIN (
                SELECT lp_hrms_id, MAX(analysis_date) as last_analysis
                FROM div_rtis_analyses
                GROUP BY lp_hrms_id
            ) a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            WHERE sm.current_office_code = 'CSMT-ML'
              AND sm.status = 'Active'
              AND (a.lp_hrms_id IS NULL OR DATEDIFF(CURDATE(), a.last_analysis) > 90)
              {three_month_desig_filter}
        """

        cursor.execute(three_month_query, three_month_params)
        three_month_result = cursor.fetchone()
        three_month_count = three_month_result['count'] if three_month_result else 0

        # Not analyzed in last 15 days
        # Default: Only LPG, LPP, LPM (5,6,7). Show ALP/Sr.ALP only when explicitly selected
        not_recent_desig_filter = ""
        not_recent_params = []
        if designation != "ALL" and designation_ids:
            # Specific designation selected
            not_recent_desig_filter = "AND sm.designation_id = %s"
            not_recent_params = [designation_ids[0]]
        else:
            # Default: only LPG, LPP, LPM
            not_recent_desig_filter = "AND sm.designation_id IN (5,6,7)"

        not_recent_query = f"""
            SELECT COUNT(DISTINCT sm.hrms_id) as count
            FROM div_staff_master sm
            INNER JOIN (
                SELECT lp_hrms_id, MAX(analysis_date) as last_analysis
                FROM div_rtis_analyses
                GROUP BY lp_hrms_id
            ) a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            WHERE sm.current_office_code = 'CSMT-ML'
              AND sm.status = 'Active'
              AND DATEDIFF(CURDATE(), a.last_analysis) > 15
              {not_recent_desig_filter}
        """

        cursor.execute(not_recent_query, not_recent_params)
        not_recent_result = cursor.fetchone()
        not_recent_count = not_recent_result['count'] if not_recent_result else 0

        # Distinct LP count - LPs who have been analyzed
        distinct_lp_query = f"""
            SELECT COUNT(DISTINCT a.lp_hrms_id) as count
            FROM div_rtis_analyses a
            LEFT JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            WHERE 1=1 {date_filter} {designation_filter}
        """
        cursor.execute(distinct_lp_query, params)
        distinct_lp_result = cursor.fetchone()
        distinct_lp_count = distinct_lp_result['count'] if distinct_lp_result else 0

        cursor.close()
        conn.close()

        return {
            "success": True,
            "total": total_count,
            "distinct_lps": distinct_lp_count,
            "this_month": month_count,
            "not_analyzed_3_months": 0,  # TODO: REVERT TO three_month_count after presentation
            "not_analyzed_15_days": not_recent_count
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch KPI stats: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/designation-breakdown")
async def get_designation_breakdown(
    request: Request,
    from_date: str = Query(None),
    to_date: str = Query(None),
    designation: str = Query("ALL")
):
    """Get analysis count breakdown by designation"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build date filter
        filters = []
        params = []
        if from_date and to_date:
            filters.append("a.analysis_date BETWEEN %s AND %s")
            params.extend([from_date, to_date])

        # Build designation filter.
        # Default: Show LPG/LPP/LPM unless a specific designation is selected.
        designation_filter = ""
        if designation != "ALL":
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                designation_filter = "sm.designation_id = %s"
                params.append(designation_map[designation])
        else:
            designation_filter = "sm.designation_id IN (5,6,7)"

        if designation_filter:
            filters.append(designation_filter)

        where_clause = "AND " + " AND ".join(filters) if filters else ""

        # Default: Only show LPG, LPP, LPM (exclude ALP, Sr.ALP)
        query = f"""
            SELECT
                d.designation_name,
                COUNT(DISTINCT a.id) as count
            FROM div_rtis_analyses a
            INNER JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            INNER JOIN designations d ON sm.designation_id = d.id
            WHERE sm.current_office_code = 'CSMT-ML'
              {where_clause}
            GROUP BY d.designation_name, sm.designation_id
            ORDER BY count DESC
        """

        cursor.execute(query, params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "data": results
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch designation breakdown: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/not-analyzed-3months")
async def get_not_analyzed_3months(
    request: Request,
    designation: str = Query("ALL")
):
    """Get list of LPs not analyzed in last 3 months (90 days)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build designation filter
        # Default: Show only LPG, LPP, LPM (exclude ALP, Sr.ALP)
        # Show ALP/Sr.ALP only when explicitly selected
        designation_filter = ""
        params = []
        if designation != "ALL":
            # Map designation names to IDs (1=ALP, 2=Sr.ALP, 5=LPG, 6=LPP, 7=LPM)
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                designation_filter = "AND sm.designation_id = %s"
                params.append(designation_map[designation])
        else:
            # Default: only LPG, LPP, LPM
            designation_filter = "AND sm.designation_id IN (5,6,7)"

        query = f"""
            SELECT
                sm.name as lp_name,
                sm.hrms_id,
                d.designation_name,
                a.last_analysis,
                CASE
                    WHEN a.last_analysis IS NULL THEN NULL
                    ELSE DATEDIFF(CURDATE(), a.last_analysis)
                END as days_since
            FROM div_staff_master sm
            INNER JOIN designations d ON sm.designation_id = d.id
            LEFT JOIN (
                SELECT lp_hrms_id, MAX(analysis_date) as last_analysis
                FROM div_rtis_analyses
                GROUP BY lp_hrms_id
            ) a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            WHERE sm.current_office_code = 'CSMT-ML'
              AND sm.status = 'Active'
              AND (a.lp_hrms_id IS NULL OR DATEDIFF(CURDATE(), a.last_analysis) > 90)
              {designation_filter}
            ORDER BY a.last_analysis IS NULL DESC, a.last_analysis ASC, sm.name
        """

        cursor.execute(query, params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "data": []  # TODO: REVERT TO results after presentation
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch not analyzed 3 months list: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/not-analyzed-15days")
async def get_not_analyzed_15days(
    request: Request,
    designation: str = Query("ALL")
):
    """Get list of LPs not analyzed in last 15 days"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build designation filter
        # Default: Show only LPG, LPP, LPM (exclude ALP, Sr.ALP)
        # Show ALP/Sr.ALP only when explicitly selected
        designation_filter = ""
        params = []
        if designation != "ALL":
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                designation_filter = "AND sm.designation_id = %s"
                params.append(designation_map[designation])
        else:
            # Default: only LPG, LPP, LPM
            designation_filter = "AND sm.designation_id IN (5,6,7)"

        query = f"""
            SELECT
                sm.hrms_id,
                sm.name as lp_name,
                a.last_analysis,
                DATEDIFF(CURDATE(), a.last_analysis) as days_since,
                a.last_train,
                a.last_analyst
            FROM div_staff_master sm
            INNER JOIN (
                SELECT
                    lp_hrms_id,
                    MAX(analysis_date) as last_analysis,
                    (SELECT train_number FROM div_rtis_analyses WHERE lp_hrms_id = a.lp_hrms_id ORDER BY analysis_date DESC LIMIT 1) as last_train,
                    (SELECT analyst_name FROM div_rtis_analyses WHERE lp_hrms_id = a.lp_hrms_id ORDER BY analysis_date DESC LIMIT 1) as last_analyst
                FROM div_rtis_analyses a
                GROUP BY lp_hrms_id
            ) a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            WHERE sm.current_office_code = 'CSMT-ML'
              AND sm.status = 'Active'
              AND DATEDIFF(CURDATE(), a.last_analysis) > 15
              {designation_filter}
            ORDER BY days_since DESC
        """

        cursor.execute(query, params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "data": results
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch not analyzed in 15 days: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/all-analyses")
async def get_all_analyses(
    request: Request,
    from_date: str = Query(None),
    to_date: str = Query(None),
    designation: str = Query("ALL"),
    search: str = Query(""),
    page: int = Query(1),
    limit: int = Query(50)
):
    """Get all analysis records with pagination and filters"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build filters
        filters = []
        params = []

        if from_date and to_date:
            filters.append("a.analysis_date BETWEEN %s AND %s")
            params.extend([from_date, to_date])

        # Designation filter
        # Default: Show only LPG, LPP, LPM (exclude ALP, Sr.ALP)
        # Show ALP/Sr.ALP only when explicitly selected
        if designation != "ALL":
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                filters.append("sm.designation_id = %s")
                params.append(designation_map[designation])
        else:
            # Default: only LPG, LPP, LPM
            filters.append("sm.designation_id IN (5,6,7)")

        if search:
            filters.append("(sm.name LIKE %s OR a.train_number LIKE %s)")
            search_term = f"%{search}%"
            params.extend([search_term, search_term])

        where_clause = "WHERE " + " AND ".join(filters) if filters else ""

        # Get total count
        count_query = f"""
            SELECT COUNT(*) as total
            FROM div_rtis_analyses a
            LEFT JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            {where_clause}
        """
        cursor.execute(count_query, params)
        total_result = cursor.fetchone()
        total = total_result['total'] if total_result else 0

        # Get paginated data
        offset = (page - 1) * limit
        data_query = f"""
            SELECT
                a.id,
                a.analysis_date,
                a.lp_hrms_id,
                sm.name as lp_name,
                a.train_number,
                CONCAT(a.from_station, '-', a.to_station) as route,
                a.analyst_name,
                a.pdf_filename
            FROM div_rtis_analyses a
            LEFT JOIN div_staff_master sm ON a.lp_hrms_id COLLATE utf8mb4_unicode_ci = sm.hrms_id COLLATE utf8mb4_unicode_ci
            {where_clause}
            ORDER BY a.analysis_date DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        cursor.execute(data_query, params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "data": results,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch analyses: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/lp-wise-summary")
async def get_lp_wise_summary(
    request: Request,
    from_date: str = Query(None),
    to_date: str = Query(None),
    designation: str = Query("ALL"),
    search: str = Query(""),
    page: int = Query(1),
    limit: int = Query(50)
):
    """Get LP-wise analysis summary with pagination"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build filters for the WHERE clause
        filters = ["sm.current_office_code = 'CSMT-ML'", "sm.status = 'Active'"]
        params = []

        # Designation filter
        # Default: Show only LPG, LPP, LPM (exclude ALP, Sr.ALP)
        if designation != "ALL":
            designation_map = {"ALP": 1, "SrALP": 2, "LPG": 5, "LPP": 6, "LPM": 7}
            if designation in designation_map:
                filters.append("sm.designation_id = %s")
                params.append(designation_map[designation])
        else:
            # Default: only LPG, LPP, LPM
            filters.append("sm.designation_id IN (5,6,7)")

        if search:
            filters.append("sm.name LIKE %s")
            params.append(f"%{search}%")

        where_clause = "WHERE " + " AND ".join(filters)

        # Build date filter for analysis subquery
        date_filter = ""
        date_params = []
        if from_date and to_date:
            date_filter = "AND analysis_date BETWEEN %s AND %s"
            date_params = [from_date, to_date]

        # Get total count of LPs
        count_query = f"""
            SELECT COUNT(DISTINCT sm.hrms_id) as total
            FROM div_staff_master sm
            INNER JOIN div_rtis_analyses a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            {where_clause}
        """
        cursor.execute(count_query, params)
        total_result = cursor.fetchone()
        total = total_result['total'] if total_result else 0

        # Get paginated LP data
        offset = (page - 1) * limit
        data_query = f"""
            SELECT
                sm.hrms_id,
                sm.name as lp_name,
                d.designation_name,
                COUNT(DISTINCT a.id) as total_analyses,
                MAX(a.analysis_date) as last_analysis_date,
                (SELECT train_number FROM div_rtis_analyses
                 WHERE lp_hrms_id = sm.hrms_id COLLATE utf8mb4_unicode_ci
                 ORDER BY analysis_date DESC LIMIT 1) as last_train
            FROM div_staff_master sm
            INNER JOIN designations d ON sm.designation_id = d.id
            INNER JOIN div_rtis_analyses a ON sm.hrms_id COLLATE utf8mb4_unicode_ci = a.lp_hrms_id COLLATE utf8mb4_unicode_ci
            {where_clause}
            {"" if not date_filter else "AND a." + date_filter[4:]}
            GROUP BY sm.hrms_id, sm.name, d.designation_name
            ORDER BY total_analyses DESC, sm.name ASC
            LIMIT %s OFFSET %s
        """
        all_params = params + date_params + [limit, offset]
        cursor.execute(data_query, all_params)
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "data": results,
            "total": total,
            "page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if total > 0 else 0
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch LP-wise summary: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/reports/lp-details/{hrms_id}")
async def get_lp_details(
    request: Request,
    hrms_id: str
):
    """Get detailed analysis history for a specific LP"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get LP info
        lp_query = """
            SELECT
                sm.hrms_id,
                sm.name as lp_name,
                d.designation_name,
                sm.current_office_code
            FROM div_staff_master sm
            INNER JOIN designations d ON sm.designation_id = d.id
            WHERE sm.hrms_id = %s
        """
        cursor.execute(lp_query, [hrms_id])
        lp_info = cursor.fetchone()

        if not lp_info:
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": "LP not found", "success": False},
                status_code=404
            )

        # Get all analyses for this LP
        analyses_query = """
            SELECT
                a.id,
                a.analysis_date,
                a.train_number,
                CONCAT(a.from_station, '-', a.to_station) as route,
                a.analyst_name,
                a.pdf_filename
            FROM div_rtis_analyses a
            WHERE a.lp_hrms_id COLLATE utf8mb4_unicode_ci = %s
            ORDER BY a.analysis_date DESC
        """
        cursor.execute(analyses_query, [hrms_id])
        analyses = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "lp_info": lp_info,
            "analyses": analyses,
            "total_analyses": len(analyses)
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch LP details: {str(e)}", "success": False},
            status_code=500
        )


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


# Zone boundary stations for braking pattern 1000m threshold
ZONE_BOUNDARY_STATIONS = {
    "main_line": ["KYN"],  # Kalyan boundary for main line
    "konkan": ["DTVL"],  # Dativali boundary for Konkan line
}

# Mumbai-side terminal stations
MUMBAI_TERMINALS = {"CSMT", "LTT", "DR", "CSTM"}

# Route end stations and their boundary type
ROUTE_BOUNDARY_MAP = {
    # Main line destinations (use TLA/BUD boundary)
    "PUNE": "main_line", "IGP": "main_line", "MMR": "main_line", "JL": "main_line",
    "KJT": "main_line", "LNL": "main_line", "PDI": "main_line",
    # Konkan destinations (use DTVL boundary)
    "ROHA": "konkan", "RN": "konkan", "CHI": "konkan",
}

# Ghat sections - steep gradient descent in UP direction
# Format: (start_station, end_station) - halts between these get 40 km/h at 1000m
GHAT_SECTIONS = [
    ("IGP", "KSRA"),  # Bhor Ghat section on IGP-Mumbai route
    ("LNL", "PDI"),   # Bhor Ghat section on Pune-Mumbai route
]


def _find_station_row_index(df: pl.DataFrame, station_code: str) -> int | None:
    """Find the first row index where train passes a specific station using geofence."""
    geos = _geofences_for_station(station_code, None)
    if not geos:
        return None

    lat_col = _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON")
    if not lat_col or not lon_col:
        return None

    lats = df[lat_col].cast(pl.Float64, strict=False).to_list()
    lons = df[lon_col].cast(pl.Float64, strict=False).to_list()

    for idx in range(len(lats)):
        lat, lon = lats[idx], lons[idx]
        if lat is None or lon is None:
            continue
        inside, _ = _within_geofence(lat, lon, geos)
        if inside:
            return idx
    return None


def _compute_ghat_section_halts(
    df: pl.DataFrame,
    halts: list[dict[str, Any]],
    direction: str | None,
) -> set[int]:
    """
    Determine which halt indices are in ghat sections (steep gradient).
    Only applies to UP direction (descending towards Mumbai).
    Returns a set of halt indices that are in ghat sections.
    """
    dir_norm = (direction or "").strip().upper()
    if dir_norm != "UP":
        return set()  # Ghat threshold only applies in UP direction

    ghat_halt_indices: set[int] = set()

    for start_station, end_station in GHAT_SECTIONS:
        start_row = _find_station_row_index(df, start_station)
        end_row = _find_station_row_index(df, end_station)

        if start_row is None or end_row is None:
            continue

        # Ensure start_row < end_row for range checking
        min_row, max_row = min(start_row, end_row), max(start_row, end_row)

        # Normalize start station for comparison
        start_station_norm = start_station.strip().upper()

        # Find halts that are AFTER the start station (descent begins after IGP/LNL)
        # Exclude the start station itself - ghat speed restriction applies during descent only
        for idx, halt in enumerate(halts):
            halt_row = halt.get("index", 0)
            halt_station = (halt.get("station") or "").strip().upper()

            # Exclude if halt is at the ghat start station (IGP/LNL)
            if halt_station == start_station_norm:
                continue

            if min_row < halt_row <= max_row:
                ghat_halt_indices.add(idx)

    return ghat_halt_indices


def _detect_violations(
    df: pl.DataFrame,
    halts: list[dict[str, Any]],
    criteria: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Detect speed violations from braking profile data.

    Returns list of violations with type, speed, threshold, and halt info.

    Violation types:
    - '1000m_zone_b': Zone B (mainline) violations at 1000m (>60 or >90 for 222xx)
    - '400m_zone_a': Zone A (suburban) violations at 400m (>30 or >40 for 222xx)
    - 'ghat': Ghat section violations (>40, UP direction only)
             Distance: 900m default, 800m for KAD and NNCN (T5 stations)
    """
    if not halts:
        return []

    violations = []

    from_station = (criteria.get("from_station_equals") or "").strip().upper()
    to_station = (criteria.get("to_station_equals") or "").strip().upper()
    direction = (criteria.get("direction_equals") or "").strip().upper()
    train_number = (criteria.get("train_number") or "").strip()

    # Compute boundary info
    boundary_type = ROUTE_BOUNDARY_MAP.get(to_station) or ROUTE_BOUNDARY_MAP.get(from_station)
    boundary_stations = ZONE_BOUNDARY_STATIONS.get(boundary_type, []) if boundary_type else []
    boundary_row_idx = _find_boundary_row_index(df, boundary_stations) if boundary_stations else None
    boundary_halt_idx = _find_boundary_halt_index(halts, boundary_row_idx)

    # Compute ghat section halts
    ghat_halt_indices = _compute_ghat_section_halts(df, halts, direction)

    # Zone B threshold: 90 for trains starting with "222", else 60
    zone_b_threshold = 90.0 if train_number.startswith("222") else 60.0

    # Zone A 400m threshold: 40 for trains starting with "222", else 30
    zone_a_400m_threshold = 40.0 if train_number.startswith("222") else 30.0

    # Ghat threshold: always 40
    ghat_threshold = 40.0

    # Determine if journey involves Mumbai terminals (only then Zone A applies)
    involves_mumbai = from_station in MUMBAI_TERMINALS or to_station in MUMBAI_TERMINALS
    starts_from_mumbai = from_station in MUMBAI_TERMINALS or direction == "DN"

    for idx, halt in enumerate(halts):
        halt_station = halt.get("station") or f"Halt {halt.get('sequence')}"
        speeds = halt.get("speeds") or {}

        # Determine zone for this halt - Zone A only applies for Mumbai-connected journeys
        in_zone_a = False  # Default to Zone B (no 400m threshold)
        if boundary_halt_idx is not None and involves_mumbai:
            if starts_from_mumbai:
                in_zone_a = idx <= boundary_halt_idx
            else:
                in_zone_a = idx > boundary_halt_idx

        in_ghat = idx in ghat_halt_indices

        # Check ghat violation first (takes precedence, only UP direction)
        if in_ghat:
            # Get ghat distance for this station: 800m for KAD/NNCN, 900m for others
            station_code = (halt.get("station") or "").strip().upper()
            ghat_distance = GHAT_STATION_DISTANCES.get(station_code, GHAT_DEFAULT_DISTANCE)
            reading_ghat = speeds.get(str(ghat_distance))
            if reading_ghat and isinstance(reading_ghat.get("speed"), (int, float)):
                speed_ghat = reading_ghat["speed"]
                if speed_ghat >= ghat_threshold + 1:
                    violations.append({
                        "violation_type": "ghat",
                        "speed": speed_ghat,
                        "threshold": ghat_threshold,
                        "halt_station": halt_station,
                        "zone": "ghat",
                        "halt_time": halt.get("logging_time"),
                        "distance": ghat_distance,  # Include distance for reference
                    })

        # Check 1000m speed for Zone B (only if not in ghat)
        reading_1000 = speeds.get("1000")
        if reading_1000 and isinstance(reading_1000.get("speed"), (int, float)):
            speed_1000 = reading_1000["speed"]

            # Zone B violation (only if not in ghat and in zone B)
            if not in_zone_a and not in_ghat and speed_1000 >= zone_b_threshold + 1:
                violations.append({
                    "violation_type": "1000m_zone_b",
                    "speed": speed_1000,
                    "threshold": zone_b_threshold,
                    "halt_station": halt_station,
                    "zone": "mainline",
                    "halt_time": halt.get("logging_time"),
                })

        # Check 400m speed (Zone A only) - uses speed at 300m
        reading_400 = speeds.get("300")
        if reading_400 and isinstance(reading_400.get("speed"), (int, float)):
            speed_400 = reading_400["speed"]

            if in_zone_a and speed_400 >= zone_a_400m_threshold + 1:
                violations.append({
                    "violation_type": "400m_zone_a",
                    "speed": speed_400,
                    "threshold": zone_a_400m_threshold,
                    "halt_station": halt_station,
                    "zone": "suburban",
                    "halt_time": halt.get("logging_time"),
                })

    return violations


def _find_boundary_row_index(df: pl.DataFrame, boundary_stations: list[str]) -> int | None:
    """
    Find the first row index where train passes a boundary station.
    Uses geofence coordinates first, falls back to station code column.
    """
    if not boundary_stations:
        return None

    # Collect geofences for all boundary stations
    all_geos: list[dict[str, Any]] = []
    for station in boundary_stations:
        geos = _geofences_for_station(station, None)
        all_geos.extend(geos)

    # Try GPS-based detection first
    lat_col = _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON")
    if all_geos and lat_col and lon_col:
        lats = df[lat_col].cast(pl.Float64, strict=False).to_list()
        lons = df[lon_col].cast(pl.Float64, strict=False).to_list()
        for idx in range(len(lats)):
            lat, lon = lats[idx], lons[idx]
            if lat is None or lon is None:
                continue
            inside, _ = _within_geofence(lat, lon, all_geos)
            if inside:
                return idx

    # Fallback: station code column
    station_col = _first_matching_column(df.columns, "STATION", "CODE") or _first_matching_column(df.columns, "STATION")
    if station_col:
        boundary_set = {s.upper() for s in boundary_stations}
        stations = df[station_col].to_list()
        for idx, station in enumerate(stations):
            if station and str(station).strip().upper() in boundary_set:
                return idx

    return None


def _find_boundary_halt_index(halts: list[dict[str, Any]], boundary_row_idx: int | None) -> int | None:
    """
    Find the halt index corresponding to the boundary row.
    Returns the last halt before or at the boundary (Zone A side).
    """
    if boundary_row_idx is None:
        return None
    last_before = None
    for idx, halt in enumerate(halts):
        halt_row_idx = halt.get("index", 0)
        if halt_row_idx >= boundary_row_idx:
            return last_before if last_before is not None else 0
        last_before = idx
    # All halts are before boundary
    return last_before


def _get_1000m_threshold(
    halt_idx: int,
    boundary_idx: int | None,
    from_station: str | None,
    to_station: str | None,
    direction: str | None,
    train_number: str | None = None,
    in_ghat_section: bool = False,
) -> float:
    """
    Determine 1000m speed threshold based on halt's zone.

    Zone A (Mumbai suburban side): threshold = 70 km/h
    Zone B (mainline/Konkan side): threshold = 60 km/h (stricter)
        - Exception: trains starting with "222" use 90 km/h for Zone B
        - Exception: ghat sections in UP direction use 40 km/h

    For DN direction: halts before boundary = Zone A (70), after = Zone B (60/90)
    For UP direction: halts before boundary = Zone B (60/90 or 40 for ghat), after = Zone A (70)
    """
    # Ghat section override: 40 km/h for steep gradient descent (UP direction only)
    if in_ghat_section:
        return 40.0

    if boundary_idx is None:
        return 70.0  # Default if no boundary found

    from_norm = (from_station or "").strip().upper()
    dir_norm = (direction or "").strip().upper()
    train_num = (train_number or "").strip()

    # Zone B threshold: 90 for trains starting with "222", else 60
    zone_b_threshold = 90.0 if train_num.startswith("222") else 60.0

    # Determine if journey starts from Mumbai side
    starts_from_mumbai = from_norm in MUMBAI_TERMINALS or dir_norm == "DN"

    if starts_from_mumbai:
        # DN: Mumbai → destination
        # Before boundary = Zone A (suburban) = 70
        # After boundary = Zone B (mainline) = 60 or 90
        return 70.0 if halt_idx <= boundary_idx else zone_b_threshold
    else:
        # UP: destination → Mumbai
        # Before boundary = Zone B (mainline) = 60 or 90
        # After boundary = Zone A (suburban) = 70
        return zone_b_threshold if halt_idx <= boundary_idx else 70.0


def _get_400m_threshold(
    halt_idx: int,
    boundary_idx: int | None,
    from_station: str | None,
    direction: str | None,
    train_number: str | None = None,
) -> float | None:
    """
    Determine 400m speed threshold based on halt's zone.

    Only Zone A (Mumbai suburban side) has a 400m threshold:
        - Trains starting with "222": 40 km/h
        - Other trains: 30 km/h

    Zone B (mainline/Konkan): No 400m threshold (returns None)

    For DN direction: halts before boundary = Zone A (threshold), after = Zone B (None)
    For UP direction: halts before boundary = Zone B (None), after = Zone A (threshold)
    """
    if boundary_idx is None:
        return None  # Default: no 400m threshold if no boundary found

    from_norm = (from_station or "").strip().upper()
    dir_norm = (direction or "").strip().upper()
    train_num = (train_number or "").strip()

    # Zone A threshold: 40 for trains starting with "222", else 30
    zone_a_threshold = 40.0 if train_num.startswith("222") else 30.0

    # Determine if journey starts from Mumbai side
    starts_from_mumbai = from_norm in MUMBAI_TERMINALS or dir_norm == "DN"

    if starts_from_mumbai:
        # DN: Mumbai → destination
        # Before boundary = Zone A (suburban) = threshold
        # After boundary = Zone B (mainline) = None
        return zone_a_threshold if halt_idx <= boundary_idx else None
    else:
        # UP: destination → Mumbai
        # Before boundary = Zone B (mainline) = None
        # After boundary = Zone A (suburban) = threshold
        return None if halt_idx <= boundary_idx else zone_a_threshold


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
        sharp_brake = _detect_sharp_brake_point(
            speeds,
            dist_to_halt,
            times,
            halt_idx,
        )

        halt_entry = {
            "sequence": seq,
            "index": halt_idx,
            "station": display_station,
            "logging_time": times[halt_idx],
            "distance_m": halt_dist,
            "speeds": {},
            "sharp_brake": sharp_brake,
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


def _halt_deceleration_metrics(
    halts: list[dict[str, Any]],
    start_station: str | None = None,
    end_station: str | None = None,
) -> list[dict[str, Any]]:
    """
    Prepare deceleration metrics table for the requested halt range.

    Returns list of dicts with:
    - sequence, station, logging_time
    - speed_1000_kmph / speed_400_kmph (approach speeds)
    - decel_full_1000_mps2 / decel_full_400_mps2 (uniform slowdown to 0)
    - decel_1000_to_400_mps2 (actual rate between the two checkpoints)
    """

    def _norm_station(value: str | None) -> str:
        return (value or "").strip().upper()

    def _actual_distance(target: float, sample: dict[str, Any] | None) -> float | None:
        if not sample:
            return None
        delta = sample.get("delta_m")
        try:
            actual = float(target) - float(delta or 0.0)
        except (TypeError, ValueError):
            return None
        return actual if actual > 0 else None

    def _uniform_decel(speed_kmph: float | None, distance_m: float | None) -> float | None:
        if speed_kmph is None or distance_m is None or distance_m <= 0:
            return None
        speed_mps = float(speed_kmph) * (1000.0 / 3600.0)
        return (speed_mps ** 2) / (2.0 * distance_m)

    def _segment_decel(
        upper_sample: dict[str, Any] | None,
        lower_sample: dict[str, Any] | None,
        upper_target: float,
        lower_target: float,
    ) -> float | None:
        if not upper_sample or not lower_sample:
            return None
        dist_upper = _actual_distance(upper_target, upper_sample)
        dist_lower = _actual_distance(lower_target, lower_sample)
        if dist_upper is None or dist_lower is None:
            return None
        delta_d = dist_upper - dist_lower
        if delta_d <= 0:
            return None
        v_upper = upper_sample.get("speed")
        v_lower = lower_sample.get("speed")
        if v_upper is None or v_lower is None:
            return None
        try:
            v_upper_mps = float(v_upper) * (1000.0 / 3600.0)
            v_lower_mps = float(v_lower) * (1000.0 / 3600.0)
        except (TypeError, ValueError):
            return None
        return (v_upper_mps ** 2 - v_lower_mps ** 2) / (2.0 * delta_d)

    start_norm = _norm_station(start_station) if start_station else None
    end_norm = _norm_station(end_station) if end_station else None
    inside = start_norm is None
    start_found = False
    filtered: list[dict[str, Any]] = []
    for halt in halts:
        station = _norm_station(halt.get("station"))
        if not inside and start_norm and station == start_norm:
            inside = True
            start_found = True
        if inside:
            filtered.append(halt)
        if inside and end_norm and station == end_norm:
            break
    if start_norm and not start_found and not filtered:
        # Dataset may already be trimmed to the desired range; include all halts.
        filtered = list(halts)

    results: list[dict[str, Any]] = []
    for halt in filtered:
        speeds = halt.get("speeds", {})
        sample_1000 = speeds.get("1000")
        sample_400 = speeds.get("400")
        sample_250 = speeds.get("250")
        dist_1000 = _actual_distance(1000.0, sample_1000)
        dist_400 = _actual_distance(400.0, sample_400)
        dist_250 = _actual_distance(250.0, sample_250)

        results.append({
            "sequence": halt.get("sequence"),
            "station": halt.get("station"),
            "halt_time": halt.get("logging_time"),
            "speed_1000_kmph": sample_1000.get("speed") if sample_1000 else None,
            "speed_400_kmph": sample_400.get("speed") if sample_400 else None,
            "speed_250_kmph": sample_250.get("speed") if sample_250 else None,
            "decel_full_1000_mps2": _uniform_decel(
                sample_1000.get("speed") if sample_1000 else None,
                dist_1000,
            ),
            "decel_full_400_mps2": _uniform_decel(
                sample_400.get("speed") if sample_400 else None,
                dist_400,
            ),
            "decel_full_250_mps2": _uniform_decel(
                sample_250.get("speed") if sample_250 else None,
                dist_250,
            ),
            "decel_1000_to_400_mps2": _segment_decel(
                sample_1000,
                sample_400,
                1000.0,
                400.0,
            ),
            "decel_400_to_250_mps2": _segment_decel(
                sample_400,
                sample_250,
                400.0,
                250.0,
            ),
            "decel_1000_to_250_mps2": _segment_decel(
                sample_1000,
                sample_250,
                1000.0,
                250.0,
            ),
            "sharp_brake_speed_kmph": halt.get("sharp_brake", {}).get("speed_kmph") if halt.get("sharp_brake") else None,
            "sharp_brake_distance_m": halt.get("sharp_brake", {}).get("distance_m") if halt.get("sharp_brake") else None,
            "sharp_brake_time": halt.get("sharp_brake", {}).get("time") if halt.get("sharp_brake") else None,
            "decel_sharp_mps2": _uniform_decel(
                halt.get("sharp_brake", {}).get("speed_kmph") if halt.get("sharp_brake") else None,
                halt.get("sharp_brake", {}).get("distance_m") if halt.get("sharp_brake") else None,
            ),
        })

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
# Daily Summary APIs (SIM Down / NON RTIS)
# ------------------------------

@app.get("/api/daily-entries")
async def get_daily_entries(
    request: Request,
    date: str = Query(None),
    status: str = Query(None)
):
    """Get manual daily entries (SIM Down / NON RTIS) for a date"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        where_clauses = []
        params = []

        if date:
            where_clauses.append("working_date = %s")
            params.append(date)
        if status:
            where_clauses.append("rtis_status = %s")
            params.append(status)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        cursor.execute(f"""
            SELECT * FROM div_rtis_daily_entries
            WHERE {where_sql}
            ORDER BY working_date DESC, train_number
        """, params)
        entries = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"success": True, "entries": entries}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch entries: {str(e)}", "success": False},
            status_code=500
        )


@app.post("/api/daily-entry")
async def add_daily_entry(request: Request):
    """Add a SIM Down or NON RTIS entry"""
    try:
        user = get_current_user(request)
        data = await request.json()

        working_date = data.get('working_date')
        rtis_status = data.get('rtis_status')
        train_number = data.get('train_number', '').strip()
        loco_number = data.get('loco_number', '').strip()

        if not working_date or not rtis_status or not train_number or not loco_number:
            return JSONResponse(
                {"error": "working_date, rtis_status, train_number, and loco_number are required", "success": False},
                status_code=400
            )

        if rtis_status not in ['SIM Down', 'Partial Data', 'NON RTIS']:
            return JSONResponse(
                {"error": "rtis_status must be 'SIM Down', 'Partial Data' or 'NON RTIS'", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO div_rtis_daily_entries (
                working_date, rtis_status, train_number, loco_number,
                from_station, to_station, departure_time, arrival_time,
                lp_name, lp_hrms_id, ncli_name, alp_name, alp_hrms_id, ncli_alp_name, entered_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            working_date,
            rtis_status,
            train_number,
            loco_number,
            data.get('from_station', '').strip() or None,
            data.get('to_station', '').strip() or None,
            data.get('departure_time') or None,
            data.get('arrival_time') or None,
            data.get('lp_name', '').strip() or None,
            data.get('lp_hrms_id', '').strip() or None,
            data.get('ncli_name', '').strip() or None,
            data.get('alp_name', '').strip() or None,
            data.get('alp_hrms_id', '').strip() or None,
            data.get('ncli_alp_name', '').strip() or None,
            user.get('name') if user else data.get('entered_by', '').strip() or None
        ))
        conn.commit()
        entry_id = cursor.lastrowid

        cursor.close()
        conn.close()

        return {"success": True, "message": "Entry added successfully", "id": entry_id}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to add entry: {str(e)}", "success": False},
            status_code=500
        )


@app.post("/api/daily-entries/bulk")
async def add_daily_entries_bulk(request: Request):
    """Add multiple daily entries in bulk"""
    try:
        user = get_current_user(request)
        data = await request.json()
        entries = data.get('entries', [])

        if not entries:
            return JSONResponse(
                {"error": "No entries provided", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor()

        added_count = 0
        errors = []

        for idx, entry in enumerate(entries):
            working_date = entry.get('working_date')
            rtis_status = entry.get('rtis_status')
            train_number = entry.get('train_number', '').strip()
            loco_number = entry.get('loco_number', '').strip()

            if not working_date or not rtis_status or not train_number or not loco_number:
                errors.append(f"Entry {idx + 1}: Missing required fields")
                continue

            if rtis_status not in ['SIM Down', 'Partial Data', 'NON RTIS']:
                errors.append(f"Entry {idx + 1}: Invalid status '{rtis_status}'")
                continue

            try:
                cursor.execute("""
                    INSERT INTO div_rtis_daily_entries (
                        working_date, rtis_status, train_number, loco_number,
                        from_station, to_station, departure_time, arrival_time,
                        lp_name, lp_hrms_id, ncli_name, alp_name, alp_hrms_id, ncli_alp_name, entered_by
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    working_date,
                    rtis_status,
                    train_number,
                    loco_number,
                    entry.get('from_station', '').strip() or None,
                    entry.get('to_station', '').strip() or None,
                    entry.get('departure_time') or None,
                    entry.get('arrival_time') or None,
                    entry.get('lp_name', '').strip() or None,
                    entry.get('lp_hrms_id', '').strip() or None,
                    entry.get('ncli_name', '').strip() or None,
                    entry.get('alp_name', '').strip() or None,
                    entry.get('alp_hrms_id', '').strip() or None,
                    entry.get('ncli_alp_name', '').strip() or None,
                    user.get('name') if user else entry.get('entered_by', '').strip() or None
                ))
                added_count += 1
            except Exception as insert_err:
                errors.append(f"Entry {idx + 1}: {str(insert_err)}")

        conn.commit()
        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": f"Added {added_count} entries successfully",
            "added_count": added_count,
            "errors": errors if errors else None
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to add entries: {str(e)}", "success": False},
            status_code=500
        )


@app.put("/api/daily-entry/{entry_id}")
async def update_daily_entry(request: Request, entry_id: int):
    """Update a daily entry"""
    try:
        data = await request.json()

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check if entry exists
        cursor.execute("SELECT id FROM div_rtis_daily_entries WHERE id = %s", [entry_id])
        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": "Entry not found", "success": False},
                status_code=404
            )

        # Build update
        updates = []
        params = []
        fields = ['working_date', 'rtis_status', 'train_number', 'loco_number',
                  'from_station', 'to_station', 'departure_time', 'arrival_time',
                  'lp_name', 'lp_hrms_id', 'ncli_name', 'alp_name', 'alp_hrms_id', 'ncli_alp_name']

        for field in fields:
            if field in data:
                updates.append(f"{field} = %s")
                val = data[field]
                if isinstance(val, str):
                    val = val.strip() or None
                params.append(val)

        if updates:
            params.append(entry_id)
            cursor.execute(f"""
                UPDATE div_rtis_daily_entries SET {', '.join(updates)} WHERE id = %s
            """, params)
            conn.commit()

        cursor.close()
        conn.close()

        return {"success": True, "message": "Entry updated successfully"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to update entry: {str(e)}", "success": False},
            status_code=500
        )


@app.delete("/api/daily-entry/{entry_id}")
async def delete_daily_entry(request: Request, entry_id: int):
    """Delete a daily entry"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM div_rtis_daily_entries WHERE id = %s", [entry_id])
        conn.commit()

        if cursor.rowcount == 0:
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": "Entry not found", "success": False},
                status_code=404
            )

        cursor.close()
        conn.close()

        return {"success": True, "message": "Entry deleted successfully"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to delete entry: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/daily-summary")
async def get_daily_summary(request: Request, date: str = Query(...)):
    """Get combined daily summary (Working + SIM Down + NON RTIS)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        entries = []

        # 1. Get Working entries from div_rtis_analyses (by analysis_date)
        cursor.execute("""
            SELECT
                'Working' as rtis_status,
                working_date,
                train_number,
                loco_number,
                from_station,
                to_station,
                lp_name,
                lp_hrms_id,
                alp_name,
                alp_hrms_id,
                ncli_name,
                analyst_name as analyzed_by,
                bft_status,
                bpt_status,
                'analysis' as source,
                id as source_id
            FROM div_rtis_analyses
            WHERE DATE(analysis_date) = %s
            ORDER BY train_number
        """, [date])
        working_entries = cursor.fetchall()
        entries.extend(working_entries)

        # 2. Get SIM Down / NON RTIS entries (by created_at date - when entry was made)
        cursor.execute("""
            SELECT
                rtis_status,
                working_date,
                train_number,
                loco_number,
                from_station,
                to_station,
                lp_name,
                lp_hrms_id,
                ncli_name,
                alp_name,
                alp_hrms_id,
                ncli_alp_name,
                '------' as analyzed_by,
                'NOT RUN' as bft_status,
                'NOT RUN' as bpt_status,
                'manual' as source,
                id as source_id
            FROM div_rtis_daily_entries
            WHERE DATE(created_at) = %s
            ORDER BY train_number
        """, [date])
        manual_entries = cursor.fetchall()
        entries.extend(manual_entries)

        # Sort all entries by train number
        entries.sort(key=lambda x: x.get('train_number', '') or '')

        # Add serial numbers
        for i, entry in enumerate(entries, 1):
            entry['sr_no'] = i

        # Summary counts
        working_count = len([e for e in entries if e['rtis_status'] == 'Working'])
        sim_down_count = len([e for e in entries if e['rtis_status'] == 'SIM Down'])
        partial_data_count = len([e for e in entries if e['rtis_status'] == 'Partial Data'])
        non_rtis_count = len([e for e in entries if e['rtis_status'] == 'NON RTIS'])

        cursor.close()
        conn.close()

        return {
            "success": True,
            "date": date,
            "summary": {
                "total": len(entries),
                "working": working_count,
                "sim_down": sim_down_count,
                "partial_data": partial_data_count,
                "non_rtis": non_rtis_count
            },
            "entries": entries
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch daily summary: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/analysis-totals")
async def get_analysis_totals(request: Request, date: str = Query(...)):
    """Get monthly and fiscal year totals for RTIS data analyzed"""
    try:
        from datetime import datetime
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Parse the input date
        target_date = datetime.strptime(date, '%Y-%m-%d')
        year = target_date.year
        month = target_date.month

        # Calculate fiscal year (April 1 - March 31)
        # If month is Jan-Mar, fiscal year started previous year
        if month >= 4:
            fy_start = f"{year}-04-01"
            fy_end = f"{year + 1}-03-31"
            fy_label = f"FY {str(year)[2:]}-{str(year + 1)[2:]}"
        else:
            fy_start = f"{year - 1}-04-01"
            fy_end = f"{year}-03-31"
            fy_label = f"FY {str(year - 1)[2:]}-{str(year)[2:]}"

        # Monthly total (working count from div_rtis_analyses)
        month_start = f"{year}-{month:02d}-01"
        if month == 12:
            month_end = f"{year + 1}-01-01"
        else:
            month_end = f"{year}-{month + 1:02d}-01"

        cursor.execute("""
            SELECT COUNT(*) as count
            FROM div_rtis_analyses
            WHERE DATE(analysis_date) >= %s AND DATE(analysis_date) < %s
        """, [month_start, month_end])
        monthly_result = cursor.fetchone()
        monthly_total = monthly_result['count'] if monthly_result else 0

        # Fiscal year total
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM div_rtis_analyses
            WHERE DATE(analysis_date) >= %s AND DATE(analysis_date) <= %s
        """, [fy_start, fy_end])
        fy_result = cursor.fetchone()
        fy_total = fy_result['count'] if fy_result else 0

        cursor.close()
        conn.close()

        return {
            "success": True,
            "date": date,
            "monthly_total": monthly_total,
            "monthly_label": target_date.strftime("%B %Y"),
            "fy_total": fy_total,
            "fy_label": fy_label
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch analysis totals: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/export-daily-summary")
async def export_daily_summary(request: Request, date: str = Query(...)):
    """Export daily summary as CSV"""
    try:
        # Get the summary data
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        entries = []

        # Get Working entries (by analysis_date)
        cursor.execute("""
            SELECT
                'Working' as rtis_status,
                working_date,
                train_number,
                loco_number,
                from_station,
                to_station,
                lp_name,
                lp_hrms_id,
                ncli_name,
                alp_name,
                alp_hrms_id,
                ncli_alp_name,
                analyst_name as analyzed_by,
                bft_status,
                bpt_status
            FROM div_rtis_analyses
            WHERE DATE(analysis_date) = %s
        """, [date])
        entries.extend(cursor.fetchall())

        # Get manual entries (by created_at date)
        cursor.execute("""
            SELECT
                rtis_status,
                working_date,
                train_number,
                loco_number,
                from_station,
                to_station,
                lp_name,
                lp_hrms_id,
                ncli_name,
                alp_name,
                alp_hrms_id,
                ncli_alp_name,
                '------' as analyzed_by,
                'NOT RUN' as bft_status,
                'NOT RUN' as bpt_status
            FROM div_rtis_daily_entries
            WHERE DATE(created_at) = %s
        """, [date])
        entries.extend(cursor.fetchall())

        cursor.close()
        conn.close()

        # Sort by NCLI (CLI name) so same CLI entries are grouped together, then by train number
        entries.sort(key=lambda x: (x.get('ncli_name') or '', x.get('train_number') or ''))

        # Build CSV
        import io
        output = io.StringIO()
        output.write("SR NO.,Date Of Working,RTIS Status,Train Number,Loco Number,From,To,LP NAME,ALP,NCLI,Analyzed By,BFT Done,BPT Done\n")

        for i, e in enumerate(entries, 1):
            working_date = e.get('working_date')
            if working_date:
                if hasattr(working_date, 'strftime'):
                    working_date = working_date.strftime('%d/%m/%Y')
                else:
                    working_date = str(working_date)

            bft_done = 'Done' if e.get('bft_status') == 'PASS' else ''
            bpt_done = 'Done' if e.get('bpt_status') == 'PASS' else ''

            row = [
                str(i),
                working_date or '',
                e.get('rtis_status', ''),
                e.get('train_number', ''),
                e.get('loco_number', ''),
                e.get('from_station', '') or '',
                e.get('to_station', '') or '',
                e.get('lp_name', '') or '',
                e.get('alp_name', '') or '',
                e.get('ncli_name', '') or '',
                e.get('analyzed_by', '') or '',
                bft_done,
                bpt_done
            ]
            output.write(','.join([f'"{v}"' if ',' in str(v) else str(v) for v in row]) + '\n')

        csv_content = output.getvalue()
        output.close()

        # Format filename
        filename = f"daily_summary_{date}.csv"

        return StreamingResponse(
            io.BytesIO(csv_content.encode('utf-8')),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to export: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/sim-down-weekly")
async def get_sim_down_weekly(
    request: Request,
    week_start: str = Query(...),
    week_end: str = Query(...)
):
    """Get weekly SIM down and NON RTIS report grouped by loco with daily status"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get all entries for the week (SIM Down and NON RTIS) - CR locos only
        cursor.execute("""
            SELECT
                e.loco_number,
                e.working_date,
                e.rtis_status,
                COALESCE(l.current_shed, '') as base_shed,
                COALESCE(l.loco_type, '') as loco_type
            FROM div_rtis_daily_entries e
            INNER JOIN div_cr_locos l ON e.loco_number COLLATE utf8mb4_unicode_ci = l.loco_number COLLATE utf8mb4_unicode_ci
            WHERE e.rtis_status IN ('SIM Down', 'NON RTIS')
              AND e.working_date BETWEEN %s AND %s
            ORDER BY l.current_shed, e.loco_number, e.working_date
        """, [week_start, week_end])
        entries = cursor.fetchall()

        # Generate list of dates in the week
        from datetime import datetime, timedelta
        start = datetime.strptime(week_start, '%Y-%m-%d')
        end = datetime.strptime(week_end, '%Y-%m-%d')
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)

        # Group by loco and create daily status map
        loco_map = {}
        for entry in entries:
            loco_num = entry['loco_number']
            if loco_num not in loco_map:
                loco_map[loco_num] = {
                    'loco_number': loco_num,
                    'base_shed': entry['base_shed'],
                    'loco_type': entry['loco_type'],
                    'rly': 'CR',
                    'daily_status': {d: '' for d in dates},
                    'sim_down_count': 0,
                    'non_rtis_count': 0
                }

            date_str = entry['working_date'].strftime('%Y-%m-%d') if hasattr(entry['working_date'], 'strftime') else str(entry['working_date'])
            status = entry['rtis_status']
            loco_map[loco_num]['daily_status'][date_str] = status

            if status == 'SIM Down':
                loco_map[loco_num]['sim_down_count'] += 1
            elif status == 'NON RTIS':
                loco_map[loco_num]['non_rtis_count'] += 1

        # Get analyzed locos from div_rtis_analyses to fill blank cells with "OK"
        if loco_map:
            loco_list = list(loco_map.keys())
            placeholders = ','.join(['%s'] * len(loco_list))
            cursor.execute(f"""
                SELECT DISTINCT loco_number, DATE(working_date) as working_date
                FROM div_rtis_analyses
                WHERE loco_number IN ({placeholders})
                  AND working_date BETWEEN %s AND %s
            """, loco_list + [week_start, week_end])
            analyzed_entries = cursor.fetchall()

            # Mark analyzed days as "OK" where currently blank
            for entry in analyzed_entries:
                loco_num = entry['loco_number']
                date_str = entry['working_date'].strftime('%Y-%m-%d') if hasattr(entry['working_date'], 'strftime') else str(entry['working_date'])
                if loco_num in loco_map and loco_map[loco_num]['daily_status'].get(date_str) == '':
                    loco_map[loco_num]['daily_status'][date_str] = 'OK'

        # Convert to list and sort by shed, then loco number
        locos = list(loco_map.values())
        locos.sort(key=lambda x: (x['base_shed'], x['loco_number']))

        cursor.close()
        conn.close()

        return {
            "success": True,
            "week_start": week_start,
            "week_end": week_end,
            "dates": dates,
            "locos": locos,
            "total_locos": len(locos)
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch weekly report: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# Monthly Station Report API
# ------------------------------

@app.get("/api/monthly-station-report")
async def get_monthly_station_report(
    request: Request,
    station: str = Query(...),
    month: int = Query(...),
    year: int = Query(...)
):
    """Get monthly station report with train counts and violations"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Primary stations list
        primary_stations = ['CSMT', 'LTT', 'DR', 'KYN', 'DIVA', 'PNVL', 'BSR', 'PUNE', 'IGP', 'MMR', 'JL', 'ROHA', 'RN']

        # Build station filter
        if station == 'OTHERS':
            placeholders = ','.join(['%s'] * len(primary_stations))
            station_filter = f"a.from_station NOT IN ({placeholders})"
            station_params = primary_stations
        else:
            station_filter = "a.from_station = %s"
            station_params = [station]

        # Base date filter
        date_filter = "MONTH(a.working_date) = %s AND YEAR(a.working_date) = %s"
        base_params = station_params + [month, year]

        # Get all trains from this station
        cursor.execute(f"""
            SELECT
                a.id,
                a.train_number,
                a.working_date,
                a.loco_number,
                a.from_station,
                a.to_station
            FROM div_rtis_analyses a
            WHERE {station_filter}
              AND {date_filter}
            ORDER BY a.working_date, a.train_number
        """, base_params)
        all_trains = cursor.fetchall()

        # Get violation counts per analysis
        cursor.execute(f"""
            SELECT
                a.id,
                COUNT(v.id) as violation_count
            FROM div_rtis_analyses a
            LEFT JOIN div_rtis_violations v ON v.analysis_id = a.id
                AND v.violation_type = 'ghat'
                AND v.halt_station IN ('KAD', 'MHLC', 'TKW', 'T5')
            WHERE {station_filter}
              AND {date_filter}
            GROUP BY a.id
        """, base_params)
        violation_map = {row['id']: row['violation_count'] for row in cursor.fetchall()}

        # For PUNE: Get line type (T5 or TKW)
        line_type_map = {}
        if station == 'PUNE':
            # Get T5 trains (UP Line)
            cursor.execute(f"""
                SELECT DISTINCT a.id
                FROM div_rtis_analyses a
                JOIN div_rtis_braking_runs r ON r.analysis_id = a.id
                JOIN div_rtis_braking_halts h ON h.run_id = r.run_id
                WHERE {station_filter}
                  AND {date_filter}
                  AND h.halt_code = 'T5'
            """, base_params)
            for row in cursor.fetchall():
                line_type_map[row['id']] = 'UP'

            # Get TKW trains (Middle Line)
            cursor.execute(f"""
                SELECT DISTINCT a.id
                FROM div_rtis_analyses a
                JOIN div_rtis_braking_runs r ON r.analysis_id = a.id
                JOIN div_rtis_braking_halts h ON h.run_id = r.run_id
                WHERE {station_filter}
                  AND {date_filter}
                  AND h.halt_code = 'TKW'
            """, base_params)
            for row in cursor.fetchall():
                if row['id'] not in line_type_map:  # Don't overwrite if already has T5
                    line_type_map[row['id']] = 'MIDDLE'

        # Build train lists with violation counts and line types
        trains_all = []
        trains_up = []
        trains_middle = []
        trains_violations = []
        trains_nodata = []

        for train in all_trains:
            train_id = train['id']
            train_data = {
                'train_number': train['train_number'],
                'working_date': train['working_date'].strftime('%Y-%m-%d') if hasattr(train['working_date'], 'strftime') else str(train['working_date']),
                'loco_number': train['loco_number'],
                'from_station': train['from_station'],
                'to_station': train['to_station'],
                'violation_count': violation_map.get(train_id, 0),
                'line_type': line_type_map.get(train_id, '') if station == 'PUNE' else ''
            }
            trains_all.append(train_data)

            if train_data['violation_count'] > 0:
                trains_violations.append(train_data)

            if station == 'PUNE':
                line_type = line_type_map.get(train_id, '')
                if line_type == 'UP':
                    trains_up.append(train_data)
                elif line_type == 'MIDDLE':
                    trains_middle.append(train_data)
                else:
                    trains_nodata.append(train_data)

        # Calculate summary statistics
        total_trains = len(trains_all)
        total_violations = len(trains_violations)

        summary = {
            'total_trains': total_trains,
            'total_violations': total_violations
        }

        if station == 'PUNE':
            # Get UP line violations count
            cursor.execute(f"""
                SELECT COUNT(DISTINCT a.id) as count
                FROM div_rtis_analyses a
                JOIN div_rtis_braking_runs r ON r.analysis_id = a.id
                JOIN div_rtis_braking_halts h ON h.run_id = r.run_id
                JOIN div_rtis_violations v ON v.analysis_id = a.id
                WHERE {station_filter}
                  AND {date_filter}
                  AND h.halt_code = 'T5'
                  AND v.violation_type = 'ghat'
                  AND v.halt_station IN ('KAD', 'MHLC', 'TKW', 'T5')
            """, base_params)
            up_violations = cursor.fetchone()['count']

            # Get Middle line violations count
            cursor.execute(f"""
                SELECT COUNT(DISTINCT a.id) as count
                FROM div_rtis_analyses a
                JOIN div_rtis_braking_runs r ON r.analysis_id = a.id
                JOIN div_rtis_braking_halts h ON h.run_id = r.run_id
                JOIN div_rtis_violations v ON v.analysis_id = a.id
                WHERE {station_filter}
                  AND {date_filter}
                  AND h.halt_code = 'TKW'
                  AND v.violation_type = 'ghat'
                  AND v.halt_station IN ('KAD', 'MHLC', 'TKW', 'T5')
            """, base_params)
            middle_violations = cursor.fetchone()['count']

            summary.update({
                'up_line': len(trains_up),
                'middle_line': len(trains_middle),
                'no_ghat_data': len(trains_nodata),
                'up_violations': up_violations,
                'middle_violations': middle_violations
            })

        cursor.close()
        conn.close()

        return {
            "success": True,
            "station": station,
            "month": month,
            "year": year,
            "summary": summary,
            "trains": {
                "all": trains_all,
                "up": trains_up,
                "middle": trains_middle,
                "violations": trains_violations,
                "nodata": trains_nodata
            }
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Failed to fetch monthly station report: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# LP Report PDF Storage APIs
# ------------------------------

MAX_LP_REPORTS = 20  # Maximum PDFs to keep per LP

@app.post("/api/lp-reports/save")
async def save_lp_report(
    request: Request,
    criteria: dict = Body(...),
    run_id: str | None = Query(None)
):
    """
    Save analysis PDF to LP's folder.
    Auto-deletes oldest if LP already has 20 PDFs.
    """
    try:
        # Get user and filtered DataFrame
        user, filtered = _get_user_run(request, run_id)

        # Get LP HRMS ID from criteria or filtered data
        lp_hrms_id = criteria.get("lp_hrms_id")
        if not lp_hrms_id:
            # Try to get from filtered data
            if "LP_HRMS_ID" in filtered.columns and len(filtered) > 0:
                lp_hrms_id = str(filtered["LP_HRMS_ID"][0])

        # Handle "Other Division Crew" - use special folder
        if not lp_hrms_id or lp_hrms_id.strip() == "":
            lp_hrms_id = "other_division"

        analysis_id = criteria.get("analysis_id")
        if not analysis_id:
            return JSONResponse({"error": "Analysis ID required"}, status_code=400)

        # Create LP folder if not exists
        lp_folder = os.path.join(REPORTS_DIR, "lp", lp_hrms_id)
        os.makedirs(lp_folder, exist_ok=True)

        # Check and enforce 20 PDF limit (skip for other_division)
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        current_count = 0

        if lp_hrms_id != "other_division":
            # Count existing PDFs for this LP
            cursor.execute("""
                SELECT COUNT(*) as count FROM div_rtis_analyses
                WHERE lp_hrms_id = %s AND pdf_filename IS NOT NULL AND pdf_filename != ''
            """, [lp_hrms_id])
            count_result = cursor.fetchone()
            current_count = count_result['count'] if count_result else 0

        # If at limit, delete oldest (only for tracked LPs)
        if lp_hrms_id != "other_division" and current_count >= MAX_LP_REPORTS:
            # Get oldest reports to delete
            delete_count = current_count - MAX_LP_REPORTS + 1
            cursor.execute("""
                SELECT id, pdf_filename FROM div_rtis_analyses
                WHERE lp_hrms_id = %s AND pdf_filename IS NOT NULL AND pdf_filename != ''
                ORDER BY working_date ASC, created_at ASC
                LIMIT %s
            """, [lp_hrms_id, delete_count])
            old_reports = cursor.fetchall()

            for old_report in old_reports:
                # Delete file from disk
                old_file_path = os.path.join(REPORTS_DIR, "lp", lp_hrms_id, old_report['pdf_filename'])
                if os.path.exists(old_file_path):
                    os.remove(old_file_path)

                # Clear pdf_filename in database
                cursor.execute("""
                    UPDATE div_rtis_analyses SET pdf_filename = NULL WHERE id = %s
                """, [old_report['id']])

            conn.commit()

        # Generate PDF (reuse existing logic from export_pdf)
        # Apply criteria to filter data
        filtered = apply_criteria(filtered, criteria)
        if filtered.height == 0:
            cursor.close()
            conn.close()
            return JSONResponse({"error": "No data matches criteria"}, status_code=400)

        # Build chart payload and summary
        chart_payload = _build_chart_payload(filtered, criteria)
        unified_data = _braking_profile_full_curve(filtered, BRAKE_OFFSETS)
        brake_tests = _brake_tests(filtered, criteria.get("from_station_equals"), criteria.get("direction_equals"))
        summary = _build_summary_details(filtered, criteria)

        # Compute boundary and ghat sections
        to_station = (criteria.get("to_station_equals") or "").strip().upper()
        from_station = (criteria.get("from_station_equals") or "").strip().upper()
        direction = (criteria.get("direction_equals") or "").strip().upper()
        boundary_type = ROUTE_BOUNDARY_MAP.get(to_station) or ROUTE_BOUNDARY_MAP.get(from_station)
        boundary_stations = ZONE_BOUNDARY_STATIONS.get(boundary_type, []) if boundary_type else []
        boundary_row_idx = _find_boundary_row_index(filtered, boundary_stations) if boundary_stations else None
        ghat_halt_indices = _compute_ghat_section_halts(filtered, unified_data, direction)

        # Render charts and PDF
        speed_chart = _render_speed_chart_image(chart_payload)
        brake_charts = _render_brake_curve_images(unified_data)
        sectional_charts = _generate_sectional_charts(filtered, criteria)

        pdf_buffer = _render_pdf_report(
            summary, speed_chart, brake_charts, unified_data,
            brake_tests, sectional_charts, boundary_row_idx, ghat_halt_indices
        )

        # Generate filename using same convention as export_pdf
        filename = _generate_pdf_filename(filtered, criteria)

        # Save PDF to disk
        file_path = os.path.join(lp_folder, filename)
        with open(file_path, "wb") as f:
            f.write(pdf_buffer.getvalue())

        # Update database with pdf_filename
        cursor.execute("""
            UPDATE div_rtis_analyses SET pdf_filename = %s WHERE id = %s
        """, [filename, analysis_id])
        conn.commit()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": "PDF saved successfully",
            "filename": filename,
            "path": f"/reports/lp/{lp_hrms_id}/{filename}",
            "deleted_old": current_count >= MAX_LP_REPORTS
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            {"error": f"Failed to save LP report: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/lp-reports/{hrms_id}")
async def get_lp_reports(request: Request, hrms_id: str):
    """Get list of saved PDFs for an LP"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, train_number, working_date, loco_number, pdf_filename, created_at
            FROM div_rtis_analyses
            WHERE lp_hrms_id = %s AND pdf_filename IS NOT NULL AND pdf_filename != ''
            ORDER BY working_date DESC
            LIMIT 20
        """, [hrms_id])
        reports = cursor.fetchall()

        cursor.close()
        conn.close()

        # Format dates and add full path
        for report in reports:
            if report['working_date']:
                report['working_date'] = report['working_date'].strftime('%Y-%m-%d') if hasattr(report['working_date'], 'strftime') else str(report['working_date'])
            if report['created_at']:
                report['created_at'] = report['created_at'].strftime('%Y-%m-%d %H:%M') if hasattr(report['created_at'], 'strftime') else str(report['created_at'])
            report['pdf_url'] = f"/reports/lp/{hrms_id}/{report['pdf_filename']}"

        return {
            "success": True,
            "hrms_id": hrms_id,
            "reports": reports,
            "count": len(reports)
        }

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch LP reports: {str(e)}", "success": False},
            status_code=500
        )


@app.delete("/api/lp-reports/{hrms_id}/{report_id}")
async def delete_lp_report(request: Request, hrms_id: str, report_id: int):
    """Delete a specific LP report"""
    try:
        user = get_current_user(request)
        if not user:
            return JSONResponse({"error": "Authentication required"}, status_code=401)

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get the report details
        cursor.execute("""
            SELECT pdf_filename FROM div_rtis_analyses WHERE id = %s AND lp_hrms_id = %s
        """, [report_id, hrms_id])
        report = cursor.fetchone()

        if not report or not report['pdf_filename']:
            cursor.close()
            conn.close()
            return JSONResponse({"error": "Report not found"}, status_code=404)

        # Delete file from disk
        file_path = os.path.join(REPORTS_DIR, "lp", hrms_id, report['pdf_filename'])
        if os.path.exists(file_path):
            os.remove(file_path)

        # Clear pdf_filename in database
        cursor.execute("""
            UPDATE div_rtis_analyses SET pdf_filename = NULL WHERE id = %s
        """, [report_id])
        conn.commit()

        cursor.close()
        conn.close()

        return {"success": True, "message": "Report deleted"}

    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to delete report: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# Loco Management APIs
# ------------------------------

@app.get("/api/sheds")
async def get_sheds(request: Request):
    """Get all CR sheds"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM div_cr_sheds WHERE is_active = 1 ORDER BY shed_code")
        sheds = cursor.fetchall()
        cursor.close()
        conn.close()
        return {"success": True, "sheds": sheds}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch sheds: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/locos")
async def get_locos(
    request: Request,
    shed: str = Query(None),
    status: str = Query(None),
    loco_type: str = Query(None),
    search: str = Query(None),
    page: int = Query(1),
    limit: int = Query(50)
):
    """Get locos with optional filters"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Build query with filters
        where_clauses = []
        params = []

        if shed:
            where_clauses.append("current_shed = %s")
            params.append(shed)
        if status:
            where_clauses.append("status = %s")
            params.append(status)
        if loco_type:
            where_clauses.append("loco_type = %s")
            params.append(loco_type)
        if search:
            where_clauses.append("loco_number LIKE %s")
            params.append(f"%{search}%")

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Get total count
        cursor.execute(f"SELECT COUNT(*) as total FROM div_cr_locos WHERE {where_sql}", params)
        total = cursor.fetchone()['total']

        # Get paginated results
        offset = (page - 1) * limit
        params.extend([limit, offset])
        cursor.execute(f"""
            SELECT id, loco_number, loco_type, current_shed, status, commission_date, remarks, updated_at
            FROM div_cr_locos
            WHERE {where_sql}
            ORDER BY current_shed, loco_number
            LIMIT %s OFFSET %s
        """, params)
        locos = cursor.fetchall()

        # Get unique types for filter dropdown
        cursor.execute("SELECT DISTINCT loco_type FROM div_cr_locos ORDER BY loco_type")
        types = [row['loco_type'] for row in cursor.fetchall()]

        cursor.close()
        conn.close()

        return {
            "success": True,
            "locos": locos,
            "total": total,
            "page": page,
            "limit": limit,
            "pages": math.ceil(total / limit),
            "loco_types": types
        }
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch locos: {str(e)}", "success": False},
            status_code=500
        )


@app.post("/api/locos")
async def add_loco(request: Request):
    """Add a new loco"""
    try:
        data = await request.json()
        loco_number = data.get('loco_number', '').strip()
        loco_type = data.get('loco_type', '').strip()
        current_shed = data.get('current_shed', '').strip()
        commission_date = data.get('commission_date') or None
        remarks = data.get('remarks', '').strip() or None

        if not loco_number:
            return JSONResponse(
                {"error": "Loco number is required", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check if loco already exists
        cursor.execute("SELECT id FROM div_cr_locos WHERE loco_number = %s", [loco_number])
        if cursor.fetchone():
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco {loco_number} already exists", "success": False},
                status_code=400
            )

        cursor.execute("""
            INSERT INTO div_cr_locos (loco_number, loco_type, current_shed, status, commission_date, remarks)
            VALUES (%s, %s, %s, 'Active', %s, %s)
        """, [loco_number, loco_type, current_shed, commission_date, remarks])
        conn.commit()

        cursor.close()
        conn.close()

        return {"success": True, "message": f"Loco {loco_number} added successfully"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to add loco: {str(e)}", "success": False},
            status_code=500
        )


@app.put("/api/locos/{loco_number}")
async def update_loco(request: Request, loco_number: str):
    """Update loco details"""
    try:
        data = await request.json()
        loco_type = data.get('loco_type')
        current_shed = data.get('current_shed')
        commission_date = data.get('commission_date')
        remarks = data.get('remarks')

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Check if loco exists
        cursor.execute("SELECT id FROM div_cr_locos WHERE loco_number = %s", [loco_number])
        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco {loco_number} not found", "success": False},
                status_code=404
            )

        # Build update query dynamically
        updates = []
        params = []
        if loco_type is not None:
            updates.append("loco_type = %s")
            params.append(loco_type)
        if current_shed is not None:
            updates.append("current_shed = %s")
            params.append(current_shed)
        if commission_date is not None:
            updates.append("commission_date = %s")
            params.append(commission_date if commission_date else None)
        if remarks is not None:
            updates.append("remarks = %s")
            params.append(remarks if remarks else None)

        if updates:
            params.append(loco_number)
            cursor.execute(f"""
                UPDATE div_cr_locos SET {', '.join(updates)} WHERE loco_number = %s
            """, params)
            conn.commit()

        cursor.close()
        conn.close()

        return {"success": True, "message": f"Loco {loco_number} updated successfully"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to update loco: {str(e)}", "success": False},
            status_code=500
        )


@app.post("/api/locos/{loco_number}/transfer")
async def transfer_loco(request: Request, loco_number: str):
    """Transfer loco to another shed"""
    try:
        data = await request.json()
        to_shed = data.get('to_shed', '').strip()
        transfer_date = data.get('transfer_date')
        transfer_type = data.get('transfer_type', 'Internal')
        remarks = data.get('remarks', '').strip() or None
        created_by = data.get('created_by', '').strip() or None

        if not to_shed:
            return JSONResponse(
                {"error": "Destination shed is required", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get current shed
        cursor.execute("SELECT current_shed FROM div_cr_locos WHERE loco_number = %s", [loco_number])
        loco = cursor.fetchone()
        if not loco:
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco {loco_number} not found", "success": False},
                status_code=404
            )

        from_shed = loco['current_shed']

        if from_shed == to_shed:
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco is already at {to_shed}", "success": False},
                status_code=400
            )

        # Update loco's current shed
        cursor.execute("UPDATE div_cr_locos SET current_shed = %s WHERE loco_number = %s", [to_shed, loco_number])

        # Log transfer in history
        cursor.execute("""
            INSERT INTO div_cr_loco_transfers (loco_number, from_shed, to_shed, transfer_date, transfer_type, remarks, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, [loco_number, from_shed, to_shed, transfer_date, transfer_type, remarks, created_by])

        conn.commit()
        cursor.close()
        conn.close()

        return {"success": True, "message": f"Loco {loco_number} transferred from {from_shed} to {to_shed}"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to transfer loco: {str(e)}", "success": False},
            status_code=500
        )


@app.put("/api/locos/{loco_number}/status")
async def update_loco_status(request: Request, loco_number: str):
    """Update loco status (Active, Transferred Out, Condemned)"""
    try:
        data = await request.json()
        status = data.get('status', '').strip()
        remarks = data.get('remarks', '').strip() or None

        if status not in ['Active', 'Transferred Out', 'Condemned']:
            return JSONResponse(
                {"error": "Invalid status. Must be Active, Transferred Out, or Condemned", "success": False},
                status_code=400
            )

        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("SELECT id FROM div_cr_locos WHERE loco_number = %s", [loco_number])
        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco {loco_number} not found", "success": False},
                status_code=404
            )

        cursor.execute("""
            UPDATE div_cr_locos SET status = %s, remarks = %s WHERE loco_number = %s
        """, [status, remarks, loco_number])
        conn.commit()

        cursor.close()
        conn.close()

        return {"success": True, "message": f"Loco {loco_number} status updated to {status}"}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to update loco status: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/locos/{loco_number}/history")
async def get_loco_history(request: Request, loco_number: str):
    """Get transfer history for a loco"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get loco details
        cursor.execute("""
            SELECT loco_number, loco_type, current_shed, status, commission_date, remarks
            FROM div_cr_locos WHERE loco_number = %s
        """, [loco_number])
        loco = cursor.fetchone()

        if not loco:
            cursor.close()
            conn.close()
            return JSONResponse(
                {"error": f"Loco {loco_number} not found", "success": False},
                status_code=404
            )

        # Get transfer history
        cursor.execute("""
            SELECT from_shed, to_shed, transfer_date, transfer_type, remarks, created_by, created_at
            FROM div_cr_loco_transfers
            WHERE loco_number = %s
            ORDER BY transfer_date DESC, created_at DESC
        """, [loco_number])
        history = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"success": True, "loco": loco, "history": history}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to fetch loco history: {str(e)}", "success": False},
            status_code=500
        )


@app.get("/api/locos/search/{query}")
async def search_locos(request: Request, query: str):
    """Quick search locos by number (for autocomplete)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT loco_number, loco_type, current_shed
            FROM div_cr_locos
            WHERE loco_number LIKE %s AND status = 'Active'
            ORDER BY loco_number
            LIMIT 20
        """, [f"%{query}%"])
        locos = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"success": True, "locos": locos}
    except Exception as e:
        return JSONResponse(
            {"error": f"Failed to search locos: {str(e)}", "success": False},
            status_code=500
        )


# ------------------------------
# Run Server
# ------------------------------
if __name__ == "__main__":
    import uvicorn
    print("\n[SERVER] Starting uvicorn on http://localhost:8765")
    print("[SERVER] Press Ctrl+C to stop")
    print("[SERVER] Open browser: http://localhost:8765/ui/\n")
    uvicorn.run(app, host="0.0.0.0", port=8765)
def _detect_sharp_brake_point(
    speeds: list[float],
    dist_to_halt: list[float],
    times: list[str | None],
    halt_idx: int,
    max_distance_m: float = 1500.0,
    drop_threshold_kmph: float = 15.0,
    slope_threshold_mps2: float = 0.15,
    window: int = 5,
) -> dict[str, Any] | None:
    """
    Scan from ~max_distance_m away toward the halt and find the first block
    of `window` samples where the speed drops sharply compared to the
    preceding window. Uses both absolute speed drop and average deceleration.
    """
    start_idx = halt_idx
    for idx in range(halt_idx - 1, -1, -1):
        if dist_to_halt[idx] > max_distance_m:
            continue
        start_idx = idx
    start_idx = max(start_idx, 0)

    def window_stats(begin: int, end: int) -> dict[str, float]:
        count = end - begin
        if count <= 0:
            return {"avg": 0.0, "min": 0.0, "max": 0.0}
        values = [float(speeds[i] or 0.0) for i in range(begin, end)]
        return {
            "avg": sum(values) / count,
            "min": min(values),
            "max": max(values),
        }

    for idx in range(start_idx + window, halt_idx + 1):
        baseline_window_start = max(start_idx, idx - window * 2)
        baseline_window_end = max(start_idx, idx - window)
        current_window_start = max(start_idx, idx - window)
        current_window_end = idx

        baseline = window_stats(baseline_window_start, baseline_window_end)
        current = window_stats(current_window_start, current_window_end)

        baseline_speed = baseline["avg"]
        current_speed = current["avg"]
        speed_drop = baseline_speed - current_speed

        if speed_drop < drop_threshold_kmph:
            continue

        distance_start = dist_to_halt[current_window_start]
        distance_end = dist_to_halt[current_window_end - 1]
        distance_delta = max(distance_start - distance_end, 1.0)
        baseline_mps = baseline_speed * (1000.0 / 3600.0)
        current_mps = current_speed * (1000.0 / 3600.0)
        decel = (baseline_mps ** 2 - current_mps ** 2) / (2.0 * distance_delta)

        if decel < slope_threshold_mps2:
            continue

        idx_pick = current_window_start
        return {
            "index": idx_pick,
            "distance_m": dist_to_halt[idx_pick],
            "speed_kmph": speeds[idx_pick],
            "time": times[idx_pick],
        }

    return None


# ==============================================
# Fortnightly Braking Pattern Analysis
# ==============================================

NOMINATED_HALTS_CACHE: list[dict[str, Any]] = []
NOMINATED_HALTS_LOADED_AT: float = 0.0
NOMINATED_HALTS_CACHE_TTL = 300  # 5 minutes


def _load_nominated_halts(force: bool = False) -> list[dict[str, Any]]:
    """Load nominated halts from database with caching."""
    global NOMINATED_HALTS_CACHE, NOMINATED_HALTS_LOADED_AT
    now = time.time()
    if not force and NOMINATED_HALTS_CACHE and (now - NOMINATED_HALTS_LOADED_AT) < NOMINATED_HALTS_CACHE_TTL:
        return NOMINATED_HALTS_CACHE
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, halt_code, halt_name, section_type, section_name, direction, is_active
            FROM div_rtis_nominated_halts
            WHERE is_active = TRUE
        """)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        NOMINATED_HALTS_CACHE = rows
        NOMINATED_HALTS_LOADED_AT = now
        return rows
    except Exception as e:
        print(f"[WARN] Failed to load nominated halts: {e}")
        return []


_BRAKING_GEOFENCE_DEBUG_DONE = False

def _detect_nominated_halt_at_row(lat: float, lon: float, speed: float, direction: str,
                                   nominated_halts: list[dict]) -> dict | None:
    """
    Check if a row represents a halt at a nominated station using geofence + speed.
    Returns the halt info dict if detected, None otherwise.
    """
    global _BRAKING_GEOFENCE_DEBUG_DONE
    if not _BRAKING_GEOFENCE_DEBUG_DONE:
        # Debug: check geofences for each nominated halt once
        for halt in nominated_halts:
            geos = _geofences_for_station(halt["halt_code"], direction)
            print(f"\[####BRAKING####-GEO] Halt {halt['halt_code']} ({halt['direction']}): found {len(geos)} geofences")
            for g in geos:
                print(f"  -> {g['station_code']} at ({g['latitude']}, {g['longitude']}) r={g.get('radius_m', 120)}m dir={g['direction']}")
        _BRAKING_GEOFENCE_DEBUG_DONE = True

    if speed > 0.5:
        return None
    for halt in nominated_halts:
        if halt["direction"] not in (direction, "BOTH"):
            continue
        geos = _geofences_for_station(halt["halt_code"], direction)
        if not geos:
            continue
        inside, dist = _within_geofence(lat, lon, geos)
        if inside:
            return halt
    return None


def _extract_braking_window(df: pl.DataFrame, halt_idx: int, window_m: float = 2400.0) -> dict:
    """
    Extract braking data for window_m meters before a halt point.
    Returns dict with points and summary speeds at key distances.
    """
    speed_col = _find_speed_column(df.columns)
    dist_col = _find_distance_column(df.columns)
    lat_col = _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON")
    time_col = _find_time_column(df.columns)

    if not speed_col or not dist_col:
        return {"points": [], "speeds": {}}

    # Get data as lists
    speeds = df[speed_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    dist_steps = df[dist_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    times = df[time_col].to_list() if time_col else [None] * len(speeds)
    times = [_stringify_time(t) for t in times]

    # Build cumulative distance
    cumulative = []
    running = 0.0
    for step in dist_steps:
        running += max(0.0, step or 0.0)
        cumulative.append(running)

    halt_distance = cumulative[halt_idx]

    # Collect points within window (going backwards from halt)
    points = []
    speed_at_dist = {}
    key_distances = [2400, 1000, 400, 100]

    for i in range(halt_idx, -1, -1):
        dist_to_halt = halt_distance - cumulative[i]
        if dist_to_halt > window_m:
            break
        points.append({
            "seq": len(points),
            "distance_to_halt": round(dist_to_halt, 1),
            "speed": round(speeds[i], 1) if speeds[i] else 0.0,
            "time_str": times[i],
        })
        # Capture speeds at key distances
        for key_dist in key_distances:
            if key_dist not in speed_at_dist and dist_to_halt >= key_dist:
                speed_at_dist[key_dist] = round(speeds[i], 1) if speeds[i] else 0.0

    # Reverse so points go from far to near (2400m -> 0m)
    points.reverse()
    for i, p in enumerate(points):
        p["seq"] = i

    return {
        "points": points,
        "speeds": {
            "speed_2400m": speed_at_dist.get(2400),
            "speed_1000m": speed_at_dist.get(1000),
            "speed_400m": speed_at_dist.get(400),
            "speed_100m": speed_at_dist.get(100),
        },
        "halt_distance_m": halt_distance,
        "halt_time": times[halt_idx] if halt_idx < len(times) else None,
    }


def _save_braking_run_data(conn, analysis_id: int, criteria: dict, df: pl.DataFrame, direction: str) -> int:
    """
    Detect halts at nominated stations and save braking data.
    Returns number of halts saved.
    """
    print(f"####BRAKING#### Starting braking data save for analysis_id={analysis_id}, direction={direction}")

    # Convert working_date to MySQL format
    working_date_raw = criteria.get("working_date")
    working_date_sql = None
    if working_date_raw:
        try:
            parsed = datetime.strptime(working_date_raw, "%d-%m-%Y")
            working_date_sql = parsed.strftime("%Y-%m-%d")
        except ValueError:
            try:
                parsed = datetime.strptime(working_date_raw, "%d/%m/%Y")
                working_date_sql = parsed.strftime("%Y-%m-%d")
            except ValueError:
                working_date_sql = working_date_raw

    nominated_halts = _load_nominated_halts()
    print(f"\[####BRAKING####] Loaded {len(nominated_halts)} nominated halts: {[h['halt_code'] for h in nominated_halts]}")
    if not nominated_halts:
        print("\[####BRAKING####] No nominated halts found - exiting")
        return 0

    speed_col = _find_speed_column(df.columns)
    dist_col = _find_distance_column(df.columns)
    lat_col = _first_matching_column(df.columns, "LAT")
    lon_col = _first_matching_column(df.columns, "LON")

    print(f"\[####BRAKING####] Columns: speed={speed_col}, dist={dist_col}, lat={lat_col}, lon={lon_col}")
    if not speed_col or not dist_col or not lat_col or not lon_col:
        print("\[####BRAKING####] Missing required columns - exiting")
        return 0

    speeds = df[speed_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    lats = df[lat_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()
    lons = df[lon_col].cast(pl.Float64, strict=False).fill_null(0.0).to_list()

    print(f"\[####BRAKING####] Data rows: {len(speeds)}, checking for halts...")

    # Find halts at nominated stations
    detected_halts = []
    last_halt_positions = {}  # Track last detected position for each halt code
    halt_candidates = 0
    MIN_DISTANCE_SAME_HALT = 500.0  # Minimum 500m between same halt detections

    for idx in range(len(speeds)):
        if speeds[idx] <= 0.5:
            halt_candidates += 1
        halt_info = _detect_nominated_halt_at_row(lats[idx], lons[idx], speeds[idx], direction, nominated_halts)
        if halt_info:
            halt_code = halt_info["halt_code"]
            # Check if we've detected this halt before and if we're far enough from last detection
            if halt_code in last_halt_positions:
                last_lat, last_lon = last_halt_positions[halt_code]
                dist = _haversine_m(lats[idx], lons[idx], last_lat, last_lon)
                if dist < MIN_DISTANCE_SAME_HALT:
                    # Too close to previous detection of same halt, skip
                    continue

            print(f"####BRAKING#### Detected halt: {halt_code} at idx={idx}, lat={lats[idx]}, lon={lons[idx]}")
            detected_halts.append({"idx": idx, "halt_info": halt_info})
            last_halt_positions[halt_code] = (lats[idx], lons[idx])

    print(f"\[####BRAKING####] Found {halt_candidates} rows with speed<=0.5, detected {len(detected_halts)} nominated halts")

    if not detected_halts:
        print("\[####BRAKING####] No nominated halts detected - exiting")
        return 0

    cursor = conn.cursor()

    # Insert braking run
    run_sql = """
        INSERT INTO div_rtis_braking_runs (
            analysis_id, train_number, date_of_working, lp_hrms_id, lp_name,
            alp_hrms_id, alp_name, loco_number, from_station, to_station,
            direction, route
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    cursor.execute(run_sql, (
        analysis_id,
        criteria.get("train_number"),
        working_date_sql,
        criteria.get("lp_hrms_id"),
        criteria.get("lp_name"),
        criteria.get("alp_hrms_id"),
        criteria.get("alp_name"),
        criteria.get("loco_number"),
        criteria.get("from_station_equals"),
        criteria.get("to_station_equals"),
        direction,
        criteria.get("route"),
    ))
    run_id = cursor.lastrowid

    halts_saved = 0

    for halt_data in detected_halts:
        halt_info = halt_data["halt_info"]
        halt_idx = halt_data["idx"]

        # Extract window data
        window = _extract_braking_window(df, halt_idx, 2400.0)
        if not window["points"]:
            continue

        speeds_data = window["speeds"]

        # Insert halt summary
        halt_sql = """
            INSERT INTO div_rtis_braking_halts (
                run_id, halt_code, halt_name, section_type, section_name,
                halt_distance_m, halt_time, speed_2400m, speed_1000m, speed_400m, speed_100m
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(halt_sql, (
            run_id,
            halt_info["halt_code"],
            halt_info["halt_name"],
            halt_info["section_type"],
            halt_info["section_name"],
            window["halt_distance_m"],
            window["halt_time"],
            speeds_data.get("speed_2400m"),
            speeds_data.get("speed_1000m"),
            speeds_data.get("speed_400m"),
            speeds_data.get("speed_100m"),
        ))

        # Insert window points
        points_sql = """
            INSERT INTO div_rtis_braking_points (run_id, halt_code, seq, distance_to_halt, speed, time_str)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        for point in window["points"]:
            cursor.execute(points_sql, (
                run_id,
                halt_info["halt_code"],
                point["seq"],
                point["distance_to_halt"],
                point["speed"],
                point["time_str"],
            ))

        halts_saved += 1

    conn.commit()
    cursor.close()
    return halts_saved


@app.get("/api/braking-analysis/halts")
async def get_braking_analysis_halts(
    request: Request,
    start_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="End date (YYYY-MM-DD)"),
    section_type: str = Query("ALL", description="GHAT, PLAIN, or ALL"),
    direction: str = Query("UP", description="UP or DN"),
):
    """
    Get nominated halts that have braking data in the date range.
    Returns halt codes with run counts.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Base query for halts with data
        sql = """
            SELECT
                nh.halt_code,
                nh.halt_name,
                nh.section_type,
                nh.section_name,
                nh.direction,
                COUNT(DISTINCT bh.run_id) as run_count
            FROM div_rtis_nominated_halts nh
            LEFT JOIN div_rtis_braking_halts bh ON nh.halt_code = bh.halt_code
            LEFT JOIN div_rtis_braking_runs br ON bh.run_id = br.run_id
                AND br.date_of_working BETWEEN %s AND %s
                AND br.direction = %s
            WHERE nh.is_active = TRUE
        """
        params = [start_date, end_date, direction]

        if section_type and section_type != "ALL":
            sql += " AND nh.section_type = %s"
            params.append(section_type)

        sql += " AND (nh.direction = %s OR nh.direction = 'BOTH')"
        params.append(direction)

        sql += " GROUP BY nh.halt_code, nh.halt_name, nh.section_type, nh.section_name, nh.direction"
        sql += " ORDER BY nh.section_type, nh.section_name, nh.halt_code"

        cursor.execute(sql, params)
        halts = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"halts": halts}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/braking-analysis/data")
async def get_braking_analysis_data(
    request: Request,
    halt_code: str = Query(..., description="Halt code (e.g., T5, TKW, DR)"),
    section_type: str = Query(..., description="GHAT or PLAIN"),
    direction: str = Query(..., description="UP or DN"),
    start_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="End date (YYYY-MM-DD)"),
    lp_hrms_id: str = Query(None, description="Optional LP HRMS ID filter"),
    limit: int = Query(20, description="Max runs to return (for random selection)"),
):
    """
    Get braking pattern data for a specific halt.
    Returns runs with window points for ECharts visualization.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Get runs for this halt
        run_sql = """
            SELECT DISTINCT
                br.run_id,
                br.train_number,
                br.date_of_working,
                br.lp_hrms_id,
                br.lp_name,
                br.alp_hrms_id,
                br.alp_name,
                br.loco_number,
                br.direction,
                bh.halt_code,
                bh.halt_name,
                bh.section_type,
                bh.section_name,
                bh.speed_2400m,
                bh.speed_1000m,
                bh.speed_400m,
                bh.speed_100m
            FROM div_rtis_braking_runs br
            JOIN div_rtis_braking_halts bh ON br.run_id = bh.run_id
            WHERE bh.halt_code = %s
              AND bh.section_type = %s
              AND br.direction = %s
              AND br.date_of_working BETWEEN %s AND %s
        """
        params = [halt_code, section_type, direction, start_date, end_date]

        if lp_hrms_id:
            run_sql += " AND br.lp_hrms_id = %s"
            params.append(lp_hrms_id)
            # For specific LP, get all runs
        else:
            # Random selection for "All LPs" mode
            run_sql += " ORDER BY RAND() LIMIT %s"
            params.append(limit)

        cursor.execute(run_sql, params)
        runs = cursor.fetchall()

        # Get points for each run
        result_runs = []
        for run in runs:
            points_sql = """
                SELECT seq, distance_to_halt, speed, time_str
                FROM div_rtis_braking_points
                WHERE run_id = %s AND halt_code = %s
                ORDER BY seq
            """
            cursor.execute(points_sql, (run["run_id"], halt_code))
            points = cursor.fetchall()

            # Format date for legend
            date_str = ""
            if run["date_of_working"]:
                date_str = run["date_of_working"].strftime("%d/%m") if hasattr(run["date_of_working"], "strftime") else str(run["date_of_working"])[:5]

            result_runs.append({
                "run_id": run["run_id"],
                "train_number": run["train_number"],
                "date_of_working": str(run["date_of_working"]) if run["date_of_working"] else None,
                "date_short": date_str,
                "lp_hrms_id": run["lp_hrms_id"],
                "lp_name": run["lp_name"],
                "alp_name": run["alp_name"],
                "loco_number": run["loco_number"],
                "direction": run["direction"],
                "halt_code": run["halt_code"],
                "halt_name": run["halt_name"],
                "section_type": run["section_type"],
                "section_name": run["section_name"],
                "speeds": {
                    "speed_2400m": run["speed_2400m"],
                    "speed_1000m": run["speed_1000m"],
                    "speed_400m": run["speed_400m"],
                    "speed_100m": run["speed_100m"],
                },
                # TODO: REVERT noise filter after presentation
                # Filter out noisy points:
                #   High noise: GHAT > 60 km/h, PLAIN > 110 km/h
                #   Low noise dips: speed < 10 km/h when distance > 300m
                "points": [
                    {
                        "distance": p["distance_to_halt"],
                        "speed": p["speed"],
                    }
                    for p in points
                    if (
                        # Filter high speed noise by section type
                        ((run["section_type"] == "GHAT" and (p["speed"] or 0) <= 60) or
                         (run["section_type"] == "PLAIN" and (p["speed"] or 0) <= 110) or
                         (run["section_type"] not in ("GHAT", "PLAIN")))
                        and
                        # Filter low speed dips far from halt (noise)
                        not ((p["distance_to_halt"] or 0) > 300 and (p["speed"] or 0) < 10)
                    )
                ],
                # Legend format: "DD/MM TrainNo LocoNo - LP Name"
                "legend": f"{date_str} {run['train_number'] or ''} {run['loco_number'] or ''} - {run['lp_name'] or ''}",
            })

        cursor.close()
        conn.close()

        return {
            "halt_code": halt_code,
            "section_type": section_type,
            "direction": direction,
            "runs": result_runs,
            "total_runs": len(result_runs),
        }

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/braking-analysis/lps")
async def get_braking_analysis_lps(
    request: Request,
    halt_code: str = Query(..., description="Halt code"),
    start_date: str = Query(..., description="Start date (YYYY-MM-DD)"),
    end_date: str = Query(..., description="End date (YYYY-MM-DD)"),
    direction: str = Query("UP", description="UP or DN"),
):
    """
    Get LPs who have braking data at a specific halt.
    Returns list of LPs with run counts.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        sql = """
            SELECT
                br.lp_hrms_id,
                br.lp_name,
                COUNT(*) as run_count
            FROM div_rtis_braking_runs br
            JOIN div_rtis_braking_halts bh ON br.run_id = bh.run_id
            WHERE bh.halt_code = %s
              AND br.date_of_working BETWEEN %s AND %s
              AND br.direction = %s
              AND br.lp_hrms_id IS NOT NULL
            GROUP BY br.lp_hrms_id, br.lp_name
            ORDER BY run_count DESC, br.lp_name
        """
        cursor.execute(sql, (halt_code, start_date, end_date, direction))
        lps = cursor.fetchall()

        cursor.close()
        conn.close()

        return {"lps": lps}

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/braking-analysis/cleanup")
async def cleanup_braking_data(
    request: Request,
    days: int = Query(60, description="Delete data older than N days"),
):
    """
    Cleanup old braking analysis data.
    Admin only - deletes braking data older than specified days.
    """
    user = get_current_user(request)
    if not user:
        return JSONResponse({"error": "Authentication required"}, status_code=401)

    # Optional: Check for admin role
    # if user.get("role") != "admin":
    #     return JSONResponse({"error": "Admin access required"}, status_code=403)

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Delete old braking runs (cascade deletes halts and points)
        sql = """
            DELETE FROM div_rtis_braking_runs
            WHERE created_at < DATE_SUB(NOW(), INTERVAL %s DAY)
        """
        cursor.execute(sql, (days,))
        deleted_count = cursor.rowcount
        conn.commit()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "message": f"Deleted {deleted_count} braking runs older than {days} days",
            "deleted_count": deleted_count,
        }

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
