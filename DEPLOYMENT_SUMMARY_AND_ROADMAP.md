# RTIS Web Integration - Deployment Summary & Roadmap

## ✅ Completed Deployment (December 21, 2025)

### 1. Initial Setup & Configuration

#### Security Fix: Remove Hardcoded Credentials
**Issue:** Database credentials were hardcoded in `db_config.py`

**Solution:**
```python
# Before (INSECURE):
MYSQL_USER = "jay"
MYSQL_PASSWORD = "4310jay"

# After (SECURE):
MYSQL_USER = os.getenv("MYSQL_USER")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD")
```

**Files Updated:**
- `db_config.py` - Use environment variables
- `.env.example` - Template for credentials
- `ENVIRONMENT_SETUP.md` - Configuration guide

---

#### GitHub Repository Setup
**Repository:** https://github.com/jaynair0405/rail-data-app

**Actions Completed:**
1. Created GitHub repository
2. Configured remote: `git remote add origin`
3. Pushed all code and documentation
4. `.env` protected via `.gitignore`

---

### 2. Server Deployment

#### Initial Deployment Steps

**Location:** `/home/railway/rail-data-app/`

**Steps Executed:**
```bash
# 1. Clone repository
git clone https://github.com/jaynair0405/rail-data-app.git
cd rail-data-app

# 2. Install python3-venv (system dependency)
sudo apt install python3.12-venv

# 3. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 4. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt

# 5. Configure environment
cp .env.example .env
nano .env  # Added server MySQL credentials
chmod 600 .env

# 6. Test manually
uvicorn app:app --host 127.0.0.1 --port 8765
# Verified working, then Ctrl+C

# 7. Set up PM2 process manager
nano start.sh
chmod +x start.sh
pm2 start start.sh --name rtis
pm2 save
```

**Server Specifications:**
- CPU: AMD EPYC 9354P 32-Core Processor
- Supports: AVX2, AVX-512 (no need for polars-lts-cpu)
- OS: Ubuntu with systemd

---

#### PM2 Configuration

**Startup Script:** `start.sh`
```bash
#!/bin/bash
cd /home/railway/rail-data-app
source .venv/bin/activate
exec uvicorn app:app --host 127.0.0.1 --port 8765
```

**PM2 Processes:**
```
│ id │ name            │ status  │ memory   │ Description
├────┼─────────────────┼─────────┼──────────┼────────────────────
│ 0  │ railway-system  │ online  │ 90.6mb   │ bbtro (Node.js)
│ 2  │ rtis            │ online  │ 197.6mb  │ rail-data-app (Python)
```

**Management Commands:**
```bash
pm2 list              # Show all processes
pm2 logs rtis         # View logs
pm2 restart rtis      # Restart service
pm2 save              # Save configuration
```

---

### 3. Issues Fixed During Deployment

#### Issue 1: bbtro Service Crash After Git Pull

**Error:**
```
Error: Cannot find module 'express-mysql-session'
```

**Root Cause:** New npm package added but not installed

**Solution:**
```bash
cd /home/railway/bbtro
npm install
pm2 restart railway-system
```

**Lesson:** Always run `npm install` after `git pull` if `package.json` changed

---

#### Issue 2: CSV Upload Failed (413 Error)

**Error:**
```
POST https://crtms.in/spm/rtis/load_csv 413 (Request Entity Too Large)
```

**Root Cause:** Nginx default upload limit is 1MB, but RTIS CSV files can be 5-20MB

**Solution:**

**File:** `/etc/nginx/sites-available/railway-system`

**Added:**
```nginx
server {
    server_name crtms.in www.crtms.in;
    client_max_body_size 50M;  # ← Added this line
    
    location / {
        proxy_pass http://localhost:3000;
        ...
    }
    ...
}
```

**Applied:**
```bash
sudo nginx -t                    # Test configuration
sudo systemctl reload nginx      # Apply changes
```

**Result:** CSV files up to 50MB can now be uploaded

---

#### Issue 3: LP Name Showing as "Unknown" in PDF

**Error:** PDF export showed LP name as "unknown" even though name was selected in UI

**Root Cause:** UI was sending `lp_hrms_id` but not `lp_name` to backend

**Code Analysis:**

**Backend Expected (app.py:1908-1909):**
```python
"lp_name": criteria.get("lp_name"),
"ncli_name": criteria.get("ncli_name"),
```

**Frontend Sent (ui/index.html:486-491):**
```javascript
// Before (INCOMPLETE):
lp_hrms_id: document.getElementById("lpHrmsId").value,
cli_id: document.getElementById("lpCliId").value,
analyst_cli_id: document.getElementById("analystCliId").value,
analyst_name: document.getElementById("analystInput").value,
// Missing: lp_name and ncli_name
```

**Solution:**
```javascript
// After (COMPLETE):
lp_hrms_id: document.getElementById("lpHrmsId").value || undefined,
cli_id: document.getElementById("lpCliId").value || undefined,
analyst_cli_id: document.getElementById("analystCliId").value || undefined,
lp_name: document.getElementById("lpInput").value || undefined,        // ← Added
ncli_name: document.getElementById("ncliInput").value || undefined,    // ← Added
analyst_name: document.getElementById("analystInput").value || undefined,
```

