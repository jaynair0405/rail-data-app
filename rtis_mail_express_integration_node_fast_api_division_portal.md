# RTIS Mail/Express Integration – Division Portal

This document captures the **final, working architecture and fixes** for integrating the RTIS Mail/Express analysis UI (FastAPI) inside the **Division Portal (Node/Express)**.

---

## 1. Overall Architecture

- **Node.js (bbtro)**
  - Authentication, session management
  - Division/Suburban realms
  - Acts as **reverse proxy** for RTIS FastAPI

- **FastAPI (RTIS backend)**
  - Route validation
  - Train metadata
  - RTIS analysis & PDF export

- **RTIS UI**
  - Served via Node at:
    ```
    /spm/rtis/ui/
    ```
  - All API calls routed through Node proxy:
    ```
    /spm/rtis/*
    ```

---

## 2. Division-only Access Control

RTIS is **part of the Division Portal**, not Suburban.

### Guard (Node)
```js
app.use("/spm/rtis", (req, res, next) => {
  const user = req.session?.user;
  if (!user) return res.redirect("/");
  if (user.realm !== "division") return res.redirect("/");
  next();
});
```

- Blocks unauthenticated users
- Blocks wrong realm
- UI opens only after Division login

---

## 3. Critical Fix: Hanging POST /validate_route

### Problem
- `/spm/rtis/validate_route` stayed **Pending forever**
- Browser showed:
  - `ERR_NETWORK_IO_SUSPENDED`
  - `Empty reply from server`

### Root Cause
`express.json()` was **consuming the POST body**, so the proxy forwarded an empty stream to FastAPI.

This breaks POST proxying.

---

## 4. Final Correct Fix (Permanent)

### ✅ Skip JSON parsing for proxied RTIS routes

Replace:
```js
app.use(express.json());
```

With:
```js
// Do NOT body-parse proxied RTIS requests
app.use((req, res, next) => {
  if (req.originalUrl.startsWith("/spm/rtis")) return next();
  express.json()(req, res, next);
});
```

### Why this is safe
- Affects **only** `/spm/rtis/*`
- All existing APIs (`/api/*`, `/login`, CSV uploads, etc.) continue to work
- Node becomes a **transparent proxy** for RTIS

---

## 5. Proxy Configuration (Clean)

```js
app.use(
  "/spm/rtis",
  createProxyMiddleware({
    target: "http://localhost:8765",
    changeOrigin: true,
    ws: true,
    pathRewrite: { "^/spm/rtis": "" },
    timeout: 600000,
    proxyTimeout: 600000,
    onError(err, req, res) {
      console.error("[RTIS PROXY ERROR]", err.message);
      if (!res.headersSent)
        res.writeHead(502, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ success: false, message: "RTIS proxy error" }));
    },
  })
);
```

---

## 6. Staff & Crew Logic (Finalised)

### 6.1 LP Selection (Mandatory)

Source: `div_staff_master`

Rules:
- `status = 'Active'`
- `designation_id IN (5,6,7)`
- `current_office_code = 'CSMT-ML'`

Stored:
- `lp_hrms_id`

UI behaviour:
- Strict selection from staff master
- No free-text LP allowed

---

### 6.2 NCLI (Auto, Locked)

Source:
- `div_staff_master.current_cli_id`
- Joined to `div_cli_master`

Rules:
- Auto-populated when LP selected
- Read-only in UI
- If missing → **Analyze disabled**

Stored:
- `cli_id` snapshot (as on analysis date)

---

### 6.3 Analyzed By (CLI)

Source: `div_cli_master`

Behaviour:
- Defaults to **same as NCLI**
- User may override by selecting another CLI
- Strict selection (no free text)

Stored:
- `analyst_cli_id`

---

## 7. Division RTIS APIs (Node)

Mounted at:
```text
/api/division/rtis/*
```

### Implemented endpoints
- `GET /lp-search`
- `GET /lp/:hrms_id`
- `GET /cli-search`

All protected by:
```js
requireRealm("division")
```

---

## 8. FastAPI Validation Confirmed

Direct calls tested and confirmed:

```bash
curl -X POST http://localhost:8765/validate_route
```

- Instant response
- Correct validation, reversal detection

Node proxy now mirrors this behaviour exactly.

---

## 9. Final Status

✅ RTIS UI loads inside Division Portal
✅ Division-only authentication enforced
✅ LP / NCLI / Analyst logic finalized
✅ Route validation working via proxy
✅ POST hanging issue permanently fixed

---

## 10. Next Steps (New Chat)

1. Extract **requirements** from RTIS PDF output
2. Design **RTIS analysis tables/schema**
3. Decide what to persist vs derive
4. Implement save + history

---

**End of integration note**

