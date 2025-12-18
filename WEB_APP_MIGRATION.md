# Web App Migration & MySQL Integration Plan

**Status:** Planning Phase
**Last Updated:** 2025-12-18
**Target:** Convert desktop app to web application with MySQL integration

---

## Executive Summary

**Current State:** Desktop app (PyInstaller EXE) with embedded CSV files
**Target State:** Web application with MySQL backend for dynamic data
**Key Driver:** Daily TSR updates and frequent staff changes require easier data management

---

## User Requirements (Confirmed)

| Requirement | Answer | Impact |
|-------------|--------|--------|
| **Offline Access** | Not needed - always online | ✅ Pure web app viable |
| **Server Location** | Cloud/VPS (DigitalOcean/AWS) | ✅ MySQL accessible remotely |
| **Concurrent Users** | 5-20 users (medium team) | ⚠️ Need proper resource allocation |
| **Authentication** | Existing Node.js login system | ✅ Can reuse/integrate |
| **Data Updates** | Daily (TSR), Frequent (Staff) | ✅ MySQL essential |

---

## Architecture Decisions

### ✅ Decision 1: Telemetry Data Processing
**Keep CSV upload + in-memory processing (NO MySQL)**

**Reasoning:**
- Current performance: 2-3 seconds for full analysis
- MySQL would add 10-15 seconds (insert + query overhead)
- Telemetry data is session-specific, not shared
- No need for persistence after analysis

**Implementation:**
```
User uploads CSV → FastAPI receives file → Polars loads into memory → Analysis → Response
(No database involved for telemetry)
```

**Status:** ✅ CONFIRMED - Keep current approach

---

### 🤔 Decision 2: TSR (Temporary Speed Restrictions)
**To be decided: CSV or MySQL**

**Current State:**
- Not implemented yet (planned feature)
- Changes daily
- Currently no TSR management exists

**Option A: MySQL Table**
- ✅ Easy daily updates via admin panel
- ✅ Can set expiry dates (auto-deactivate)
- ✅ Audit trail (who added/modified)
- ❌ Adds database dependency

**Option B: CSV File**
- ✅ Simple, matches current architecture
- ✅ Can version control (git)
- ❌ Requires app rebuild/redeploy for updates
- ❌ No expiry automation

**Recommendation:** MySQL (due to daily changes)
**Status:** ⏳ PENDING - Decide in next step

---

### ✅ Decision 3: Staff Data
**Use existing MySQL table: `div_staff_master`**

**Current State:**
- Desktop app uses `mail_staff.csv`
- Node.js system has `div_staff_master` table
- Changes frequently (transfers, new hires)

**Migration Path:**
1. Map CSV columns → MySQL columns
2. Replace CSV loading with MySQL query
3. Remove `mail_staff.csv` from app bundle

**Status:** ✅ CONFIRMED - Migrate to MySQL

---

### 📋 Decision 4: Static Reference Data

#### PSR (Permanent Speed Restrictions)
- **Current:** `base-data/all_section_psr.csv`
- **Change Frequency:** Rarely (months/years)
- **Recommendation:** Keep as CSV initially, migrate to MySQL later
- **Status:** ⏳ CSV for now

#### Geofences (Station Locations)
- **Current:** `base-data/geo locations - Sheet1.csv`
- **Change Frequency:** Rarely
- **Recommendation:** Keep as CSV
- **Status:** ⏳ CSV for now

#### Train Metadata
- **Current:** `base-data/train_with_from_to_stations*.csv`
- **Change Frequency:** Occasional (new trains)
- **Recommendation:** Keep as CSV initially
- **Status:** ⏳ CSV for now

#### MPS Configuration
- **Current:** Hardcoded in `app.py` as `MPS_CONFIG` dict
- **Change Frequency:** Very rare
- **Recommendation:** Keep hardcoded
- **Status:** ✅ No change

---

## Data Mapping

### Staff Data: CSV → MySQL

**Current CSV:** `mail_staff.csv`
```csv
name,designation,hq_station,email,phone
```

**MySQL Table:** `div_staff_master` (existing) ✅ **CONFIRMED**