**Deployment:**
```bash
# Local: Push fix
git commit -m "Fix: Add lp_name and ncli_name to criteria for PDF export"
git push

# Server: Pull and restart
cd /home/railway/rail-data-app
git pull origin main
pm2 restart rtis
```

**Result:** LP and NCLI names now appear correctly in PDF reports

---

#### Issue 4: Database Access Control

**Requirement:** Users must have `can_access_rtis` flag set in database

**Database Update:**
```sql
-- Add column to users table
ALTER TABLE users ADD COLUMN can_access_rtis TINYINT(1) DEFAULT 0;

-- Grant access to division admin
UPDATE users SET can_access_rtis = 1 WHERE username = 'div_admin';

-- Or grant to all division users
UPDATE users SET can_access_rtis = 1 WHERE realm = 'division';

-- Verify
SELECT id, username, realm, can_access_rtis FROM users;
```

**Authentication Flow:**
```
1. User logs into bbtro → Session stored in MySQL
2. User accesses /spm/rtis/rtis → Request proxied to rail-data-app
3. rail-data-app reads session from MySQL
4. Checks can_access_rtis flag
5. If flag = 1 → Allow access
6. If flag = 0 → Redirect to login
```

---

### 4. Architecture Overview

#### Request Flow
```
┌─────────────────────────────────────────────────────────┐
│  User Browser (Anywhere in World)                       │
│  https://crtms.in/spm/rtis/rtis                        │
└────────────────┬────────────────────────────────────────┘
                 │
                 ↓
┌─────────────────────────────────────────────────────────┐
│  Nginx (Port 443)                                       │
│  - SSL/TLS termination                                  │
│  - client_max_body_size: 50M                           │
└────────────────┬────────────────────────────────────────┘
                 │
                 ↓
┌─────────────────────────────────────────────────────────┐
│  bbtro (Node.js, localhost:3000)                       │
│  - Session management                                   │
│  - Authentication                                       │
│  - Reverse proxy: /spm/rtis/* → localhost:8765        │
└────────────────┬────────────────────────────────────────┘
                 │
                 ↓
┌─────────────────────────────────────────────────────────┐
│  rail-data-app (Python FastAPI, localhost:8765)        │
│  - Validates bbtro session from MySQL                  │
│  - Checks can_access_rtis flag                         │
│  - Serves RTIS UI and API                              │
│  - Generates PDF reports                               │
└────────────────┬────────────────────────────────────────┘
                 │
                 ↓
┌─────────────────────────────────────────────────────────┐
│  MySQL (localhost:3306)                                 │
│  - sessions table (bbtro sessions)                     │
│  - users table (authentication)                        │
│  - div_staff_master (LP/CLI data)                      │
└─────────────────────────────────────────────────────────┘
```

#### Port Security
| Port | Service | Public? | Access Method |
|------|---------|---------|---------------|
| 443 | Nginx | ✅ Yes | Direct internet access |
| 3000 | bbtro | ❌ No | Via Nginx proxy only |
| 8765 | rail-data-app | ❌ No | Via bbtro proxy only |
| 3306 | MySQL | ❌ No | Localhost only |

**Security Benefits:**
- Only port 443 exposed to internet
- Python app hidden behind Node.js auth layer
- All requests authenticated via bbtro
- Database never exposed externally

---

### 5. Update Workflow (Future)

#### For bbtro Updates:
```bash
# Local: Make changes and push
git add .
git commit -m "Description of changes"
git push origin master

# Server: Pull and restart
ssh railway@server
cd /home/railway/bbtro
git pull origin master
npm install                    # If package.json changed
pm2 restart railway-system
pm2 logs railway-system        # Check for errors
```

#### For rail-data-app Updates:
```bash
# Local: Make changes and push
git add .
git commit -m "Description of changes"
git push origin main

# Server: Pull and restart
ssh railway@server
cd /home/railway/rail-data-app
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt  # If requirements.txt changed
pm2 restart rtis
pm2 logs rtis                    # Check for errors
```

---

### 6. Current Capabilities

✅ **Working Features:**
- User authentication via bbtro sessions
- CSV file upload (up to 50MB)
- Speed profile analysis with PSR limits
- Sectional speed profile charts
- Braking pattern analysis
- Brake test validation (feel & power)
- PDF report generation with:
  - Speed profiles (full journey + sectional)
  - Braking curves
  - Braking pattern table
  - Brake test results
  - Complete journey metadata
- Staff selection from division database
- Auto-populated NCLI from staff master
- MPS (Maximum Permissible Speed) overlay
- Station halt markers on charts

---

## 🚀 Future Enhancements & Roadmap

### Phase 1: Data Persistence & Analytics (Priority: High)

#### 1.1 Database Schema Design

**New Table: `rtis_analyses`**

