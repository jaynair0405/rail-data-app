# PWA Implementation Plan for CRTMS

## Overview

This document outlines the plan to implement a **Single Unified PWA Portal** for the CRTMS platform, enabling mobile access for all user types (CLI, Analysts, Officers) through one app with role-based access.

---

## The Problem with Multiple PWAs

```
❌ BAD APPROACH - Multiple PWAs:

User's Phone Home Screen:
┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐ ┌─────┐
│ LP  │ │Subur│ │Bio- │ │Coun-│ │Daily│
│Reprt│ │-ban │ │data │ │sel  │ │Summ │
└─────┘ └─────┘ └─────┘ └─────┘ └─────┘

Problems:
- Cluttered home screen
- Users won't install all
- Multiple codebases to maintain
- Confusing for users
```

---

## The Solution: Single Portal PWA

```
✅ GOOD APPROACH - One PWA Portal:

User's Phone Home Screen:
┌─────┐
│CRTMS│  ← Just ONE icon
│ App │
└─────┘

Inside the app → Role-based dashboard with all features
```

### URL Structure

```
crtms.in/app/                    ← Single PWA entry point
├── /app/                        ← Dashboard (shows modules based on role)
├── /app/lp-reports/             ← LP Analysis Reports
├── /app/suburban-spm/           ← Suburban SPM Reports
├── /app/biodata/                ← Biodata Reports
├── /app/counselling/            ← Counselling App Reports
├── /app/daily-summary/          ← Daily Summary
├── /app/officer-report/         ← Officer Report
└── /app/settings/               ← User preferences
```

---

## Single Token → Multiple Access Explained

### What is a Token?

A token is a **secret key** that identifies who you are and what you can access - without needing username/password.

```
Token Example: "cli_a1b2c3d4e5f6g7h8i9j0"

This token contains (encrypted):
- User ID: CLI Staff "Prem Singh"
- HRMS ID: "NCLI001"
- Role: "cli"
- Assigned LPs: ["LP001", "LP002", "LP003"]
- Permissions: ["view_lp_reports", "view_suburban"]
- Expiry: 2026-06-30
```

### How Single Token Works

```
Step 1: Admin/System generates token for CLI staff
        ┌─────────────────────────────────────────┐
        │ Generate Token for: Prem Singh (CLI)    │
        │                                         │
        │ Access Permissions:                     │
        │ ☑ LP Reports (assigned LPs only)        │
        │ ☑ Suburban SPM Reports                  │
        │ ☐ Biodata (no access)                   │
        │ ☐ Counselling (no access)               │
        │ ☐ Daily Summary (no access)             │
        │                                         │
        │ Expiry: 90 days                         │
        │                                         │
        │ [Generate Token]                        │
        └─────────────────────────────────────────┘

Step 2: Token link sent via WhatsApp
        ┌─────────────────────────────────────────┐
        │ 📱 WhatsApp Message                     │
        │                                         │
        │ CRTMS Mobile Access                     │
        │ Click to open app:                      │
        │ https://crtms.in/app/?token=cli_a1b2... │
        │                                         │
        │ Valid for 90 days.                      │
        └─────────────────────────────────────────┘

Step 3: CLI opens link → Token saved in browser
        ┌─────────────────────────────────────────┐
        │ Browser/PWA stores token locally        │
        │ (like "Remember Me" functionality)      │
        │                                         │
        │ localStorage:                           │
        │ {                                       │
        │   "token": "cli_a1b2c3d4...",          │
        │   "name": "Prem Singh",                 │
        │   "role": "cli",                        │
        │   "permissions": ["view_lp_reports"...] │
        │ }                                       │
        └─────────────────────────────────────────┘

Step 4: Dashboard shows only permitted modules
        ┌─────────────────────────────────────────┐
        │  CRTMS Mobile                           │
        │  Welcome, Prem Singh                    │
        │                                         │
        │  ┌───────────┐  ┌───────────┐          │
        │  │ LP        │  │ Suburban  │          │
        │  │ Reports   │  │ SPM       │          │
        │  │    ✓      │  │    ✓      │          │
        │  └───────────┘  └───────────┘          │
        │                                         │
        │  (Biodata, Counselling not shown -     │
        │   user doesn't have permission)         │
        └─────────────────────────────────────────┘

Step 5: Every API call includes token
        ┌─────────────────────────────────────────┐
        │ GET /app/api/lp-reports                 │
        │ Header: Authorization: Bearer cli_a1b2..│
        │                                         │
        │ Server validates:                       │
        │ ✓ Token valid?                          │
        │ ✓ Not expired?                          │
        │ ✓ Has permission for this API?          │
        │ ✓ Return only data user can access      │
        └─────────────────────────────────────────┘
```