**Key Fields:**
- `hrms_id` (PRIMARY KEY) - Employee ID
- `name` - Full name
- `designation_id` (FK) - Links to `designations` table
- `current_cli_id` (FK) - Crew Lobby Index (for loco pilots)
- `hq_station` - Headquarter station
- `cug_number` - Railway CUG phone
- `phone_number` - Personal phone
- `email` - Email address
- `status` - 'Active', 'Retired', etc.
- Foreign keys: `designations`, `offices`, `div_cli_master`

**Required JOIN for Full Data:**
```sql
SELECT
    s.hrms_id,
    s.name,
    d.designation_name,  -- From designations table
    c.cli_name,          -- From div_cli_master table (if applicable)
    s.hq_station,
    s.cug_number,
    s.email,
    s.status
FROM div_staff_master s
LEFT JOIN designations d ON s.designation_id = d.id
LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
WHERE s.status = 'Active'
    AND d.designation_name IN ('LP', 'ALP', 'LP(PASS)', 'LP(GOODS)', 'LP(MAIL)')
    -- Filter only loco pilots and assistant loco pilots
```

**Column Mapping:**
| App Needs | MySQL Source | Notes |
|-----------|--------------|-------|
| Employee ID | `hrms_id` | Primary key |
| Name | `name` | Direct mapping |
| Designation | `designations.designation_name` | Requires JOIN |
| CLI/Lobby | `div_cli_master.cli_name` | Requires JOIN |
| HQ Station | `hq_station` | Direct mapping |
| Phone | `cug_number` OR `phone_number` | Prefer CUG |
| Email | `email` | Optional |

**Related Tables:** ✅ **CONFIRMED**

**1. `designations` table:**
- `id` (PK) - Auto increment
- `designation_code` - Unique code (e.g., 'LP')
- `designation_name` - Full name (e.g., 'Loco Pilot Mail/Express')
- `department` - Department name
- `grade_level` - Pay grade

**2. `div_cli_master` table (Crew Lobby In-charge):**
- `cli_id` (PK) - Auto increment
- `cli_hrms_id` - HRMS ID of the CLI
- `cli_name` - CLI's name (e.g., 'PREM SINGH')
- `cli_mobile` - CLI contact number
- `current_office_code` - Office/depot code
- `is_active` - Active status

**Sample Data:**
```
HRMS ID  | Name                      | Designation               | CLI Name       | HQ Station | CUG Number  | Status
---------|---------------------------|---------------------------|----------------|------------|-------------|--------
AACWWR   | Narhari Prabhu            | Loco Pilot Mail/Express   | PREM SINGH     | CSMT-ML    | 9004413755  | Active
ABNXJC   | Sabu Anthony              | Loco Pilot Mail/Express   | Amarnath Dubey | CSMT-ML    | 9004413561  | Active
AFCWJE   | SD Shah                   | Loco Pilot Mail/Express   | PREM SINGH     | CSMT-ML    | 9004413810  | Active
```

---

### TSR Data: New MySQL Table (If MySQL chosen)

**Proposed Schema:**
```sql
CREATE TABLE tsr_restrictions (
    id INT PRIMARY KEY AUTO_INCREMENT,
    restriction_id VARCHAR(50) UNIQUE NOT NULL,
    route VARCHAR(100) NOT NULL,
    direction ENUM('UP', 'DN', 'BOTH') DEFAULT 'BOTH',
    from_station VARCHAR(10),
    to_station VARCHAR(10),
    from_lat DECIMAL(10, 7),
    from_lon DECIMAL(10, 7),
    to_lat DECIMAL(10, 7),
    to_lon DECIMAL(10, 7),
    speed_limit_kmph INT NOT NULL,
    reason TEXT,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_route_active (route, is_active),
    INDEX idx_dates (start_date, end_date),
    INDEX idx_active (is_active)
);
```

**Features:**
- Auto-expiry based on `end_date`
- Audit trail (`created_by`, `created_at`)
- Can be managed via admin panel
- Supports same format as PSR for code compatibility

---

## Deployment Architecture