```sql
CREATE TABLE rtis_analyses (
    -- Primary Key
    id INT AUTO_INCREMENT PRIMARY KEY,
    
    -- Dates
    analysis_date DATE NOT NULL COMMENT 'Date when analysis was performed',
    working_date DATE COMMENT 'Date of actual train journey',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    -- Journey Details
    train_number VARCHAR(10) COMMENT 'Train number',
    from_station VARCHAR(10) COMMENT 'Origin station code',
    to_station VARCHAR(10) COMMENT 'Destination station code',
    direction VARCHAR(5) COMMENT 'UP or DN',
    loco_number VARCHAR(20) COMMENT 'Locomotive number',
    coach_type VARCHAR(10) COMMENT 'LHB or ICF',
    
    -- Staff Details (Links to div_staff_master)
    lp_hrms_id VARCHAR(20) COMMENT 'Loco Pilot HRMS ID',
    lp_name VARCHAR(100) COMMENT 'Loco Pilot name',
    lp_cli_id INT COMMENT 'LP CLI ID',
    ncli_cli_id INT COMMENT 'NCLI CLI ID',
    ncli_name VARCHAR(100) COMMENT 'NCLI name',
    analyst_cli_id INT COMMENT 'Analyst CLI ID',
    analyst_name VARCHAR(100) COMMENT 'Analyst name',
    
    -- Journey Statistics
    start_time TIME COMMENT 'Journey start time',
    end_time TIME COMMENT 'Journey end time',
    duration_minutes INT COMMENT 'Total journey duration',
    total_distance_km DECIMAL(8,2) COMMENT 'Total distance covered',
    
    -- Analysis Results
    total_halts INT COMMENT 'Number of halts detected',
    csv_row_count INT COMMENT 'Number of data points in CSV',
    max_speed DECIMAL(5,2) COMMENT 'Maximum speed recorded (km/h)',
    avg_speed DECIMAL(5,2) COMMENT 'Average speed (km/h)',
    
    -- PSR Compliance
    psr_violations_count INT DEFAULT 0 COMMENT 'Number of PSR violations',
    psr_violation_details JSON COMMENT 'Details of each violation',
    
    -- Brake Tests
    brake_test_feel VARCHAR(10) COMMENT 'PASS/FAIL/NOT_RUN',
    brake_test_feel_start_speed DECIMAL(5,2),
    brake_test_feel_end_speed DECIMAL(5,2),
    brake_test_power VARCHAR(10) COMMENT 'PASS/FAIL/NOT_RUN',
    brake_test_power_start_speed DECIMAL(5,2),
    brake_test_power_end_speed DECIMAL(5,2),
    
    -- Braking Performance
    braking_100m_max DECIMAL(5,2) COMMENT 'Highest speed at 100m before halt',
    braking_100m_avg DECIMAL(5,2) COMMENT 'Average speed at 100m',
    braking_20m_max DECIMAL(5,2) COMMENT 'Highest speed at 20m before halt',
    braking_violations_count INT DEFAULT 0 COMMENT 'Braking pattern violations',
    
    -- Files & References
    pdf_filename VARCHAR(255) COMMENT 'Generated PDF filename',
    pdf_stored BOOLEAN DEFAULT FALSE COMMENT 'PDF saved to disk?',
    csv_filename VARCHAR(255) COMMENT 'Original CSV filename',
    
    -- Metadata
    remarks TEXT COMMENT 'Additional notes or observations',
    flagged BOOLEAN DEFAULT FALSE COMMENT 'Flagged for review',
    flag_reason VARCHAR(255) COMMENT 'Reason for flagging',
    
    -- Indexes for Performance
    INDEX idx_lp_hrms (lp_hrms_id),
    INDEX idx_working_date (working_date),
    INDEX idx_analysis_date (analysis_date),
    INDEX idx_route (from_station, to_station),
    INDEX idx_train (train_number),
    INDEX idx_direction (direction),
    INDEX idx_analyst (analyst_cli_id),
    INDEX idx_created (created_at),
    
    -- Foreign Key Constraints (Optional)
    FOREIGN KEY (lp_cli_id) REFERENCES div_staff_master(cli_id) ON DELETE SET NULL,
    FOREIGN KEY (ncli_cli_id) REFERENCES div_staff_master(cli_id) ON DELETE SET NULL,
    FOREIGN KEY (analyst_cli_id) REFERENCES div_staff_master(cli_id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**Design Rationale:**
- **Normalization:** Links to existing `div_staff_master` via CLI IDs
- **Performance:** Indexes on commonly queried fields
- **Flexibility:** JSON field for complex violation details
- **Audit Trail:** `created_at` and `updated_at` timestamps
- **Scalability:** InnoDB engine for transactions and foreign keys

---

#### 1.2 Backend Implementation

**File:** `app.py`

**New Function: Save Analysis to Database**

```python
def _save_analysis_to_db(
    criteria: dict, 
    summary: dict, 
    brake_tests: dict,
    halts: list,
    dataset: pl.DataFrame
) -> int:
    """
    Save analysis metadata to rtis_analyses table.
    Returns the inserted analysis ID.
    """
    from db_config import get_db_connection
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Calculate additional statistics
    speeds = dataset[_find_speed_column(dataset.columns)].to_list()
    max_speed = max(speeds) if speeds else None
    avg_speed = sum(speeds) / len(speeds) if speeds else None
    
    # Count PSR violations (simplified)
    psr_violations = 0
    # TODO: Implement actual PSR violation counting logic
    
    # Braking statistics
    braking_100m_speeds = []
    braking_20m_speeds = []
    for halt in halts:
        if halt.get("speeds", {}).get("100", {}).get("speed"):
            braking_100m_speeds.append(halt["speeds"]["100"]["speed"])
        if halt.get("speeds", {}).get("20", {}).get("speed"):
            braking_20m_speeds.append(halt["speeds"]["20"]["speed"])
    
    braking_100m_max = max(braking_100m_speeds) if braking_100m_speeds else None
    braking_100m_avg = sum(braking_100m_speeds) / len(braking_100m_speeds) if braking_100m_speeds else None
    braking_20m_max = max(braking_20m_speeds) if braking_20m_speeds else None
    
    # Count braking violations
    braking_violations = sum(1 for halt in halts 
                            if halt.get("speeds", {}).get("100", {}).get("speed", 0) > 20
                            or halt.get("speeds", {}).get("20", {}).get("speed", 0) >= 10)
    
    # Parse working date
    working_date_str = summary.get("working_date")
    working_date = None
    if working_date_str:
        try:
            working_date = datetime.strptime(working_date_str, "%d/%m/%Y").date()
        except:
            pass
    
    # Insert query
    cursor.execute("""
        INSERT INTO rtis_analyses (
            analysis_date, working_date,
            train_number, from_station, to_station, direction,
            loco_number, coach_type,
            lp_hrms_id, lp_name, lp_cli_id,
            ncli_cli_id, ncli_name,
            analyst_cli_id, analyst_name,
            start_time, end_time,
            total_halts, csv_row_count,
            max_speed, avg_speed,
            psr_violations_count,
            brake_test_feel, brake_test_feel_start_speed, brake_test_feel_end_speed,
            brake_test_power, brake_test_power_start_speed, brake_test_power_end_speed,
            braking_100m_max, braking_100m_avg, braking_20m_max,
            braking_violations_count,
            pdf_generated
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """, (
        datetime.now().date(),
        working_date,
        criteria.get("train_number"),
        criteria.get("from_station_equals"),
        criteria.get("to_station_equals"),
        criteria.get("direction_equals"),
        criteria.get("loco_number"),
        criteria.get("coach_type"),
        criteria.get("lp_hrms_id"),
        criteria.get("lp_name"),
        criteria.get("cli_id"),
        criteria.get("cli_id"),  # ncli uses same cli_id
        criteria.get("ncli_name"),
        criteria.get("analyst_cli_id"),
        criteria.get("analyst_name"),
        summary.get("start_time"),
        summary.get("end_time"),
        len(halts),
        summary.get("row_count"),
        max_speed,
        avg_speed,
        psr_violations,
        brake_tests.get("feel", {}).get("status"),
        brake_tests.get("feel", {}).get("start_speed"),
        brake_tests.get("feel", {}).get("end_speed"),
        brake_tests.get("power", {}).get("status"),
        brake_tests.get("power", {}).get("start_speed"),
        brake_tests.get("power", {}).get("end_speed"),
        braking_100m_max,
        braking_100m_avg,
        braking_20m_max,
        braking_violations,
        True  # pdf_generated
    ))
    
    analysis_id = cursor.lastrowid
    conn.commit()
    cursor.close()
    conn.close()
    
    print(f"[DB] Saved analysis ID: {analysis_id}")
    return analysis_id
