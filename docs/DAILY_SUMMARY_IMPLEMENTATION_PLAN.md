# Daily Summary CSV - Implementation Plan

## Overview

This document outlines the implementation plan for generating daily summary CSV reports that track:
- **Working** locos (auto-generated from saved analyses)
- **SIM Down** locos (manual entry)
- **NON RTIS** locos (manual entry)
- **BFT/BPT status** (auto-detected from brake test results)

---

## Target CSV Format

```csv
SR NO.,Date Of Working,RTIS Status,Train Number,Loco Number,From,To,Dep.,Arr.,LP NAME,ALP,NCLI,Analyzed By,BFT Done,BPT Done
1,22/01/2026,SIM Down,20103,30667,LTT,IGP,,,Rabindra Choudhary,,V S R SARMA,------,,
2,22/01/2026,Working,12165,39033,LTT,IGP,,,Ajay Kumar Maurya,,S V OHOL,S V OHOL,Done,Done
3,22/01/2026,NON RTIS,11059,37333,LTT,IGP,,,KC Roy,,D N PARGHI,------,,
```

### Column Definitions

| Column | Source | Description |
|--------|--------|-------------|
| SR NO. | Auto-generated | Sequential number |
| Date Of Working | `working_date` | Date of train operation |
| RTIS Status | Auto/Manual | Working, SIM Down, NON RTIS |
| Train Number | `train_number` | Train number |
| Loco Number | `loco_number` | Locomotive number |
| From | `from_station` | Origin station |
| To | `to_station` | Destination station |
| Dep. | Manual | Departure time (optional) |
| Arr. | Manual | Arrival time (optional) |
| LP NAME | `lp_name` | Loco Pilot name |
| ALP | `alp_name` | Assistant Loco Pilot name |
| NCLI | `ncli_name` | CLI name from daily order |
| Analyzed By | `analyzed_by` | Who analyzed (for Working) or "------" |
| BFT Done | Auto-detected | "Done" if Brake Feel Test PASS |
| BPT Done | Auto-detected | "Done" if Brake Power Test PASS |

---

## Phase 1: Database Schema Changes

### 1.0 Create `div_cr_locos` table for Loco Management

```sql
-- Main locos table
CREATE TABLE IF NOT EXISTS div_cr_locos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loco_number VARCHAR(20) NOT NULL UNIQUE,
    loco_type VARCHAR(20),                -- WAP7, WAG9H, WAP4, WCAM2, WCAM3, WAG7, etc.
    current_shed VARCHAR(20),             -- AQE, BSLL, DNDE, KYNE, PADX
    status ENUM('Active', 'Transferred Out', 'Condemned') DEFAULT 'Active',
    commission_date DATE,
    remarks VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_shed (current_shed),
    INDEX idx_status (status),
    INDEX idx_loco_type (loco_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Shed master table
CREATE TABLE IF NOT EXISTS div_cr_sheds (
    id INT AUTO_INCREMENT PRIMARY KEY,
    shed_code VARCHAR(10) NOT NULL UNIQUE,
    shed_name VARCHAR(100),
    zone VARCHAR(20) DEFAULT 'CR',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Insert default sheds
INSERT INTO div_cr_sheds (shed_code, shed_name) VALUES
('AQE', 'Ajni Electric Loco Shed'),
('BSLL', 'Bhusawal Electric Loco Shed'),
('DNDE', 'Danapur Electric Loco Shed'),
('KYNE', 'Kalyan Electric Loco Shed'),
('PADX', 'Pune Electric Loco Shed');

-- Loco transfer history table
CREATE TABLE IF NOT EXISTS div_cr_loco_transfers (
    id INT AUTO_INCREMENT PRIMARY KEY,
    loco_number VARCHAR(20) NOT NULL,
    from_shed VARCHAR(20),
    to_shed VARCHAR(20),
    transfer_date DATE,
    transfer_type ENUM('Internal', 'Incoming', 'Outgoing') DEFAULT 'Internal',
    remarks VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    INDEX idx_loco (loco_number),
    INDEX idx_date (transfer_date),
    INDEX idx_from_shed (from_shed),
    INDEX idx_to_shed (to_shed)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

**Initial Data Load:** Import from cr_locos.csv (959 locos)

**Sheds in CSV:**
| Shed Code | Count |
|-----------|-------|
| AQE       | 313   |
| BSLL      | 277   |
| DNDE      | 49    |
| KYNE      | 232   |
| PADX      | 88    |

**Loco Types in CSV:**
- WAP7, WAP5, WAP4 (Passenger)
- WAG9H, WAG7, WAG5TAOCHI, WAG5P (Goods)
- WCAM2, WCAM3, WCAG1 (AC/DC Dual)
- WCM6 (DC Motor)

```sql
-- Example import (run via Python script)
-- LOAD DATA INFILE 'cr_locos.csv' INTO TABLE div_cr_locos ...
```

### 1.1 Add BFT/BPT columns to `div_rtis_analyses`

```sql
-- Add brake test status columns
ALTER TABLE div_rtis_analyses
ADD COLUMN bft_status ENUM('PASS', 'FAIL', 'NOT RUN') DEFAULT 'NOT RUN' AFTER ncli_alp_name;