---

## Role-Based Access Matrix

### User Roles & Their Access

| Module | CLI Staff | Analyst | Officer | Admin |
|--------|:---------:|:-------:|:-------:|:-----:|
| LP Reports | ✓ (assigned only) | ✓ (all) | ✓ (all) | ✓ |
| Suburban SPM | ✓ | ✓ | ✓ | ✓ |
| Biodata | ✗ | ✓ | ✓ | ✓ |
| Counselling | ✗ | ✗ | ✓ | ✓ |
| Daily Summary | ✗ | ✓ | ✓ | ✓ |
| Officer Report | ✗ | ✗ | ✓ | ✓ |
| Quick Entry | ✗ | ✓ | ✓ | ✓ |
| Settings/Admin | ✗ | ✗ | ✗ | ✓ |

### Permission Granularity

```
Permissions are hierarchical:

view_lp_reports
├── view_lp_reports:assigned    ← CLI: only their assigned LPs
├── view_lp_reports:division    ← Analyst: all division LPs
└── view_lp_reports:all         ← Admin: everything

Example token permissions:
{
  "permissions": [
    "view_lp_reports:assigned",
    "view_suburban_spm",
    "view_biodata:read_only"
  ]
}
```

---

## Token Types & Use Cases

### 1. Permanent Staff Token (CLI, Analysts, Officers)

```
Generated: Once by admin
Expiry: 1 year (renewable)
Access: Based on role
Use: Regular app access

Example:
- CLI Prem Singh gets token
- Opens app daily to check reports
- Token stored on phone
- Auto-refreshes before expiry
```

### 2. Temporary Report Token

```
Generated: Auto when report is ready
Expiry: 30 days
Access: Single report only
Use: Quick sharing via WhatsApp

Example:
- Analyst completes LP analysis
- System generates report token
- WhatsApp sent to CLI: "New report ready: [link]"
- CLI clicks → sees just that report
```

### 3. Session Token (Login-based users)

```
Generated: On username/password login
Expiry: 24 hours (or on logout)
Access: Based on account role
Use: Full app users (analysts, officers)

Example:
- Officer logs in with credentials
- Gets session token
- Full access based on role
- Expires on logout or timeout
```

---

## Visual: Complete User Journey

### CLI Staff Journey

```
┌─────────────────────────────────────────────────────────────┐
│                     CLI STAFF JOURNEY                        │
└─────────────────────────────────────────────────────────────┘

Day 1: First Time Setup
─────────────────────────
     Admin                          CLI Phone
       │                                │
       │  1. Creates token for CLI      │
       ├───────────────────────────────>│
       │     (WhatsApp message)         │
       │                                │
       │                          2. CLI clicks link
       │                                │
       │                          3. "Add to Home Screen?"
       │                                │
       │                          4. CLI taps "Add"
       │                                │
       │                          5. App icon appears
       │                                │
       │                          6. Token saved locally
       │                                │

Day 2 onwards: Regular Use
──────────────────────────
     CLI Phone
         │
         │  1. Tap CRTMS icon
         │
         ▼
    ┌─────────┐
    │Dashboard│──────> Shows: LP Reports, Suburban SPM
    └─────────┘
         │
         │  2. Tap "LP Reports"
         ▼
    ┌─────────┐
    │ List of │──────> Only assigned LPs shown
    │ Reports │        (Ramesh, Suresh, Mahesh)
    └─────────┘
         │
         │  3. Tap a report
         ▼
    ┌─────────┐
    │   PDF   │──────> Full report view
    │  Viewer │        (can zoom, scroll)
    └─────────┘
```

### Analyst Journey

```
┌─────────────────────────────────────────────────────────────┐
│                    ANALYST JOURNEY                           │
└─────────────────────────────────────────────────────────────┘

    Analyst Phone
         │
         │  1. Login with credentials
         │     (or saved session)
         ▼
    ┌─────────────┐
    │  Dashboard  │──────> Shows ALL modules they can access
    └─────────────┘
         │
    ┌────┴────┬─────────┬──────────┐
    ▼         ▼         ▼          ▼
┌───────┐ ┌───────┐ ┌───────┐ ┌───────┐
│ Quick │ │ Daily │ │  LP   │ │Suburb-│
│ Entry │ │Summary│ │Reports│ │an SPM │
└───────┘ └───────┘ └───────┘ └───────┘
    │
    │  2. Quick Entry on the go
    ▼
┌─────────────────┐
│ Add SIM Down    │
│ ─────────────── │
│ LP: [Search...] │
│ Train: [12345]  │
│ Loco: [37691]   │
│                 │
│ [Submit]        │
└─────────────────┘
```

---

## Database Schema for Tokens