```

**Modify `export_pdf` Endpoint:**

```python
@app.post("/export_pdf")
def export_pdf(criteria: dict = Body(...)):
    if DF is None:
        return JSONResponse({"error": "no data loaded"}, status_code=400)
    
    filtered = apply_criteria(DF, criteria)
    if filtered.height == 0:
        return JSONResponse({"error": "no data matches criteria"}, status_code=400)
    
    try:
        chart_payload = _build_chart_payload(filtered, criteria)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    
    unified_data = _braking_profile_full_curve(filtered, BRAKE_OFFSETS)
    brake_tests = _brake_tests(filtered, criteria.get("from_station_equals"), 
                                criteria.get("direction_equals"))
    summary = _build_summary_details(filtered, criteria)
    
    try:
        speed_chart = _render_speed_chart_image(chart_payload)
        brake_charts = _render_brake_curve_images(unified_data)
        sectional_charts = _generate_sectional_charts(filtered, criteria)
        pdf_buffer = _render_pdf_report(summary, speed_chart, brake_charts, 
                                       unified_data, brake_tests, sectional_charts)
        
        # ✅ NEW: Save analysis to database
        try:
            analysis_id = _save_analysis_to_db(criteria, summary, brake_tests, 
                                              unified_data, filtered)
            print(f"[SUCCESS] Analysis saved with ID: {analysis_id}")
        except Exception as db_error:
            # Log error but don't fail PDF generation
            print(f"[ERROR] Failed to save to database: {db_error}")
            # Continue with PDF generation
        
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    
    filename = _generate_pdf_filename(filtered, criteria)
    
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
```

**Error Handling Strategy:**
- Database save errors are logged but don't prevent PDF generation
- User still gets their PDF even if DB save fails
- Errors are visible in logs for debugging

---

#### 1.3 API Endpoints for Data Access

**Add to `app.py`:**

```python
# ==========================================
# Analysis History & Reporting APIs
# ==========================================

