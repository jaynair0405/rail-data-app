# SPM / RTIS Web Integration – Complete Technical Guide

## 1. Project Aim & Context

### Objective
To integrate an existing **Python-based RTIS / SPM analysis application** into the **bbtro (Node.js + MySQL) web platform**, so that:

- All SPM / RTIS tools are accessed **from within bbtro**
- Authentication is **shared** (single login)
- Python apps run as **web services**, not desktop executables
- Multiple SPM/RTIS modules (5–6 offices) can be accessed via a **Hub page**
- Analysis results can later be **stored in MySQL** and surfaced in bbtro dashboards (OTS, reports, etc.)

### Key Design Principle
> Python apps are never “launched” directly.  
> They are exposed as **web services** and opened like normal web pages through bbtro.

---

## 2. High-Level Architecture

Browser  
→ bbtro (Node + Express + MySQL)  
→ `/spm/rtis/*` (reverse proxy)  
→ rail-data-app (FastAPI + Python)

Key points:
- Single domain (cookies shared)
- bbtro owns authentication
- FastAPI reads bbtro sessions from MySQL
- Python ports are never exposed directly

---

## 3. rail-data-app (Python / FastAPI)

### Purpose
Analyze RTIS CSV data to evaluate:
- Speed profiles
- PSR compliance
- Braking patterns
- Brake feel / brake power tests

### Base Data (CSV)
Located in `base-data/`:
- all_section_psr.csv
- geo locations - Sheet1.csv
- main_stations.csv
- route_graph.csv
- train_with_from_to_stations.csv

---

## 4. Authentication Integration

### How bbtro Auth Works
- express-session
- Sessions stored in MySQL (`sessions` table)
- Cookie name: `connect.sid`
- Format: `s:<session_id>.<signature>`

### FastAPI Strategy
1. Read `connect.sid`
2. URL-decode
3. Extract `session_id`
4. Query `sessions` table
5. Parse JSON in `data`
6. Extract `user`
7. Enforce `users.can_access_rtis`

---

## 5. rail-data-app Fixes & Changes

### Cookie Parsing
```python
from urllib.parse import unquote

def extract_session_id_from_connect_sid(connect_sid):
    if not connect_sid:
        return None
    decoded = unquote(connect_sid)
    if decoded.startswith("s:"):
        decoded = decoded[2:]
    return decoded.split(".", 1)[0]
```

### Session Expiry Fix
`sessions.expires` is stored in seconds.

```python
expires_sec = int(row["expires"])
now_sec = int(datetime.now().timestamp())
if now_sec > expires_sec:
    return None
```

### MySQL Configuration (.env)
```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=jay
MYSQL_PASSWORD=*****
```

### API Base Prefix (UI)
```js
const API = "/spm/rtis";
```

### Protected Entry Route
```python
@app.get("/rtis")
def rtis_home(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/")
    return RedirectResponse(url="/spm/rtis/ui/")
```

---

## 6. bbtro Changes

### SPM Hub Page
Created:
`public/div/spm-hub.html`

### Sidebar Link
```html
href="/div/spm-hub.html"
```

### Protect /div Pages
```js
app.use("/div", (req, res, next) => {
  const user = req.session?.user;
  if (!user) return res.redirect("/");
  if (user.realm !== "division") return res.redirect("/");
  next();
});
```

### Reverse Proxy
```js
app.use(
  "/spm/rtis",
  createProxyMiddleware({
    target: "http://localhost:8765",
    changeOrigin: true,
    ws: true,
    pathRewrite: { "^/spm/rtis": "" },
  })
);
```

### Correct Entry URL
`/spm/rtis/rtis`

---

## 7. Verified Flow

1. Login via bbtro
2. Session stored in MySQL
3. Open SPM Hub
4. Click RTIS
5. Proxy forwards request
6. FastAPI validates session
7. RTIS UI loads
8. API calls succeed via prefixed paths

---

## 8. Current Status

- RTIS fully integrated
- Auth enforced
- Hub architecture ready
- Proxy stable
- Ready for DB persistence

---

## 9. Next Planned Work

- Staff selection from `div_staff_master`
- Create analysis results table
- Surface reports in bbtro & OTS dashboards

---

## 10. Resume Instructions

In next chat:
> “Continue from SPM / RTIS integration – start with staff selection & DB table design”