ALTER TABLE div_rtis_analyses
ADD COLUMN bpt_status ENUM('PASS', 'FAIL', 'NOT RUN') DEFAULT 'NOT RUN' AFTER bft_status;

-- Add index for faster queries
CREATE INDEX idx_bft_status ON div_rtis_analyses(bft_status);
CREATE INDEX idx_bpt_status ON div_rtis_analyses(bpt_status);
```

### 1.2 Create `div_rtis_daily_entries` table for SIM Down / NON RTIS

```sql
CREATE TABLE IF NOT EXISTS div_rtis_daily_entries (
    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Date and Status
    working_date DATE NOT NULL,
    rtis_status ENUM('SIM Down', 'NON RTIS') NOT NULL,

    -- Train/Loco Details
    train_number VARCHAR(20) NOT NULL,
    loco_number VARCHAR(20) NOT NULL,
    from_station VARCHAR(50),
    to_station VARCHAR(50),
    departure_time TIME,
    arrival_time TIME,

    -- Crew Details
    lp_name VARCHAR(100),
    lp_hrms_id VARCHAR(20),
    alp_name VARCHAR(100),
    alp_hrms_id VARCHAR(20),
    ncli_name VARCHAR(100),

    -- Metadata
    entered_by VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    -- Indexes
    INDEX idx_working_date (working_date),
    INDEX idx_rtis_status (rtis_status),
    INDEX idx_loco_number (loco_number),
    INDEX idx_train_number (train_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

### 1.3 Create `div_rtis_sim_down_weekly` table for weekly tracking

```sql
CREATE TABLE IF NOT EXISTS div_rtis_sim_down_weekly (
    id INT AUTO_INCREMENT PRIMARY KEY,

    -- Week Reference
    week_start_date DATE NOT NULL,
    week_end_date DATE NOT NULL,

    -- Loco Details
    loco_number VARCHAR(20) NOT NULL,
    sim_down_count INT DEFAULT 0,

    -- Dates when SIM was down
    sim_down_dates JSON,

    -- Report metadata
    report_generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_to_officers BOOLEAN DEFAULT FALSE,
    sent_at TIMESTAMP,

    INDEX idx_week_dates (week_start_date, week_end_date),
    INDEX idx_loco_number (loco_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
```

---

## Phase 1B: Loco Management API Endpoints

### 1B.1 Get All Locos (with filters)

```
GET /api/locos?shed=KYNE&status=Active&type=WAP7
```

**Response:**
```json
{
    "total": 232,
    "locos": [
        {
            "id": 1,
            "loco_number": "30074",
            "loco_type": "WAP5",
            "current_shed": "KYNE",
            "status": "Active",
            "commission_date": null,
            "remarks": null
        }
    ]
}
```

### 1B.2 Add New Loco

```
POST /api/locos
```

**Request Body:**
```json
{
    "loco_number": "39550",
    "loco_type": "WAP7",
    "current_shed": "KYNE",
    "commission_date": "2026-01-15",
    "remarks": "New commission"
}
```

### 1B.3 Update Loco

```
PUT /api/locos/{loco_number}
```

### 1B.4 Transfer Loco

```
POST /api/locos/{loco_number}/transfer
```

**Request Body:**
```json
{
    "to_shed": "AQE",
    "transfer_date": "2026-01-28",
    "transfer_type": "Internal",
    "remarks": "Transferred for maintenance"
}
```

**Logic:**
1. Update `current_shed` in `div_cr_locos`
2. Insert record in `div_cr_loco_transfers`

### 1B.5 Mark Loco as Transferred Out / Condemned

```
PUT /api/locos/{loco_number}/status
```

**Request Body:**
```json
{
    "status": "Transferred Out",
    "remarks": "Transferred to SCR"
}
```

### 1B.6 Get Loco Transfer History

```
GET /api/locos/{loco_number}/history
```

### 1B.7 Get All Sheds

```
GET /api/sheds
```

### 1B.8 Import Locos from CSV (One-time)

```
POST /api/locos/import
```

Upload cr_locos.csv to bulk import.

---

## Phase 2: Backend API Changes

### 2.1 Update `save_analysis` endpoint

**File:** `app.py`

**Changes:**
- Compute BFT and BPT status from brake test results
- Store in `bft_status` and `bpt_status` columns

```python
# In save_analysis function, after computing brake tests:
brake_tests = _brake_tests(df, start_station, direction)

bft_status = 'NOT RUN'
bpt_status = 'NOT RUN'

if brake_tests:
    if brake_tests.get('feel', {}).get('status') == 'PASS':
        bft_status = 'PASS'
    elif brake_tests.get('feel', {}).get('status') == 'FAIL':
        bft_status = 'FAIL'

    if brake_tests.get('power', {}).get('status') == 'PASS':
        bpt_status = 'PASS'
    elif brake_tests.get('power', {}).get('status') == 'FAIL':
        bpt_status = 'FAIL'

# Add to INSERT query
```

### 2.2 New API Endpoints

#### 2.2.1 Manual Entry - Add SIM Down / NON RTIS

```
POST /api/daily-entry
```

**Request Body:**
```json
{
    "working_date": "2026-01-22",
    "rtis_status": "SIM Down",
    "train_number": "20103",
    "loco_number": "30667",
    "from_station": "LTT",
    "to_station": "IGP",
    "departure_time": null,
    "arrival_time": null,
    "lp_name": "Rabindra Choudhary",
    "lp_hrms_id": "",
    "alp_name": "",
    "alp_hrms_id": "",
    "ncli_name": "V S R SARMA",
    "entered_by": "operator_name"
}
```

#### 2.2.2 Get Daily Entries

```
GET /api/daily-entries?date=2026-01-22
```

**Response:**
```json
{
    "entries": [
        {
            "id": 1,
            "working_date": "2026-01-22",
            "rtis_status": "SIM Down",
            "train_number": "20103",
            "loco_number": "30667",
            ...
        }
    ]
}
```

#### 2.2.3 Update Daily Entry

```
PUT /api/daily-entry/{id}
```

#### 2.2.4 Delete Daily Entry

```
DELETE /api/daily-entry/{id}
```

#### 2.2.5 Export Daily Summary CSV

```
GET /api/export-daily-summary?date=2026-01-22
```

**Logic:**
1. Query `div_rtis_analyses` for Working entries (where `working_date = date`)
2. Query `div_rtis_daily_entries` for SIM Down / NON RTIS entries
3. Combine and sort by train number or serial order
4. Generate CSV with all columns

**Response:** CSV file download

#### 2.2.6 Get Combined Daily Summary (for UI)

```
GET /api/daily-summary?date=2026-01-22
```

**Response:**
```json
{
    "date": "2026-01-22",
    "summary": {
        "total": 14,
        "working": 9,
        "sim_down": 3,
        "non_rtis": 2
    },
    "entries": [
        {
            "sr_no": 1,
            "working_date": "22/01/2026",
            "rtis_status": "Working",
            "train_number": "12165",
            "loco_number": "39033",
            "from_station": "LTT",
            "to_station": "IGP",
            "lp_name": "Ajay Kumar Maurya",
            "alp_name": "",
            "ncli_name": "S V OHOL",
            "analyzed_by": "S V OHOL",
            "bft_done": "Done",
            "bpt_done": "Done",
            "source": "analysis"
        },
        {
            "sr_no": 2,
            "working_date": "22/01/2026",
            "rtis_status": "SIM Down",
            "train_number": "20103",
            "loco_number": "30667",
            ...
            "analyzed_by": "------",
            "bft_done": "",
            "bpt_done": "",
            "source": "manual"
        }
    ]
}
```

#### 2.2.7 Weekly SIM Down Report

```
GET /api/sim-down-weekly-report?week_start=2026-01-20&week_end=2026-01-26
```

**Response:**
```json
{
    "week_start": "2026-01-20",
    "week_end": "2026-01-26",
    "locos": [
        {
            "loco_number": "30667",
            "sim_down_count": 3,
            "dates": ["2026-01-20", "2026-01-22", "2026-01-24"]
        },
        {
            "loco_number": "37194",
            "sim_down_count": 2,
            "dates": ["2026-01-21", "2026-01-23"]
        }
    ]
}
```

---

## Phase 3: UI Implementation

### 3.1 New Page: `daily-summary.html`

**Features:**
1. **Date Selector** - Pick date to view/edit summary
2. **Summary Statistics** - Show counts (Working, SIM Down, NON RTIS)
3. **Combined Table** - Show all entries for the date
4. **Manual Entry Form** - Add SIM Down / NON RTIS entries
5. **Export Button** - Download CSV

**Layout:**
```
+----------------------------------------------------------+
|  Daily Summary Report                     [Date Picker]   |
+----------------------------------------------------------+
|  Statistics:                                              |
|  [Working: 9] [SIM Down: 3] [NON RTIS: 2] [Total: 14]    |
+----------------------------------------------------------+
|  [+ Add SIM Down Entry]  [+ Add NON RTIS Entry]          |
+----------------------------------------------------------+
|  SR | Date | Status | Train | Loco | From | To | LP |... |
|  1  | 22/01| Working| 12165 | 39033| LTT  | IGP| ...|... |
|  2  | 22/01| SIM Dwn| 20103 | 30667| LTT  | IGP| ...|... |
|  ...                                                      |
+----------------------------------------------------------+
|  [Export CSV]  [Print]                                    |
+----------------------------------------------------------+
```

### 3.2 Manual Entry Modal

**Fields:**
- Working Date (pre-filled with selected date)
- RTIS Status (dropdown: SIM Down / NON RTIS)
- Train Number (with autocomplete from train list)
- Loco Number (with autocomplete from cr_locos.csv)
- From Station
- To Station
- Departure Time (optional)
- Arrival Time (optional)
- LP Name
- LP HRMS ID (optional)
- ALP Name (optional)
- ALP HRMS ID (optional)
- NCLI Name

### 3.3 Weekly SIM Down Report Page: `sim-down-weekly.html`

**Features:**
1. **Week Selector** - Auto-calculate week (Mon-Sun)
2. **Loco-wise breakdown** - List of locos with SIM down days
3. **Export for Officers** - Generate report

### 3.4 Loco Management Page: `loco-management.html`

**Features:**
1. **Loco List Table** - Searchable, sortable, filterable
2. **Filter Options:**
   - By Shed (AQE, BSLL, DNDE, KYNE, PADX)
   - By Status (Active, Transferred Out, Condemned)
   - By Loco Type (WAP7, WAG9H, WAP4, WCAM2, WCAM3, WAG7, etc.)
3. **Actions:**
   - Add New Loco
   - Edit Loco Details
   - Transfer Loco (with history)
   - Change Status (Transferred Out / Condemned)
   - View Transfer History
4. **Bulk Import** - One-time import from CSV

**Layout:**
```
+----------------------------------------------------------+
|  CR Loco Management                                       |
+----------------------------------------------------------+
|  Filters: [Shed ▼] [Status ▼] [Type ▼] [Search...]       |
+----------------------------------------------------------+
|  [+ Add Loco]  [Import CSV]                              |
+----------------------------------------------------------+
|  # | Loco No | Type  | Shed | Status | Actions          |
|  1 | 30074   | WAP5  | KYNE | Active | [Edit][Transfer] |
|  2 | 30078   | WAP5  | KYNE | Active | [Edit][Transfer] |
|  ...                                                      |
+----------------------------------------------------------+
|  Showing 1-50 of 959 | [◀ Prev] [Next ▶]                 |
+----------------------------------------------------------+
```

**Add/Edit Loco Modal:**
- Loco Number (required, unique)
- Loco Type (dropdown)
- Current Shed (dropdown from div_cr_sheds)
- Status (Active / Transferred Out / Condemned)
- Commission Date (optional)
- Remarks (optional)

**Transfer Loco Modal:**
- From Shed (read-only, current)
- To Shed (dropdown)
- Transfer Date
- Transfer Type (Internal / Incoming / Outgoing)
- Remarks

**Transfer History View:**
- Date | From | To | Type | Remarks

---

## Phase 4: Backfill Existing Data

### 4.1 Update existing analyses with BFT/BPT status

```python
# Script to backfill brake test status for existing records
# This would need to re-analyze each saved analysis file
# or update based on stored brake test results if available
```

**Option A:** If brake test results are stored in analysis JSON
- Parse stored results and update bft_status, bpt_status

**Option B:** If not stored
- Set all existing records to 'NOT RUN' (conservative approach)
- New analyses will have correct status going forward

---

## Phase 5: Weekly Report Automation (Optional)

### 5.1 Scheduled Task

Every Friday at 5 PM:
1. Query SIM Down entries for the week (Mon-Sun)
2. Group by loco_number
3. Generate report
4. Store in `div_rtis_sim_down_weekly`
5. (Optional) Send email notification

---

## Implementation Order

### Step 1: Database Setup - Loco Management
- [ ] Create div_cr_locos table
- [ ] Create div_cr_sheds table (with default data)
- [ ] Create div_cr_loco_transfers table
- [ ] Import data from cr_locos.csv

### Step 2: Backend - Loco Management APIs
- [ ] GET /api/locos (with filters)
- [ ] POST /api/locos (add new)
- [ ] PUT /api/locos/{loco_number} (update)
- [ ] POST /api/locos/{loco_number}/transfer
- [ ] PUT /api/locos/{loco_number}/status
- [ ] GET /api/locos/{loco_number}/history
- [ ] GET /api/sheds
- [ ] POST /api/locos/import (CSV import)

### Step 3: UI - Loco Management Page
- [ ] Create loco-management.html
- [ ] Loco list table with pagination
- [ ] Filter by shed, status, type
- [ ] Add/Edit loco modal
- [ ] Transfer loco modal
- [ ] Transfer history view

### Step 4: Database Setup - Daily Summary
- [ ] Add bft_status, bpt_status columns to div_rtis_analyses
- [ ] Create div_rtis_daily_entries table
- [ ] Create div_rtis_sim_down_weekly table

### Step 5: Backend - Save Analysis Update
- [ ] Modify save_analysis to compute and store BFT/BPT status

### Step 6: Backend - Manual Entry APIs
- [ ] POST /api/daily-entry
- [ ] GET /api/daily-entries
- [ ] PUT /api/daily-entry/{id}
- [ ] DELETE /api/daily-entry/{id}

### Step 7: Backend - Summary APIs
- [ ] GET /api/daily-summary
- [ ] GET /api/export-daily-summary (CSV download)

### Step 8: UI - Daily Summary Page
- [ ] Create daily-summary.html
- [ ] Date picker and statistics
- [ ] Combined entries table
- [ ] Manual entry form/modal (with loco autocomplete from div_cr_locos)
- [ ] CSV export button

### Step 9: Backend - Weekly Report API
- [ ] GET /api/sim-down-weekly-report

### Step 10: UI - Weekly Report Page
- [ ] Create sim-down-weekly.html

### Step 11: Navigation Update
- [ ] Add links in index.html sidebar
- [ ] Add menu items for new pages

---

## Data Flow Diagram

```
+------------------+                      +------------------+
|  CR Loco Master  |                      |   Daily Order    |
|  (div_cr_locos)  |                      | (Train Schedule) |
+--------+---------+                      +--------+---------+
         |                                         |
         | (autocomplete)           +--------------+--------------+
         |                          |              |              |
         v                          v              v              v
    +---------+               +---------+    +---------+    +-----------+
    | Loco    |               | Working |    |SIM Down |    | NON RTIS  |
    | Mgmt UI |               |(Analyzed|    | (Manual)|    | (Manual)  |
    +---------+               +---------+    +---------+    +-----------+
         |                          |              |              |
         v                          v              v              v
+------------------+          +----------+   +--------------------+
|div_cr_loco_      |          |div_rtis_ |   |div_rtis_daily_     |
|transfers         |          |analyses  |   |entries             |
|(history)         |          +----------+   +--------------------+
+------------------+                |              |
                                    +--------------+
                                           |
                                           v
                                  +------------------+
                                  | Daily Summary    |
                                  | (Combined View)  |
                                  +------------------+
                                           |
                          +----------------+----------------+
                          |                                 |
                          v                                 v
                  +---------------+                +------------------+
                  | CSV Export    |                | Weekly SIM Down  |
                  +---------------+                | Report           |
                                                   +------------------+
```

---

## Questions to Resolve

1. **Sorting Order:** Should daily summary entries be sorted by:
   - Train number?
   - Time of departure?
   - Entry order?

2. **Duplicate Detection:** What happens if same train/loco is entered twice for same date?
   - Allow duplicates?
   - Warn and prevent?

3. **Edit Working Entries:** Can users modify "Working" entries from daily summary page?
   - Or should they use the main analysis page?

4. **Historical Data:** Should we backfill BFT/BPT for past analyses?

5. **Loco Autocomplete:** ~~Use cr_locos.csv for suggestions?~~ **RESOLVED** - Use div_cr_locos table

6. **Train Autocomplete:** Use train_with_from_to_stations.csv or create a table?

7. **Loco Types:** Should loco types be a master table or fixed ENUM?
   - Current types from CSV: WAP7, WAG9H, WAP4, WCAM2, WCAM3, WAG7, WAG5TAOCHI, WAG5P, WCM6, WCAG1, WAP5

---

## File Changes Summary

| File | Changes |
|------|---------|
| `app.py` | Add 15 new endpoints, modify save_analysis |
| `ui/loco-management.html` | New page - Loco CRUD, transfer, history |
| `ui/daily-summary.html` | New page - Daily summary with manual entry |
| `ui/sim-down-weekly.html` | New page - Weekly SIM Down report |
| `ui/index.html` | Add navigation links |
| `sql/create_loco_tables.sql` | New SQL file for loco management |
| `sql/create_daily_summary_tables.sql` | New SQL file for daily summary |

---

## Estimated Components

- **Database:** 6 tables
  - `div_cr_locos` (new)
  - `div_cr_sheds` (new)
  - `div_cr_loco_transfers` (new)
  - `div_rtis_analyses` (alter - add bft_status, bpt_status)
  - `div_rtis_daily_entries` (new)
  - `div_rtis_sim_down_weekly` (new)
- **API Endpoints:** 15 new endpoints
  - Loco Management: 8 endpoints
  - Daily Summary: 5 endpoints
  - Weekly Report: 1 endpoint
  - Save Analysis: 1 modification
- **UI Pages:** 3 new pages
- **SQL Scripts:** 2 new files

---

*Document Created: 2026-01-26*
*Last Updated: 2026-01-28*
*Status: Awaiting Review*

---

## Quick Reference - Implementation Phases

| Phase | Component | Tables | APIs | UI Pages |
|-------|-----------|--------|------|----------|
| 1 | Loco Management | 3 | 8 | 1 |
| 2 | Daily Summary | 3 | 6 | 1 |
| 3 | Weekly Report | - | 1 | 1 |
| **Total** | | **6** | **15** | **3** |