@app.get("/api/analyses/recent")
def get_recent_analyses(
    limit: int = 50, 
    offset: int = 0,
    from_date: str = None,
    to_date: str = None,
    lp_hrms_id: str = None,
    train_number: str = None
):
    """
    Get list of recent analyses with optional filters.
    
    Query Parameters:
    - limit: Number of records (default 50, max 200)
    - offset: Pagination offset
    - from_date: Filter by analysis_date >= (YYYY-MM-DD)
    - to_date: Filter by analysis_date <= (YYYY-MM-DD)
    - lp_hrms_id: Filter by specific LP
    - train_number: Filter by train
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Build WHERE clause
    where_clauses = []
    params = []
    
    if from_date:
        where_clauses.append("analysis_date >= %s")
        params.append(from_date)
    
    if to_date:
        where_clauses.append("analysis_date <= %s")
        params.append(to_date)
    
    if lp_hrms_id:
        where_clauses.append("lp_hrms_id = %s")
        params.append(lp_hrms_id)
    
    if train_number:
        where_clauses.append("train_number = %s")
        params.append(train_number)
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    # Get total count
    cursor.execute(f"SELECT COUNT(*) as total FROM rtis_analyses WHERE {where_sql}", params)
    total = cursor.fetchone()["total"]
    
    # Get records
    params.extend([min(limit, 200), offset])
    cursor.execute(f"""
        SELECT 
            id, analysis_date, working_date, train_number,
            from_station, to_station, direction,
            lp_hrms_id, lp_name, ncli_name, analyst_name,
            total_halts, psr_violations_count, braking_violations_count,
            brake_test_feel, brake_test_power,
            max_speed, avg_speed,
            created_at
        FROM rtis_analyses 
        WHERE {where_sql}
        ORDER BY created_at DESC 
        LIMIT %s OFFSET %s
    """, params)
    
    results = cursor.fetchall()
    cursor.close()
    conn.close()
    
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "analyses": results
    }


@app.get("/api/analyses/{analysis_id}")
def get_analysis_detail(analysis_id: int):
    """Get detailed information for a specific analysis."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("SELECT * FROM rtis_analyses WHERE id = %s", (analysis_id,))
    result = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    if not result:
        return JSONResponse({"error": "Analysis not found"}, status_code=404)
    
    return result


@app.get("/api/analyses/lp/{hrms_id}")
def get_lp_analyses(hrms_id: str):
    """Get all analyses for a specific Loco Pilot."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    cursor.execute("""
        SELECT 
            id, analysis_date, working_date, train_number,
            from_station, to_station, direction,
            total_halts, brake_test_feel, brake_test_power,
            psr_violations_count, braking_violations_count
        FROM rtis_analyses 
        WHERE lp_hrms_id = %s
        ORDER BY working_date DESC
    """, (hrms_id,))
    
    results = cursor.fetchall()
    
    # Get summary statistics
    cursor.execute("""
        SELECT 
            COUNT(*) as total_analyses,
            SUM(CASE WHEN brake_test_feel = 'PASS' AND brake_test_power = 'PASS' THEN 1 ELSE 0 END) as both_passed,
            SUM(psr_violations_count) as total_psr_violations,
            SUM(braking_violations_count) as total_braking_violations,
            MAX(working_date) as last_analysis_date
        FROM rtis_analyses 
        WHERE lp_hrms_id = %s
    """, (hrms_id,))
    
    stats = cursor.fetchone()
    
    cursor.close()
    conn.close()
    
    return {
        "lp_hrms_id": hrms_id,
        "statistics": stats,
        "analyses": results
    }


@app.get("/api/reports/lp-coverage")
def lp_coverage_report():
    """
    LP Coverage Report: Compare div_staff_master with rtis_analyses
    to identify which LPs have been analyzed and which haven't.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Get all active LPs from staff master
    cursor.execute("""
        SELECT hrms_id, name, office, designation
        FROM div_staff_master 
        WHERE designation LIKE '%LP%' 
        AND status = 'active'
        ORDER BY name
    """)
    all_lps = cursor.fetchall()
    
    # Get LPs who have been analyzed
    cursor.execute("""
        SELECT 
            lp_hrms_id,
            lp_name,
            COUNT(*) as analysis_count,
            MAX(working_date) as last_analysis_date,
            SUM(CASE WHEN brake_test_feel = 'PASS' AND brake_test_power = 'PASS' THEN 1 ELSE 0 END) as passed_count,
            SUM(psr_violations_count) as total_violations
        FROM rtis_analyses 
        GROUP BY lp_hrms_id, lp_name
        ORDER BY last_analysis_date DESC
    """)
    analyzed_lps = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    # Calculate coverage
    total_lps = len(all_lps)
    analyzed_count = len(analyzed_lps)
    coverage_percentage = (analyzed_count / total_lps * 100) if total_lps > 0 else 0
    
    # Find LPs not yet analyzed
    analyzed_hrms_ids = {lp["lp_hrms_id"] for lp in analyzed_lps}
    not_analyzed = [
        {
            "hrms_id": lp["hrms_id"],
            "name": lp["name"],
            "office": lp["office"],
            "designation": lp["designation"]
        }
        for lp in all_lps 
        if lp["hrms_id"] not in analyzed_hrms_ids
    ]
    
    return {
        "summary": {
            "total_lps": total_lps,
            "analyzed_count": analyzed_count,
            "not_analyzed_count": len(not_analyzed),
            "coverage_percentage": round(coverage_percentage, 2)
        },
        "analyzed_lps": analyzed_lps,
        "not_analyzed_lps": not_analyzed
    }