### Current (Desktop App)
```
User's PC
  └─> RailDataApp.exe (localhost:8765)
      ├─> Embedded CSV files
      ├─> Polars processing
      └─> Opens browser → http://localhost:8765/ui/
```

### Target (Web App)
```
User Browser
  ↓
Nginx (port 80/443) - yourserver.com
  ↓
  ├─> Node.js App (port 3000) - /
  │   └─> MySQL (existing tables)
  │
  └─> FastAPI App (port 8765) - /rail-analysis
      ├─> MySQL (staff, TSR)
      ├─> CSV files (PSR, geofences, trains)
      └─> In-memory CSV processing (telemetry)
```

**URL Structure:**
- Main website: `https://yourserver.com/`
- Rail Analysis: `https://yourserver.com/rail-analysis/ui/`
- Node.js Admin: `https://yourserver.com/admin/tsr`

---

## Authentication Strategy

### Option A: Shared JWT Token (Recommended)
```
Node.js login → Generate JWT → Set cookie
  ↓
User accesses /rail-analysis
  ↓
FastAPI reads cookie → Validates JWT → Grants access
```

**Pros:**
- Single login for both apps
- Stateless, scalable
- Standard approach

**Implementation:**
1. Node.js signs JWT on login
2. FastAPI verifies JWT signature
3. Share secret key via environment variable

---

### Option B: Shared Session Table
```
Node.js login → Create session in MySQL
  ↓
User accesses /rail-analysis
  ↓
FastAPI checks session in MySQL → Grants access
```

**Pros:**
- Centralized session management
- Can revoke sessions immediately

**Implementation:**
1. Node.js creates session record
2. FastAPI queries same session table
3. Both read from MySQL

---

**Decision:** ⏳ PENDING - Depends on Node.js auth implementation

---

## Migration Phases

### Phase 1: Database Integration (Local Testing) ✅ COMPLETE
**Goal:** Connect app to MySQL, load staff data

**Tasks:**
- [x] Get MySQL connection details (host, user, password, database)
- [x] Get exact schema of `div_staff_master` table
- [x] Install `mysql-connector-python`
- [x] Create `db_config.py` with connection pool
- [x] Create `db_loader.py` with `load_staff_from_db()`
- [x] Create SSH tunnel setup (`start-ssh-tunnel.sh`)
- [x] Create `.env` file with credentials
- [x] Test connection: ✅ SUCCESS
- [x] Test staff loading: ✅ 2,446 records loaded
- [ ] Integrate with `app.py` (Next step)
- [ ] Verify staff data appears in analysis/exports

**Actual Time:** 2 hours
**Status:** ✅ COMPLETE (pending app.py integration)

---

### Phase 2: TSR Implementation ⏳
**Goal:** Decide TSR approach and implement

**Tasks:**
- [ ] **Decision:** CSV or MySQL for TSR?
- [ ] If MySQL: Create `tsr_restrictions` table
- [ ] If MySQL: Create `load_tsr_from_db()` function
- [ ] If CSV: Create TSR CSV file format
- [ ] Integrate TSR with existing PSR logic in `app.py`
- [ ] Test TSR overlays on speed charts
- [ ] Test TSR in PDF exports

**Estimated Time:** 4-5 hours
**Status:** NOT STARTED

---

### Phase 3: Web Deployment ⏳
**Goal:** Deploy FastAPI app on VPS alongside Node.js

**Tasks:**
- [ ] Prepare server (install Python 3.11, setup venv)
- [ ] Upload code to `/var/www/rail-analysis`
- [ ] Configure environment variables (.env file)
- [ ] Setup process manager (PM2 or systemd)
- [ ] Configure Nginx reverse proxy
- [ ] Test from external browser
- [ ] Monitor performance with 2-3 concurrent users

**Estimated Time:** 4-6 hours
**Status:** NOT STARTED

---

### Phase 4: Authentication Integration ⏳
**Goal:** Share login between Node.js and FastAPI

**Tasks:**
- [ ] Understand Node.js auth mechanism (JWT or sessions?)
- [ ] Implement token/session validation in FastAPI
- [ ] Add authentication middleware to protected routes
- [ ] Test: Login on Node.js → Access rail-analysis without re-login
- [ ] Handle unauthorized access (redirect to login)