```sql
-- Main tokens table
CREATE TABLE app_access_tokens (
    id INT PRIMARY KEY AUTO_INCREMENT,
    token VARCHAR(64) UNIQUE NOT NULL,
    token_type ENUM('staff', 'report', 'session') NOT NULL,

    -- User identification
    user_hrms_id VARCHAR(20),
    user_name VARCHAR(100),
    user_role ENUM('cli', 'analyst', 'officer', 'admin'),

    -- For CLI: assigned LPs
    assigned_lp_hrms_ids JSON,  -- ["LP001", "LP002", "LP003"]

    -- Permissions
    permissions JSON,  -- ["view_lp_reports", "view_suburban_spm"]

    -- For report tokens: specific report
    report_id INT NULL,

    -- Validity
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP NOT NULL,
    last_used_at TIMESTAMP NULL,

    -- Security
    is_active BOOLEAN DEFAULT TRUE,
    created_by_user_id INT,
    revoked_at TIMESTAMP NULL,
    revoked_reason VARCHAR(255) NULL,

    INDEX idx_token (token),
    INDEX idx_user_hrms (user_hrms_id),
    INDEX idx_expires (expires_at)
);

-- Token usage log (for analytics & security)
CREATE TABLE token_usage_log (
    id BIGINT PRIMARY KEY AUTO_INCREMENT,
    token_id INT NOT NULL,
    accessed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    endpoint VARCHAR(255),
    ip_address VARCHAR(45),
    user_agent VARCHAR(500),

    INDEX idx_token_id (token_id),
    INDEX idx_accessed_at (accessed_at)
);
```

---

## API Design for Token Auth

### Token Validation Middleware

```python
# Every API call goes through this
async def validate_token(request: Request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    if not token:
        raise HTTPException(401, "Token required")

    # Check database
    token_data = get_token_from_db(token)

    if not token_data:
        raise HTTPException(401, "Invalid token")

    if token_data.expires_at < datetime.now():
        raise HTTPException(401, "Token expired")

    if not token_data.is_active:
        raise HTTPException(401, "Token revoked")

    # Log usage
    log_token_usage(token_data.id, request)

    # Attach user info to request
    request.state.user = {
        "hrms_id": token_data.user_hrms_id,
        "name": token_data.user_name,
        "role": token_data.user_role,
        "permissions": token_data.permissions,
        "assigned_lps": token_data.assigned_lp_hrms_ids
    }

    return token_data
```

### Permission Check Helper

```python
def require_permission(permission: str):
    def decorator(func):
        async def wrapper(request: Request, *args, **kwargs):
            user = request.state.user

            # Check if user has required permission
            if permission not in user["permissions"]:
                raise HTTPException(403, "Permission denied")

            return await func(request, *args, **kwargs)
        return wrapper
    return decorator

# Usage
@app.get("/app/api/lp-reports")
@require_permission("view_lp_reports")
async def get_lp_reports(request: Request):
    user = request.state.user

    if user["role"] == "cli":
        # CLI only sees assigned LPs
        return get_reports_for_lps(user["assigned_lps"])
    else:
        # Others see all
        return get_all_reports()
```

---

## PWA Technical Setup

### File Structure

```
/app/                           ← PWA root
├── index.html                  ← Single Page App entry
├── manifest.json               ← PWA manifest
├── service-worker.js           ← Offline & caching
├── icons/
│   ├── icon-72.png
│   ├── icon-96.png
│   ├── icon-128.png
│   ├── icon-144.png
│   ├── icon-152.png
│   ├── icon-192.png
│   ├── icon-384.png
│   └── icon-512.png
├── css/
│   └── app.css                 ← Mobile-first styles
└── js/
    ├── app.js                  ← Main application
    ├── router.js               ← Client-side routing
    ├── auth.js                 ← Token management
    └── modules/
        ├── dashboard.js
        ├── lp-reports.js
        ├── suburban-spm.js
        └── ...
```

### manifest.json

```json
{
  "name": "CRTMS Mobile",
  "short_name": "CRTMS",
  "description": "Central Railway TMS Mobile Portal",
  "start_url": "/app/",
  "scope": "/app/",
  "display": "standalone",
  "orientation": "portrait",
  "theme_color": "#2563eb",
  "background_color": "#ffffff",
  "icons": [
    { "src": "/app/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/app/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

### Token Storage in Browser

```javascript
// auth.js - Token management

const TOKEN_KEY = 'crtms_token';
const USER_KEY = 'crtms_user';

// Save token after first access
function saveToken(token, userData) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(userData));
}

// Get token for API calls
function getToken() {
    return localStorage.getItem(TOKEN_KEY);
}

// Get user info for UI
function getUser() {
    const data = localStorage.getItem(USER_KEY);
    return data ? JSON.parse(data) : null;
}