@app.get("/api/reports/statistics")
def get_statistics(
    from_date: str = None,
    to_date: str = None
):
    """
    Get aggregate statistics for analyses within a date range.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    # Build date filter
    where_clauses = []
    params = []
    
    if from_date:
        where_clauses.append("analysis_date >= %s")
        params.append(from_date)
    
    if to_date:
        where_clauses.append("analysis_date <= %s")
        params.append(to_date)
    
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    
    # Overall statistics
    cursor.execute(f"""
        SELECT 
            COUNT(*) as total_analyses,
            COUNT(DISTINCT lp_hrms_id) as unique_lps,
            COUNT(DISTINCT train_number) as unique_trains,
            SUM(psr_violations_count) as total_psr_violations,
            SUM(braking_violations_count) as total_braking_violations,
            SUM(CASE WHEN brake_test_feel = 'PASS' THEN 1 ELSE 0 END) as feel_pass_count,
            SUM(CASE WHEN brake_test_power = 'PASS' THEN 1 ELSE 0 END) as power_pass_count,
            AVG(max_speed) as avg_max_speed,
            AVG(avg_speed) as avg_journey_speed
        FROM rtis_analyses 
        WHERE {where_sql}
    """, params)
    
    overall_stats = cursor.fetchone()
    
    # Analyses per day
    cursor.execute(f"""
        SELECT 
            analysis_date,
            COUNT(*) as count
        FROM rtis_analyses
        WHERE {where_sql}
        GROUP BY analysis_date
        ORDER BY analysis_date DESC
        LIMIT 30
    """, params)
    
    daily_counts = cursor.fetchall()
    
    # Most analyzed routes
    cursor.execute(f"""
        SELECT 
            CONCAT(from_station, ' - ', to_station) as route,
            direction,
            COUNT(*) as count
        FROM rtis_analyses
        WHERE {where_sql}
        GROUP BY from_station, to_station, direction
        ORDER BY count DESC
        LIMIT 10
    """, params)
    
    top_routes = cursor.fetchall()
    
    cursor.close()
    conn.close()
    
    return {
        "overall": overall_stats,
        "daily_trend": daily_counts,
        "top_routes": top_routes
    }
```

---

### Phase 2: Dashboard & Visualization (Priority: Medium)

#### 2.1 Dashboard Design

**Location:** `/home/railway/bbtro/public/div/rtis-dashboard.html`

**Features:**

1. **Summary Cards (Top Row)**
   - Total Analyses This Month
   - Total LPs Analyzed
   - Coverage Percentage
   - Average Compliance Rate

2. **Recent Analyses Table**
   - Sortable columns
   - Filters: Date range, LP, Train, Route
   - Pagination
   - Click row to view details

3. **LP Coverage Section**
   - Pie chart: Analyzed vs Not Analyzed
   - List of LPs not yet analyzed
   - Export to Excel

4. **Trends Charts**
   - Analyses per day (line chart)
   - Brake test pass rates (bar chart)
   - PSR violations trend

5. **Quick Actions**
   - Export report to Excel
   - View full analysis details
   - Flag analysis for review

#### 2.2 Implementation Approach

**Technology Stack:**
- HTML/CSS/JavaScript (matches bbtro styling)
- Chart.js for visualizations
- DataTables.js for sortable tables
- Fetch API for backend calls

**Sample Code Snippet:**

```html
<!-- rtis-dashboard.html -->
<!DOCTYPE html>
<html>
<head>
    <title>RTIS Analytics Dashboard</title>
    <link rel="stylesheet" href="/css/dashboard.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body>
    <div class="dashboard-container">
        <!-- Summary Cards -->
        <div class="summary-cards">
            <div class="card">
                <h3>Total Analyses</h3>
                <div class="stat" id="totalAnalyses">0</div>
            </div>
            <div class="card">
                <h3>LP Coverage</h3>
                <div class="stat" id="lpCoverage">0%</div>
            </div>
            <!-- More cards -->
        </div>
        
        <!-- Recent Analyses Table -->
        <div class="table-section">
            <h2>Recent Analyses</h2>
            <table id="analysesTable">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Train</th>
                        <th>Route</th>
                        <th>LP Name</th>
                        <th>Brake Tests</th>
                        <th>Violations</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody></tbody>
            </table>
        </div>
        
        <!-- Charts -->
        <div class="charts-section">
            <canvas id="trendsChart"></canvas>
            <canvas id="coverageChart"></canvas>
        </div>
    </div>
    
    <script>
        // Fetch data from API
        async function loadDashboard() {
            const response = await fetch('/spm/rtis/api/analyses/recent?limit=50');
            const data = await response.json();
            
            // Populate table
            renderTable(data.analyses);
            
            // Update summary cards
            document.getElementById('totalAnalyses').textContent = data.total;
            
            // Load coverage data
            const coverage = await fetch('/spm/rtis/api/reports/lp-coverage');
            const coverageData = await coverage.json();
            document.getElementById('lpCoverage').textContent = 
                coverageData.summary.coverage_percentage.toFixed(1) + '%';
            
            // Render charts
            renderCharts(data, coverageData);
        }
        
        loadDashboard();
    </script>
</body>
</html>
```

---

### Phase 3: Advanced Features (Priority: Low)

#### 3.1 PDF Storage & Retrieval

**Current:** PDFs generated on-demand, not stored

**Enhancement:** Store PDFs for later download

**Implementation:**
```python
# Create directory for PDF storage
PDF_STORAGE_PATH = "/home/railway/rail-data-app/pdf_reports/"

def _save_pdf_to_disk(pdf_buffer, filename):
    """Save PDF to disk and return file path."""
    os.makedirs(PDF_STORAGE_PATH, exist_ok=True)
    filepath = os.path.join(PDF_STORAGE_PATH, filename)
    
    with open(filepath, 'wb') as f:
        pdf_buffer.seek(0)
        f.write(pdf_buffer.read())
    
    return filepath

# Add to export_pdf endpoint
pdf_path = _save_pdf_to_disk(pdf_buffer, filename)

# Update database record
cursor.execute("""
    UPDATE rtis_analyses 
    SET pdf_filename = %s, pdf_stored = TRUE 
    WHERE id = %s
