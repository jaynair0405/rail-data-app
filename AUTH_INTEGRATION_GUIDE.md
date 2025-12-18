# Authentication Integration Guide

**Status:** Ready for Testing
**Created:** 2025-12-18
**Purpose:** Share authentication between Node.js (bbtro) and Python (rail-data-app)

---

## What Changed

### Summary
- **Sessions** moved from memory → MySQL (shared between Node.js and Python)
- **New tables** for sessions and RTIS reports
- **Python app** can now validate Node.js sessions
- **Users** can log in once and access both apps

### Files Modified/Created

**Database:**
- `sql/01_create_sessions_and_reports_tables.sql` - Create sessions & rtis_reports tables

**Node.js (bbtro):**
- `package.json` - Added `express-mysql-session`
- `server.js` - Changed session storage to MySQL

**Python (rail-data-app):**
- `auth.py` - Session validation middleware

---

## Testing Workflow (Local First)

### Step 1: Database Setup (Local MySQL)

**1.1 Run SQL migration on your local MySQL:**
```bash
cd /Users/neeraja/Desktop/rail-data-app

# Login to local MySQL
mysql -u root -p

# Run migration
source sql/01_create_sessions_and_reports_tables.sql

# Verify tables were created
USE bbtro;
SHOW TABLES LIKE 'sessions';
SHOW TABLES LIKE 'rtis_reports';

# Grant RTIS access to test user (replace 'your_username' with actual username)
UPDATE users SET can_access_rtis = TRUE WHERE username = 'your_username';

# Check who has access
SELECT username, full_name, realm, div_role, can_access_rtis
FROM users
WHERE can_access_rtis = TRUE;
```

---

### Step 2: Update bbtro (Node.js) Locally

**2.1 Copy changes to your actual bbtro folder:**

From `/Users/neeraja/Desktop/rail-data-app/bbtro/` (the copy I edited)
To `[Your actual bbtro folder location]`

**Files to copy:**
- `package.json` (updated)
- `server.js` (updated)

**2.2 Install new dependency:**
```bash
cd [your-bbtro-folder]

# Install express-mysql-session
npm install

# Should show: + express-mysql-session@3.0.3
```

**2.3 Check .env file has SESSION_SECRET:**
```bash
# Open .env file
nano .env

# Make sure it has:
SESSION_SECRET=railway-bbtro-secret-key-2025
DB_HOST=localhost
DB_USER=root
DB_PASSWORD=your_password
DB_NAME=bbtro
```

**2.4 Test Node.js app locally:**
```bash
# Start the server
node server.js

# Should see:
# Server is running on port 3000
```

**2.5 Test login:**
1. Open browser: `http://localhost:3000`
2. Login with your credentials
3. Check MySQL that session was created:
   ```sql
   USE bbtro;
   SELECT session_id, FROM_UNIXTIME(expires/1000) AS expires_at
   FROM sessions;
   ```

**Expected:** You should see a session record!

---

### Step 3: Test Python App (rail-data-app) Locally

**3.1 Start SSH tunnel (if not already running):**
```bash
cd /Users/neeraja/Desktop/rail-data-app
./start-ssh-tunnel.sh
```

**3.2 Start Python app:**
```bash
# In another terminal
cd /Users/neeraja/Desktop/rail-data-app
source .venv/bin/activate
python3 app.py
```

**3.3 Test authentication:**
1. Make sure you're logged in to Node.js app (Step 2.5)
2. Open: `http://localhost:8765/ui/`
3. Try uploading a CSV

**Expected:** Python app should recognize your Node.js session!

---

## Deployment to Server

### Step 1: Deploy Database Changes

**1.1 Upload SQL file to server:**
```bash
scp sql/01_create_sessions_and_reports_tables.sql railway@93.127.198.125:/tmp/
```

**1.2 Run on server:**
```bash
ssh railway@93.127.198.125

# Run SQL
mysql -u railway_user -p bbtro < /tmp/01_create_sessions_and_reports_tables.sql

# Grant RTIS access to users (customize as needed)
mysql -u railway_user -p bbtro -e "
UPDATE users
SET can_access_rtis = TRUE
WHERE div_role = 'admin' AND realm = 'division';
"

# Verify
mysql -u railway_user -p bbtro -e "
SELECT username, full_name, can_access_rtis
FROM users
WHERE can_access_rtis = TRUE;
"
```

---

### Step 2: Deploy Node.js Changes (bbtro)

**2.1 From your local bbtro folder, commit and push:**
```bash
cd [your-bbtro-folder]

git status
# Should show:
#   modified: package.json
#   modified: server.js

git add package.json server.js
git commit -m "Add MySQL session store for shared authentication with Python app"
git push origin main
```

**2.2 On server, pull and update:**
```bash
ssh railway@93.127.198.125
cd /var/www/html

# Pull changes
git pull origin main

# Install new dependency
npm install

# Check .env has SESSION_SECRET
nano .env
# Add if missing:
# SESSION_SECRET=railway-bbtro-secret-key-2025

# Restart Node.js app
pm2 restart railway-system

# Check logs
pm2 logs railway-system --lines 50
```

**2.3 Verify sessions are being stored:**
```bash
mysql -u railway_user -p bbtro -e "SELECT COUNT(*) AS session_count FROM sessions;"
```

---

### Step 3: Deploy Python App