**Estimated Time:** 3-4 hours
**Status:** NOT STARTED

---

### Phase 5: Admin Panel (Optional) ⏳
**Goal:** Non-technical users can manage TSR via web form

**Tasks:**
- [ ] Create admin UI in Node.js for TSR management
- [ ] Add/Edit/Delete TSR records
- [ ] Set expiry dates, activate/deactivate
- [ ] View active TSRs
- [ ] Audit log (who added/modified)

**Estimated Time:** 6-8 hours
**Status:** NOT STARTED

---

## Performance Considerations

### Expected Performance After Migration

| Operation | Current (Desktop) | Target (Web) | Notes |
|-----------|-------------------|--------------|-------|
| **App Startup** | 1-2 sec | 2-3 sec | +1 sec for MySQL connection |
| **CSV Upload** | 2-3 sec | 3-4 sec | Network upload overhead |
| **Analysis** | 2-3 sec | 2-3 sec | No change (in-memory) |
| **PDF Export** | 5-6 sec | 6-7 sec | +1 sec for network transfer |
| **Concurrent Users** | 1 user | 5-20 users | Need 4-8 GB RAM |

### Server Requirements

**Minimum:**
- CPU: 2 cores
- RAM: 4 GB (2 GB for FastAPI, 2 GB for Node.js + MySQL)
- Storage: 20 GB
- Network: 10 Mbps upload (for PDF downloads)

**Recommended (5-20 users):**
- CPU: 4 cores
- RAM: 8 GB
- Storage: 50 GB (for uploaded CSV storage)
- Network: 50 Mbps

---

## Rollback Plan

If web app has issues, can revert to desktop app:

**Option 1: Keep desktop app available**
- Distribute both web URL and desktop EXE
- Users choose based on preference
- Desktop app uses local CSV, web app uses MySQL

**Option 2: Desktop app with MySQL**
- Desktop app also connects to MySQL
- Best of both worlds
- Requires MySQL accessible from user machines

---

## Open Questions

### Technical
- [ ] What is exact schema of `div_staff_master`?
- [ ] Which columns from staff table are needed?
- [ ] Does Node.js use JWT or session-based auth?
- [ ] What is JWT secret key (if using JWT)?
- [ ] What is MySQL host/port for remote connection?
- [ ] Is MySQL accessible from internet or VPN required?

### Business
- [ ] Who will manage TSR updates? (Railway staff or admin)
- [ ] How often are TSRs added/modified?
- [ ] Is there existing TSR data to migrate?
- [ ] Do users need mobile access (responsive design)?
- [ ] Should old analysis reports be stored in database?

---

## Next Steps (Immediate)

### Step 1: Information Gathering ⏳ IN PROGRESS

**Goal:** Get MySQL connection details and schema

**Completed:**
- ✅ Got `div_staff_master` schema
- ✅ Got `designations` table schema
- ✅ Got `div_cli_master` table schema
- ✅ Got sample data (5 active loco pilots)

**Analysis from Sample Data:**
- Staff identified by `hrms_id` (unique, e.g., 'AACWWR')
- Names can be displayed as-is (e.g., 'Narhari Prabhu')
- All sample records are "Loco Pilot Mail/Express"
- HQ stations use format: STATION-TYPE (e.g., 'CSMT-ML' = CSMT Mail)
- CUG numbers are 10-digit mobile format

**Still Needed:**

1. **MySQL connection details** (to start coding):
   - Host: ?
   - Port: ? (usually 3306)
   - Database name: ?
   - Username: ?
   - Password: ?

2. **Business Logic Questions:**
   - Should we filter only specific designations (LP, ALP) or load all staff?
   - How does the app currently match staff names from telemetry CSV to staff database?
   - Are staff names in telemetry CSV exactly same as DB (or need fuzzy matching)?

3. **Current CSV format:**
   - Can you share a few lines from current `mail_staff.csv`?
   - This will help us ensure data compatibility

### Step 2: Local MySQL Integration Test
Once we have connection details, I'll create these files:

**File 1: `db_config.py`**
```python
"""Database connection configuration"""
import os
from typing import Optional
import mysql.connector
from mysql.connector import pooling

# Load from environment or defaults
DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "your-host.com"),
    "port": int(os.getenv("MYSQL_PORT", 3306)),
    "user": os.getenv("MYSQL_USER", "your_user"),
    "password": os.getenv("MYSQL_PASSWORD", "your_password"),
    "database": os.getenv("MYSQL_DATABASE", "your_database"),
}

# Connection pool for concurrent users (web app)
connection_pool = None

def init_connection_pool(pool_size: int = 5):
    """Initialize connection pool"""
    global connection_pool
    try:
        connection_pool = pooling.MySQLConnectionPool(
            pool_name="rail_analysis_pool",
            pool_size=pool_size,
            **DB_CONFIG
        )
        print(f"[DB] Connection pool initialized ({pool_size} connections)")
        return True
    except mysql.connector.Error as e:
        print(f"[DB ERROR] Failed to create connection pool: {e}")
        return False

def get_db_connection():
    """Get connection from pool (or create new if pool not initialized)"""
    if connection_pool:
        return connection_pool.get_connection()
    else:
        # Fallback: direct connection (for desktop app or testing)
        return mysql.connector.connect(**DB_CONFIG)

def test_connection() -> bool:
    """Test database connectivity"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        cursor.close()
        conn.close()
        print("[DB] Connection test successful!")
        return result[0] == 1
    except Exception as e:
        print(f"[DB ERROR] Connection test failed: {e}")
        return False
```

**File 2: `db_loader.py`**
```python
"""Database data loaders for base reference data"""
import polars as pl
from db_config import get_db_connection

def load_staff_from_db(active_only: bool = True, lp_only: bool = True) -> pl.DataFrame:
    """
    Load staff data from MySQL

    Args:
        active_only: Only load active staff
        lp_only: Only load Loco Pilots and ALPs

    Returns:
        Polars DataFrame with columns: hrms_id, name, designation, cli_name, hq_station, cug_number
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Build query with optional filters
    query = """
        SELECT
            s.hrms_id,
            s.name,
            d.designation_name AS designation,
            c.cli_name,
            s.hq_station,
            s.cug_number,
            s.email,
            s.status
        FROM div_staff_master s
        LEFT JOIN designations d ON s.designation_id = d.id
        LEFT JOIN div_cli_master c ON s.current_cli_id = c.cli_id
        WHERE 1=1
    """

    if active_only:
        query += " AND s.status = 'Active'"

    if lp_only:
        # Filter for loco pilot designations
        query += """ AND d.designation_name IN (
            'Loco Pilot Mail/Express',
            'Loco Pilot Goods',
            'Loco Pilot Passenger',
            'Assistant Loco Pilot',
            'LP',
            'ALP'
        )"""

    query += " ORDER BY s.name"

    try:
        cursor.execute(query)
        rows = cursor.fetchall()
        cursor.close()
        conn.close()

        # Convert to Polars DataFrame
        if rows:
            df = pl.DataFrame(rows)
            print(f"[DB] Loaded {len(df)} staff records from MySQL")
            return df
        else:
            print("[DB] No staff records found")
            return pl.DataFrame(schema={
                "hrms_id": pl.Utf8,
                "name": pl.Utf8,
                "designation": pl.Utf8,
                "cli_name": pl.Utf8,
                "hq_station": pl.Utf8,
                "cug_number": pl.Utf8,
                "email": pl.Utf8,
                "status": pl.Utf8,
            })
    except Exception as e:
        print(f"[DB ERROR] Failed to load staff data: {e}")
        cursor.close()
        conn.close()
        raise

def load_staff_from_csv_fallback() -> pl.DataFrame:
    """Fallback: Load staff from CSV if MySQL fails"""
    try:
        df = pl.read_csv("mail_staff.csv")
        print(f"[FALLBACK] Loaded {len(df)} staff records from CSV")
        return df
    except Exception as e:
        print(f"[FALLBACK ERROR] Failed to load CSV: {e}")
        return pl.DataFrame()
```