""", (filename, analysis_id))
```

**New Endpoint:**
```python
@app.get("/api/analyses/{analysis_id}/pdf")
def download_stored_pdf(analysis_id: int):
    """Download previously generated PDF."""
    # Query database for filename
    # Return file from disk
    pass
```

#### 3.2 Automated Alerts & Notifications

**Trigger alerts for:**
- PSR violations detected
- Brake test failures
- Excessive braking violations
- LPs overdue for analysis

**Implementation:**
```python
def _check_and_send_alerts(analysis_id, criteria, brake_tests, violations):
    """Check analysis results and send alerts if needed."""
    
    alerts = []
    
    # Check brake tests
    if brake_tests.get("feel", {}).get("status") == "FAIL":
        alerts.append({
            "type": "BRAKE_TEST_FAIL",
            "severity": "HIGH",
            "message": f"Brake feel test failed for LP {criteria['lp_name']}"
        })
    
    # Check PSR violations
    if violations["psr_count"] > 0:
        alerts.append({
            "type": "PSR_VIOLATION",
            "severity": "MEDIUM",
            "message": f"{violations['psr_count']} PSR violations detected"
        })
    
    # Send alerts (email, database notification, etc.)
    for alert in alerts:
        _send_alert_notification(alert)
```

#### 3.3 Comparative Analysis

**Feature:** Compare LP performance over time

**Queries:**
```sql
-- LP improvement trend
SELECT 
    DATE_FORMAT(working_date, '%Y-%m') as month,
    AVG(CASE WHEN brake_test_feel = 'PASS' THEN 1 ELSE 0 END) as feel_pass_rate,
    AVG(CASE WHEN brake_test_power = 'PASS' THEN 1 ELSE 0 END) as power_pass_rate,
    AVG(psr_violations_count) as avg_violations
FROM rtis_analyses
WHERE lp_hrms_id = ?
GROUP BY month
ORDER BY month;
```

#### 3.4 Excel Export for Reports

**Endpoint:**
```python
@app.get("/api/reports/export-excel")
def export_to_excel(from_date: str, to_date: str):
    """Export analyses to Excel file."""
    import xlsxwriter
    
    # Query data
    # Create Excel file
    # Return as download
    pass
```

---

## 📊 Complexity & Time Estimates

### Phase 1: Data Persistence
| Task | Complexity | Time |
|------|------------|------|
| Create database table | ⭐ Easy | 30 min |
| Implement save function | ⭐⭐ Easy-Medium | 2 hours |
| Add to export_pdf | ⭐ Easy | 30 min |
| Basic API endpoints | ⭐⭐ Easy | 2 hours |
| LP coverage logic | ⭐⭐⭐ Medium | 3 hours |
| Testing & debugging | ⭐⭐ Easy | 2 hours |
| **Total Phase 1** | | **1-2 days** |

### Phase 2: Dashboard
| Task | Complexity | Time |
|------|------------|------|
| Dashboard HTML/CSS | ⭐⭐ Easy-Medium | 4 hours |
| Data tables integration | ⭐⭐ Easy-Medium | 3 hours |
| Charts & visualizations | ⭐⭐⭐ Medium | 4 hours |
| Filters & search | ⭐⭐⭐ Medium | 3 hours |
| Responsive design | ⭐⭐ Easy-Medium | 2 hours |
| Testing & polish | ⭐⭐ Easy-Medium | 2 hours |
| **Total Phase 2** | | **2-3 days** |

### Phase 3: Advanced Features
| Task | Complexity | Time |
|------|------------|------|
| PDF storage system | ⭐⭐ Easy-Medium | 2 hours |
| Alerts system | ⭐⭐⭐ Medium | 4 hours |
| Comparative analysis | ⭐⭐⭐ Medium | 3 hours |
| Excel export | ⭐⭐ Easy-Medium | 2 hours |
| **Total Phase 3** | | **1-2 days** |

---

## 🎯 Recommended Implementation Order

### Immediate (Now)
1. ✅ Current deployment is stable
2. ✅ Users can perform analyses
3. ✅ PDFs are generated correctly

### Short Term (Next 1-2 weeks)
1. Create `rtis_analyses` table
2. Implement save-to-database on PDF export
3. Create basic "Recent Analyses" API endpoint
4. Simple table view in bbtro dashboard

**Deliverable:** Historical data starts accumulating

### Medium Term (Next month)
1. LP coverage report API
2. Full dashboard with charts
3. Filters and search functionality
4. Export to Excel

**Deliverable:** Comprehensive reporting capability

### Long Term (Future)
1. PDF storage and retrieval
2. Automated alerts
3. Comparative analysis features
4. Advanced analytics

**Deliverable:** Full-featured analytics platform

---

## 💡 Key Benefits of Future Enhancements

### For Management
- **Coverage Tracking:** Know exactly which LPs need analysis
- **Compliance Monitoring:** Track PSR/braking compliance trends
- **Resource Planning:** Identify training needs based on test results
- **Audit Trail:** Complete historical record for inspections

### For Analysts
- **Quick Reference:** Find past analyses instantly
- **Comparison:** Compare LP performance over time
- **Efficiency:** Avoid duplicate analyses

### For Division
- **Reporting:** Generate monthly/quarterly reports automatically
- **Accountability:** Clear records of who analyzed what and when
- **Quality Control:** Flag problematic analyses for review

---

## 🤔 Decision Points Before Implementation

### 1. Data Retention
**Question:** How long should we keep analysis records?

**Options:**
- Keep forever (unlimited growth)
- Archive after 2 years
- Delete after 5 years

**Recommendation:** Keep 2 years active, archive older

### 2. PDF Storage
**Question:** Should we store generated PDFs?

**Pros:**
- Can re-download without re-generating
- Complete archive of reports

**Cons:**
- Disk space (50-100 MB per PDF × hundreds = GBs)
- Backup considerations

**Recommendation:** Store for 6 months, then delete (metadata remains)

### 3. Access Control
**Question:** Who can view analytics dashboard?

**Options:**
- Division staff only
- HQ can view all divisions
- Public (anonymized data)

**Recommendation:** Division staff + HQ read access

### 4. Real-time Updates
**Question:** Should dashboard update in real-time?

**Options:**
- Auto-refresh every 30 seconds
- Manual refresh only
- WebSocket live updates

**Recommendation:** Manual refresh (simpler, sufficient)

---

## 📝 Next Steps

1. **Review this document** with stakeholders
2. **Prioritize features** based on needs
3. **Get approval** for Phase 1 (database + basic APIs)
4. **Begin implementation** step by step
5. **Test thoroughly** before deployment
6. **Deploy to production** incrementally

---

## 📞 Support & Contact

**For Questions or Issues:**
- Technical Lead: [Your Name]
- Repository: https://github.com/jaynair0405/rail-data-app
- Server: railway@crtms.in

**Documentation:**
- `FIRST_TIME_DEPLOYMENT.md` - Initial setup guide
- `SERVER_DEPLOYMENT_STRUCTURE.md` - Architecture details
- `DEPLOYMENT_VISUAL.md` - Visual diagrams
- `ENVIRONMENT_SETUP.md` - Configuration guide
- This file - Complete roadmap

---

**Document Status:** Draft - Not Committed  
**Last Updated:** December 21, 2025  
**Version:** 1.0
