# Server Deployment - Visual Guide

## 📁 Folder Structure

```
/home/railway/
│
├── 🟦 bbtro/                       ← Node.js Web Platform
│   ├── node_modules/
│   ├── public/
│   │   └── div/
│   │       └── spm-hub.html        ← SPM Hub (links to RTIS)
│   ├── server.js                   ← Reverse proxy to RTIS
│   ├── .env                        ← ⚠️ CREATE ON SERVER
│   └── package.json
│
├── 🟩 rail-data-app/               ← Python RTIS Analysis
│   ├── .venv/                      ← Virtual environment
│   ├── base-data/                  ← Reference CSVs
│   ├── ui/
│   │   └── index.html              ← RTIS Interface
│   ├── app.py                      ← FastAPI server
│   ├── auth.py                     ← Session validation
│   ├── .env                        ← ⚠️ CREATE ON SERVER
│   └── requirements.txt
│
└── logs/                           ← Optional logs
```

---

## 🔄 Request Flow

```
┌─────────────┐
│   Browser   │
└──────┬──────┘
       │ https://your-domain.com/spm/rtis/rtis
       ↓
┌──────────────────────────────────┐
│   Nginx (Port 80/443)            │ ← Public facing
└──────┬───────────────────────────┘
       │ proxy to localhost:3000
       ↓
┌──────────────────────────────────┐
│   bbtro (Node.js, Port 3000)     │ ← Main platform
│   ├── Session Management         │
│   ├── Authentication              │
│   └── Reverse Proxy              │
└──────┬───────────────────────────┘
       │ /spm/rtis/* → localhost:8765
       ↓
┌──────────────────────────────────┐
│   rail-data-app                  │ ← RTIS Analysis
│   (FastAPI, Port 8765)           │    (localhost only)
│   ├── Validates bbtro session    │
│   ├── Checks can_access_rtis     │
│   └── Serves RTIS UI + API       │
└──────┬───────────────────────────┘
       │ reads sessions
       ↓
┌──────────────────────────────────┐
│   MySQL (Port 3306)              │ ← Shared database
│   ├── sessions table             │    (localhost only)
│   ├── users table                │
│   └── div_staff_master           │
└────────────────────────────────────┘
```

---

## 🚦 Service Management

### Check Status
```bash
sudo systemctl status bbtro
sudo systemctl status rtis
```

### View Logs
```bash
# Follow logs in real-time
sudo journalctl -u bbtro -f
sudo journalctl -u rtis -f

# View last 50 lines
sudo journalctl -u rtis -n 50
```

### Restart Services
```bash
sudo systemctl restart bbtro
sudo systemctl restart rtis
```

---

## 🔐 Security Layers

```
Layer 1: Nginx (SSL/TLS)
  ↓
Layer 2: bbtro Authentication (express-session)
  ↓
Layer 3: RTIS Access Check (can_access_rtis flag)
  ↓
Layer 4: Session Validation (MySQL lookup)
  ↓
Layer 5: RTIS Application
```

---

## 📊 Port Mapping

| Port | Service | Public? | Purpose |
|------|---------|---------|---------|
| 80/443 | Nginx | ✅ Yes | Web traffic |
| 3000 | bbtro | ❌ No | Node.js app (proxied) |
| 8765 | rail-data-app | ❌ No | Python app (proxied) |
| 3306 | MySQL | ❌ No | Database |

**Important:** Only Nginx is exposed to internet. All other services on localhost only.

---

## 🛠️ Quick Deploy Commands

```bash
# 1. Clone repositories
cd /home/railway
git clone https://github.com/jaynair0405/bbtro.git
git clone https://github.com/jaynair0405/rail-data-app.git

# 2. Setup bbtro
cd bbtro
npm install
cp .env.example .env
nano .env  # Add credentials

# 3. Setup rail-data-app
cd ../rail-data-app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env  # Add credentials

# 4. Start services
sudo systemctl start bbtro rtis
sudo systemctl enable bbtro rtis

# 5. Check everything is running
sudo systemctl status bbtro rtis
curl http://localhost:3000
curl http://localhost:3000/spm/rtis/
```

---

## 📝 Environment Files

### bbtro/.env
```env
PORT=3000
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=production_user
MYSQL_PASSWORD=production_password
SESSION_SECRET=your_random_secret_here
```

### rail-data-app/.env
```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=production_user
MYSQL_PASSWORD=production_password
```

**⚠️ Important:** These .env files exist ONLY on server, never in git!

---

## ✅ Post-Deployment Checklist

- [ ] Both services running: `systemctl status bbtro rtis`
- [ ] Ports listening: `netstat -tlnp | grep -E '3000|8765'`
- [ ] No errors in logs: `journalctl -u rtis -n 50`
- [ ] Can access bbtro: `curl http://localhost:3000`
- [ ] Reverse proxy works: `curl http://localhost:3000/spm/rtis/`
- [ ] MySQL connection works from both apps
- [ ] .env files protected: `ls -l /home/railway/*/.env`
- [ ] Services auto-start: `systemctl is-enabled bbtro rtis`
