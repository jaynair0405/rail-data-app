# Rail Data Analysis App — Development Guide

## Overview
A cross-platform **desktop data-analysis app** built with **Python (FastAPI + Polars + DuckDB)** for backend data handling and **HTML/JS (ECharts)** for the frontend.  
The app loads large CSVs (~60k–100k rows), filters data by criteria, exports Excel files, and generates charts and PDFs.

---

## 1. Folder structure
```
rail-data-app/
├── app.py                 # main FastAPI backend
├── data_ops.py            # Polars filtering helpers
├── storage.py             # (placeholder for persistence later)
├── ui/
│   └── index.html         # front-end UI
├── requirements.txt
└── app-data/              # created at runtime (e.g., parquet)
```

---

## 2. Virtual environment setup (macOS)
```bash
cd ~/Desktop
mkdir rail-data-app && cd rail-data-app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

---

## 3. Install dependencies
```bash
pip install fastapi "uvicorn[standard]" polars pandas xlsxwriter pyarrow python-multipart
```

`requirements.txt`
```
fastapi
uvicorn[standard]
polars
duckdb
pandas
xlsxwriter
pyarrow
python-multipart
```

---

## 4. `app.py` — core backend
Current behavior highlights:
- `/load_csv` uploads the raw file, keeps the original headers, converts known bad tokens (`NULL`, etc.) to nulls, and auto-repairs rows where GPS timestamps dropped and pushed every column left.
- `/preview`, `/analyze`, `/export`, `/chart_data` all run on the in-memory Polars DataFrame, and use header matching (not column indices) so uploads with shuffled order still work.
- Train metadata is hydrated from `base-data/train_with_from_to_stations*.csv` and geofences from `geo locations - Sheet1.csv`, enabling `/train_info` plus the geofence-aware start/end slicing logic.
- Filtering honors direction, optional start/end station overrides, and supports Excel export + chart aggregation. Destination slicing now prefers the first `<1 km/h` sample inside the end geofence (falling back to the slowest sample if the train never stops) so charts always run down to a true halt.
- `/debug/base_data` exposes the loader status, while `/train_info` is used by the UI before running analysis.

Refer to `app.py` for the full FastAPI implementation; it now weighs in at ~1100 lines with the geofence helpers, CSV cleaning utilities, and intelligent route selection logic.

### Data cleaning & alignment
1. **Null token handling**: `pl.read_csv` maps `NULL/Null/null` (and similar future tokens) to proper nulls so numeric casts succeed.
2. **Column shift repair**: if the GPS timestamp column vanishes mid-file (device bug), the loader detects rows where `distFromSpeed` suddenly contains a station code while the station column is empty. For those rows we shift each value back to its rightful column (GPS time ← logging time, latitude ← GPS time, longitude ← latitude, etc.) so downstream calculations see consistent schemas.
3. **Telemetry pruning**: `BE/GUI/ODU Version` + `DB Circle/Polygon Count` are parsed as strings (to avoid `0.0.0` float errors) and dropped immediately since they aren’t used anywhere else.
4. **Type recovery**: distance/speed columns stay as strings in memory until filters/export phases cast them with `strict=False`. That way bad tokens become nulls without aborting the request.

### Geofence-aware slicing
- Base geofence coordinates (100 m tolerance) are loaded once at startup. When a user picks a train, `/train_info` resolves direction/from/to stations.
- During `/analyze` or `/chart_data`, the server locates the first row where the train leaves the origin geofence (speed < 1 → speed > 0) and the first row where it stops inside the destination geofence (speed < 1). Only the rows between those indices are used for filtering, export, and charting, eliminating long dwell periods.
- If a geofence is missing or misaligned, the code falls back to station-header cues: start index becomes the first matching station row, and the end index becomes the first matching station row where speed < 1 (else the last occurrence). This keeps output bounded even when device logs don’t hit the provided geofence coordinates.

### Speed restrictions overlay
- `all_section_psr.csv` (route-level PSR list) is loaded alongside the other base data. Restrictions are filtered by route (e.g., `CSMT-PUNE`) and direction (`UP`, `DN`, or `BOTH`) for each request; sub-routes (e.g., `IGP-CSMT`, `MMR-CSMT`) automatically alias to their parent corridors so existing PSR definitions (like `JL-CSMT`) still apply.
- For every restriction, the backend finds the first telemetry point near its `from_lat/from_lon` and the first point near `to_lat/to_lon` (within ~120 m). All samples between those indices inherit the restriction’s `speed_limit_kmph` (taking the most restrictive limit when overlaps occur).
- When the chart aggregates data per minute, it also aggregates the lowest applicable restriction in that minute and returns contiguous spans. The UI renders those as translucent bands beneath the speed line so any overspeeding is obvious wherever the line climbs above the shaded area.
- **Important**: The CSV loader silently skips malformed rows (missing `restriction_id` or invalid coordinates). Ensure all PSR entries have proper sequential IDs to avoid gaps in restriction coverage (e.g., PNVL-KJT restrictions use IDs 333-334).

### Maximum Permissible Speed (MPS) overlay
- A static `MPS_CONFIG` maps each route section (e.g., `CSMT-KYN`, `KYN-KJT`) to its maximum permissible speed. `ROUTE_SECTION_MAP` describes which sections apply to a route/direction pair (CSMT→PUNE DN, PUNE→CSMT UP, etc.).
- Using station coordinates from `geo locations - Sheet1.csv`, the backend matches each section's `from`/`to` stations to telemetry indices and assigns the section's MPS to all rows between those points (maintaining continuity even when some coordinates are missing by forward-filling).
- `/chart_data` now returns both the PSR shading and an `mps` series; the UI plots the MPS curve as a step line so violations are immediately visible when the actual speed crosses above it.

### Intelligent route variant selection
When multiple route variants exist for the same origin-destination pair (e.g., CSMT-PUNE via KYN vs via PNVL), the system intelligently selects the correct route using **sequence-aware matching**:

1. **Station data quality check**: Before applying smart matching, the system verifies that >50% of telemetry rows have valid (non-NULL) station codes. If station data is poor quality, it falls back to GPS coordinate matching only.

2. **Sequence extraction**: The actual station sequence is extracted from the telemetry's station code column (e.g., `[CSMT, DR, DIVA, PNVL, KJT, PUNE]`).

3. **Route scoring** (`_score_route_match` function): Each candidate route is scored based on how well its expected stations match the actual sequence:
   - **+2.0 points**: Station found in correct sequential order
   - **+0.5 points**: Station exists but appears out of order
   - **-1.0 point**: Expected station missing from actual data

4. **Best match selection**: The route variant with the highest score is chosen. For example:
   - Route via KYN: `[CSMT, KYN, KJT, PUNE]` → Score: 5.0 (KYN missing = -1)
   - Route via PNVL: `[CSMT, DIVA, PNVL, KJT, PUNE]` → Score: 10.0 (all in order) ✓ **Winner**

5. **Handles round trips**: Since filtering already slices data by from/to stations, the sequence matching naturally handles trains that travel the same route in both directions within a single CSV.

This ensures MPS and PSR overlays always reflect the train's **actual path**, regardless of the order routes are defined in `ROUTE_SECTION_MAP`.

---

## 5. `ui/index.html`
```html
<!doctype html>
<meta charset="utf-8" />
<title>Rail Data App</title>
<style>
  body{font-family:system-ui,Arial;padding:16px;max-width:1000px;margin:auto}
  label{margin-right:12px}
  pre{background:#f6f6f6;padding:8px;white-space:pre-wrap}
  .row{margin:10px 0}
</style>

<h2>Rail Data App</h2>

<div class="row">
  <input type="file" id="csv" accept=".csv" />
  <button id="btnUpload">Load CSV</button>
</div>

<div class="row">
  <label>Station contains: <input id="station" /></label>
  <label>Direction equals: <input id="direction" placeholder="UP/DOWN" /></label>
  <label>Speed &lt; <input id="speed" type="number" step="0.1" /></label>
</div>

<div class="row">
  <button id="btnAnalyze">Analyze</button>
  <button id="btnExport">Export Excel</button>
</div>

<div id="chart" style="width:100%;height:360px;margin-top:10px;border:1px solid #eee"></div>
<pre id="out"></pre>

<script src="https://cdn.jsdelivr.net/npm/echarts@5"></script>
<script>
const API = "";

function getCriteria(){
  return {
    station_code_contains: document.getElementById("station").value || undefined,
    direction_equals: document.getElementById("direction").value || undefined,
    speed_lt: document.getElementById("speed").value || undefined,
  };
}

document.getElementById("btnUpload").onclick = async () => {
  const f = document.getElementById("csv").files[0];
  if(!f){ alert("Choose a CSV first"); return; }
  const fd = new FormData(); fd.append("file", f);
  const r = await fetch(`${API}/load_csv`, { method:"POST", body: fd });
  const j = await r.json();
  document.getElementById("out").textContent = "Loaded:\n" + JSON.stringify(j,null,2);
};

document.getElementById("btnAnalyze").onclick = async () => {
  const r = await fetch(`${API}/analyze`, {
    method:"POST",
    headers: { "Content-Type":"application/json" },
    body: JSON.stringify(getCriteria())
  });
  const j = await r.json();
  document.getElementById("out").textContent = "Analyze result:\n" + JSON.stringify(j,null,2);
  await drawChart();
};

document.getElementById("btnExport").onclick = async () => {
  const r = await fetch(`${API}/export`, {
    method:"POST",
    headers: { "Content-Type":"application/json" },
    body: JSON.stringify(getCriteria())
  });
  if(!r.ok){ const t = await r.text(); alert("Export failed:\n" + t); return; }
  const blob = await r.blob();
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "filtered.xlsx";
  a.click();
  URL.revokeObjectURL(a.href);
};

async function drawChart(){
  const r = await fetch(`/chart_data`, {
    method:"POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(getCriteria())
  });
  const j = await r.json();
  const el = document.getElementById("chart");
  const chart = echarts.init(el);
  chart.setOption({
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 20, top: 30, bottom: 40 },
    xAxis: { type: 'category', data: j.labels, name: 'Time' },
    yAxis: { type: 'value', name: j.yLabel || 'Speed' },
    series: [{ name: 'Speed', type: 'line', smooth: true, data: j.values }]
  });
}
</script>
```

---

## 6. Run locally
```bash
uvicorn app:app --port 8765
```
Open → http://127.0.0.1:8765/  

1. **Load CSV**  
2. **Analyze** (filter preview)  
3. **Export Excel**  
4. **Chart** updates automatically.

---

## 7. Packaging (later)
When ready to share:
1. Add a GitHub Actions workflow (Windows build) using PyInstaller.  
2. Users download `.exe` artifact → run directly.  
3. For local packaging:
   ```bash
   pip install pyinstaller
   pyinstaller --noconfirm --onefile --windowed app.py
   ```

---

## 8. Next milestones
1. Add **combo chart** (bars + line for distance markers).  
2. Implement **multi-page PDF generation** (Playwright or ReportLab).  
3. Add **DuckDB persistence layer** for base + analysis data.  
4. Package via GitHub Actions to create a portable `.exe`.

---

## 9. Upcoming Enhancements
- **Zero-speed anchors**: tighten both start/end detection to explicitly require a `<1 km/h` sample at the geofence so mixed-direction CSVs always slice on the real departure/arrival row (the backend already extends the end slice; start-side logic will get the same guard).
- **Section-wise visuals**: besides the current “full run” chart, generate additional per-section plots (e.g., `PUNE-LNL`, `LNL-KJT`, `KJT-KYN`, `KYN-CSMT`) using the route graph so analysts can zoom into problematic blocks while keeping the overview.
- **UI polish (next step)**: expose the new section charts behind toggles/tabs in `ui/index.html` once the backend endpoints land, plus surface which segment is currently in view.