// Check if logged in
function isAuthenticated() {
    return !!getToken();
}

// Logout / clear token
function logout() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
    window.location.href = '/app/';
}

// API call with token
async function apiCall(endpoint, options = {}) {
    const token = getToken();

    const response = await fetch(`/app/api${endpoint}`, {
        ...options,
        headers: {
            ...options.headers,
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
        }
    });

    if (response.status === 401) {
        // Token expired or invalid
        logout();
        return;
    }

    return response.json();
}
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1-2)

- [ ] **PWA Infrastructure**
  - [ ] Create `/app/` directory
  - [ ] Setup `manifest.json`
  - [ ] Create `service-worker.js`
  - [ ] Design app icons
  - [ ] Mobile-first CSS framework

- [ ] **Token System**
  - [ ] Create `app_access_tokens` table
  - [ ] Create `token_usage_log` table
  - [ ] Token generation API
  - [ ] Token validation middleware
  - [ ] Permission check helpers

- [ ] **Basic Auth Flow**
  - [ ] Token landing page (validates & saves token)
  - [ ] Dashboard with role-based modules
  - [ ] Logout functionality

### Phase 2: Core Modules (Week 3-4)

- [ ] **LP Reports Module**
  - [ ] Reports list view
  - [ ] PDF viewer (mobile optimized)
  - [ ] Filter by date/train/LP
  - [ ] Offline caching

- [ ] **PDF Storage** (deferred feature)
  - [ ] Create `/reports/` folder
  - [ ] Save PDF on analysis save
  - [ ] Cleanup old reports (20 per LP)
  - [ ] Serve PDFs via API

### Phase 3: Additional Modules (Week 5-6)

- [ ] **Suburban SPM Module**
  - [ ] Integration with existing suburban app
  - [ ] Mobile-optimized views

- [ ] **Quick Entry Module** (for analysts)
  - [ ] SIM Down / NON RTIS entry
  - [ ] LP/Train lookup
  - [ ] Submit & sync

- [ ] **Daily Summary Module**
  - [ ] View daily stats
  - [ ] Officer report generation

### Phase 4: Polish & Launch (Week 7-8)

- [ ] **Admin Features**
  - [ ] Token management UI
  - [ ] Generate tokens for CLI
  - [ ] Revoke tokens
  - [ ] View token usage

- [ ] **Notifications**
  - [ ] WhatsApp integration for token delivery
  - [ ] Push notifications (optional)

- [ ] **Testing & Launch**
  - [ ] Test on various devices
  - [ ] Offline functionality testing
  - [ ] Security audit
  - [ ] Soft launch with select users

---

## Security Considerations

| Risk | Mitigation |
|------|------------|
| Token theft | Short expiry + refresh tokens |
| Token sharing | Log device fingerprint, IP |
| Brute force | Rate limiting (5 attempts/min) |
| Data exposure | Permission-based API responses |
| XSS attacks | Sanitize all inputs, CSP headers |
| MITM attacks | HTTPS only, HSTS headers |

---

## Questions to Decide

1. **Token Expiry:**
   - [ ] CLI Staff: 90 days? 1 year?
   - [ ] Report tokens: 30 days?
   - [ ] Session tokens: 24 hours?

2. **Notification Method:**
   - [ ] WhatsApp API (paid)
   - [ ] SMS gateway (paid)
   - [ ] Email only (free)
   - [ ] Manual sharing (copy link)

3. **Offline Support:**
   - [ ] Cache last N reports per module?
   - [ ] Full offline with sync?
   - [ ] Online only?

4. **Which modules in Phase 1?**
   - [ ] LP Reports (required)
   - [ ] Suburban SPM?
   - [ ] Others?

---

## Deferred Items (Until PWA is Ready)

These features are on hold:

1. **PDF Storage** - Save PDFs to `/reports/` folder
2. **View PDF Link** - Fix broken link in LP reports tab
3. **Report Cleanup** - Keep only 20 reports per LP
4. **WhatsApp Notifications** - Auto-notify CLI on new report

---

## Summary

```
ONE APP → ONE ICON → MANY FEATURES

┌────────────────────────────────────────┐
│           CRTMS Mobile App             │
│                                        │
│   Token identifies WHO you are         │
│   Permissions control WHAT you see     │
│   Role determines your DASHBOARD       │
│                                        │
│   CLI → sees LP Reports, Suburban      │
│   Analyst → sees Entry, Summary, All   │
│   Officer → sees Reports, KPIs, All    │
│   Admin → sees EVERYTHING              │
│                                        │
└────────────────────────────────────────┘
```

---

*Document created: March 2026*
*Last updated: March 2026*
*Status: TODO - Planning Phase*