**File 3: Modify `app.py` startup**
```python
# Add at top of app.py
from db_config import init_connection_pool, test_connection
from db_loader import load_staff_from_db, load_staff_from_csv_fallback

# Global variable for staff data
STAFF_DF: pl.DataFrame | None = None

@app.on_event("startup")
def startup_event():
    """Load base data on startup"""
    global STAFF_DF

    # Initialize connection pool for web app
    pool_initialized = init_connection_pool(pool_size=10)

    # Test connection
    if pool_initialized and test_connection():
        try:
            # Try loading from MySQL
            STAFF_DF = load_staff_from_db(active_only=True, lp_only=True)
            print(f"[STARTUP] Loaded {len(STAFF_DF)} staff from MySQL")
        except Exception as e:
            print(f"[STARTUP ERROR] MySQL load failed: {e}")
            # Fallback to CSV
            STAFF_DF = load_staff_from_csv_fallback()
    else:
        # No MySQL connection, use CSV
        print("[STARTUP] MySQL unavailable, using CSV fallback")
        STAFF_DF = load_staff_from_csv_fallback()

    # Continue loading other base data (PSR, geofences, etc.)
    # ... existing code ...
```

**Testing Steps:**
1. Add connection details to `.env` file
2. Install: `pip install mysql-connector-python`
3. Run: `python -c "from db_config import test_connection; test_connection()"`
4. Run: `uvicorn app:app --reload`
5. Check logs for "Loaded X staff from MySQL"

### Step 3: Decide on TSR Approach
Based on your workflow preference:
- **MySQL:** If you want admin panel for daily updates
- **CSV:** If you're comfortable editing CSV files

---

## Success Metrics

### Phase 1 Success (MySQL Integration)
- [ ] App loads staff data from MySQL
- [ ] Staff names appear correctly in analysis
- [ ] Staff names appear correctly in PDF exports
- [ ] No performance degradation
- [ ] Error handling if MySQL is unreachable

### Phase 2 Success (TSR Implementation)
- [ ] TSR restrictions load from source (MySQL or CSV)
- [ ] TSR overlays appear on speed charts
- [ ] TSR violations highlighted in analysis
- [ ] TSR data included in PDF reports
- [ ] Expired TSRs automatically excluded

### Phase 3 Success (Web Deployment)
- [ ] App accessible from browser at yourserver.com/rail-analysis
- [ ] Multiple users can access simultaneously
- [ ] File uploads work (tested with 50 MB CSV)
- [ ] PDF downloads work correctly
- [ ] App auto-restarts if crashed
- [ ] Nginx handles SSL termination

### Phase 4 Success (Authentication)
- [ ] Users log in once on Node.js site
- [ ] Automatically authenticated for rail-analysis
- [ ] Unauthorized users redirected to login
- [ ] Session persists across both apps
- [ ] Logout works for both apps

---

## Change Log

| Date | Phase | Change | Reason |
|------|-------|--------|--------|
| 2025-12-18 | Planning | Initial document created | Architecture discussion |
| 2025-12-18 | Planning | Confirmed telemetry stays as CSV | Performance optimization |
| 2025-12-18 | Planning | Confirmed staff migrates to MySQL | Frequent updates needed |
| 2025-12-18 | Planning | Received `div_staff_master` schema | Database integration mapping |
| 2025-12-18 | Planning | Identified need for JOINs with `designations` and `div_cli_master` | Complete staff info requires related tables |
| 2025-12-18 | Phase 1 | Created MySQL connection infrastructure | db_config.py, db_loader.py, .env, SSH tunnel |
| 2025-12-18 | Phase 1 | ✅ Successfully connected to MySQL via SSH tunnel | Loaded 2,446 staff records |
| 2025-12-18 | Phase 1 | ✅ Tested staff data loading and search | Ready for app.py integration |

---

## Notes

- Keep this document updated as we progress through each phase
- Mark tasks as complete with ✅
- Add new decisions and rationale
- Document any issues encountered
- Track performance benchmarks

---

**Current Status:** Awaiting MySQL connection details to begin Phase 1