**3.1 Upload Python app (if not already on server):**
```bash
# From your Mac
cd /Users/neeraja/Desktop
scp -r rail-data-app railway@93.127.198.125:/tmp/

ssh railway@93.127.198.125
sudo mv /tmp/rail-data-app /var/www/rail-analysis
sudo chown -R railway:railway /var/www/rail-analysis
```

**3.2 Setup and start:**
```bash
cd /var/www/rail-analysis

# Create venv if not exists
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Update .env (NO SSH tunnel on server!)
nano .env
# Set:
# MYSQL_HOST=127.0.0.1
# MYSQL_PORT=3306

# Start with PM2
pm2 start .venv/bin/uvicorn --name "rail-analysis" -- app:app --host 0.0.0.0 --port 8765 --workers 4
pm2 save
```

---

## Testing Shared Authentication (Production)

**Test 1: Login to Node.js**
1. Visit: `http://93.127.198.125/`
2. Login with your credentials
3. Should redirect to division or suburban app

**Test 2: Check session in database**
```bash
mysql -u railway_user -p bbtro -e "
SELECT session_id, FROM_UNIXTIME(expires/1000) AS expires_at,
JSON_EXTRACT(data, '$.user.username') AS username
FROM sessions
ORDER BY expires DESC
LIMIT 5;
"
```

**Test 3: Access Python app (should auto-authenticate)**
1. Visit: `http://93.127.198.125/rail-analysis/ui/`
2. Should work without separate login!
3. Upload CSV and generate report

---

## Protecting Python Routes (Optional)

If you want to require authentication for Python endpoints:

### Example: Protect CSV Upload

```python
# In app.py
from fastapi import Depends
from auth import get_rtis_user

@app.post("/load_csv")
async def load_csv(
    file: UploadFile = File(...),
    user: dict = Depends(get_rtis_user)  # ← Add this
):
    # user dict contains: id, username, full_name, realm, etc.
    print(f"[UPLOAD] User {user['username']} uploading CSV")

    # ... rest of your code ...
```

### Example: Get Current User Info

```python
from auth import get_current_user

@app.get("/current-user")
async def current_user_endpoint(request: Request):
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "user": {
            "username": user['username'],
            "full_name": user['full_name'],
            "realm": user['realm']
        }
    }
```

---

## Managing RTIS Access

### Grant access to users:

```sql
-- Grant to specific user
UPDATE users
SET can_access_rtis = TRUE
WHERE username = 'specific_user';

-- Grant to all division admins
UPDATE users
SET can_access_rtis = TRUE
WHERE div_role = 'admin' AND realm = 'division';

-- Grant to specific offices
UPDATE users
SET can_access_rtis = TRUE
WHERE div_office_code IN ('IGP', 'CSMT', 'PUNE');

-- Revoke access
UPDATE users
SET can_access_rtis = FALSE
WHERE username = 'some_user';
```

### Check who has access:

```sql
SELECT
    id,
    username,
    full_name,
    realm,
    div_role,
    div_office_code,
    can_access_rtis,
    CASE
        WHEN can_access_rtis THEN '✓ GRANTED'
        ELSE '✗ DENIED'
    END AS rtis_access_status
FROM users
ORDER BY can_access_rtis DESC, username;
```

---

## Troubleshooting

### Issue: Python app says "Not authenticated"

**Check:**
1. Are you logged in to Node.js app first?
2. Is session in database?
   ```sql
   SELECT * FROM sessions ORDER BY expires DESC LIMIT 1;
   ```
3. Check Python logs:
   ```bash
   pm2 logs rail-analysis
   ```

### Issue: Sessions not persisting

**Check:**
1. MySQL `sessions` table exists?
2. Node.js `server.js` has `express-mysql-session` code?
3. `.env` has correct DB credentials?

### Issue: "No permission" error

**Grant RTIS access:**
```sql
UPDATE users SET can_access_rtis = TRUE WHERE username = 'your_username';
```

---

## Rollback Plan

If something goes wrong:

**1. Rollback Node.js:**
```bash
cd /var/www/html
git reset --hard HEAD~1  # Undo last commit
npm install
pm2 restart railway-system
```

**2. Rollback Database:**
```sql
DROP TABLE IF EXISTS sessions;
DROP TABLE IF EXISTS rtis_reports;
ALTER TABLE users DROP COLUMN IF EXISTS can_access_rtis;
```

---

## Next Steps

After successful integration:

1. **Monitor sessions table growth** - old sessions auto-expire
2. **Add report viewing UI** - show reports from `rtis_reports` table
3. **Add user management** - admin panel to grant/revoke RTIS access
4. **Setup Nginx SSL** - secure the connection with HTTPS

---

## Support

If you encounter issues:

1. Check logs:
   ```bash
   pm2 logs railway-system  # Node.js
   pm2 logs rail-analysis   # Python
   tail -f /var/log/nginx/error.log  # Nginx
   ```

2. Verify MySQL connection:
   ```bash
   mysql -u railway_user -p bbtro -e "SELECT 1"
   ```

3. Test session reading from Python:
   ```bash
   cd /var/www/rail-analysis
   source .venv/bin/activate
   python3 -c "from auth import get_session_from_mysql; print(get_session_from_mysql('test'))"
   ```

---

**Status:** Ready for Local Testing → Production Deployment
