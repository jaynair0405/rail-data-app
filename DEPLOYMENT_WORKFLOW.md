# Deployment Workflow Guide

**Purpose:** How to manage and deploy both bbtro (Node.js) and rail-data-app (Python) projects

**Last Updated:** 2025-12-18

---

## Current Setup

### Local Development (Your Mac)

```
~/bbtro/                           ← Node.js app (git enabled)
  └── .git/

~/Desktop/rail-data-app/           ← Python app (git enabled)
  └── .git/
```

### Production Server

```
/var/www/html/                     ← bbtro (Node.js)
  └── PM2: railway-system

/home/railway/rail-data-app/            ← rail-data-app (Python)
  └── PM2: rtis
```

---

## GitHub Repository Strategy

### **Recommended: Option 1 - Two Separate Repos** ⭐

**Why separate repos?**
- Different technologies (Node.js vs Python)
- Independent deployment cycles
- Clean version history
- Can assign different permissions

**Structure:**

```
GitHub:
├── your-username/bbtro                    ← Existing repo
│   └── Node.js web application
│
└── your-username/rail-data-app            ← New repo
    └── Python RTIS analysis tool
```

**Server Deployment:**

```
/var/www/
├── html/                          ← git clone bbtro
│   └── git pull to update
│
└── rail-data-app/                 ← git clone rail-data-app (in /home/railway)
    └── git pull to update
```

---

## Setup Instructions

### Step 1: Create GitHub Repo for rail-data-app

**On GitHub:**
1. Go to: https://github.com/new
2. Repository name: `rail-data-app` (or `rtis-analysis`)
3. Description: "RTIS Railway Telemetry Analysis Tool - Python FastAPI"
4. **Private** or Public (your choice)
5. **Do NOT initialize** (no README, no .gitignore)
6. Click "Create repository"

---

### Step 2: Push Existing Code to GitHub

**You already have commits in rail-data-app! Just add remote:**

```bash
cd /Users/neeraja/Desktop/rail-data-app

# Check existing commits
git log --oneline

# Add GitHub remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/rail-data-app.git

# Check remote
git remote -v

# Push to GitHub
git push -u origin main

# If branch is called 'master' instead:
git branch -M main
git push -u origin main
```

**Verify:** Visit `https://github.com/YOUR_USERNAME/rail-data-app` - you should see all your files!

---

### Step 3: Deploy to Server

**On your server:**

```bash
ssh railway@93.127.198.125

# Navigate to web root
cd /var/www

# If /home/railway/rail-data-app exists from manual upload, remove it:
sudo rm -rf rail-analysis

# Clone from GitHub
sudo git clone https://github.com/YOUR_USERNAME/rail-data-app.git rail-analysis

# Set ownership
sudo chown -R railway:railway rail-analysis

# Navigate to folder
cd rail-analysis

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create/update .env file for server (NO SSH tunnel on server!)
nano .env
```

**Server .env file:**
```bash
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=railway_user
MYSQL_PASSWORD=4310@Chakkara
```

**Start with PM2:**
```bash
pm2 start .venv/bin/uvicorn --name "rtis" -- app:app --host 0.0.0.0 --port 8765 --workers 4
pm2 save
pm2 list
```

---

## Daily Workflow

### Working on bbtro (Node.js)

**Local development:**
```bash
cd ~/bbtro

# Make changes to files
# Test locally: node server.js

# Commit changes
git add .
git commit -m "Add RTIS button to division UI"
git push origin main
```

**Deploy to server:**
```bash
# SSH to server
ssh railway@93.127.198.125

# Pull latest changes
cd /var/www/html
git pull origin main

# If package.json changed:
npm install

# Restart application
pm2 restart railway-system

# Check logs
pm2 logs railway-system --lines 50
```

**One-liner deployment:**
```bash
ssh railway@93.127.198.125 "cd /var/www/html && git pull && npm install && pm2 restart railway-system"
```

---

### Working on rail-data-app (Python)

**Local development:**
```bash
cd ~/Desktop/rail-data-app

# Make changes to files
# Test locally: python3 app.py

# Commit changes
git add .
git commit -m "Add authentication to CSV upload"
git push origin main
```

**Deploy to server:**
```bash
# SSH to server
ssh railway@93.127.198.125

# Pull latest changes
cd /home/railway/rail-data-app
git pull origin main

# If requirements.txt changed:
source .venv/bin/activate
pip install -r requirements.txt

# Restart application
pm2 restart rtis

# Check logs
pm2 logs rtis --lines 50
```

**One-liner deployment:**
```bash
ssh railway@93.127.198.125 "cd /home/railway/rail-data-app && git pull && source .venv/bin/activate && pip install -r requirements.txt && pm2 restart rtis"
```

---

## Making Changes to Both Apps

**If a change affects both apps** (e.g., database schema):

```bash
# 1. Make database changes on server
ssh railway@93.127.198.125
mysql -u railway_user -p bbtro < /path/to/migration.sql

# 2. Update and deploy bbtro
cd ~/bbtro
git add .
git commit -m "Update for new database schema"
git push
ssh railway@93.127.198.125 "cd /var/www/html && git pull && pm2 restart railway-system"

# 3. Update and deploy rail-data-app
cd ~/Desktop/rail-data-app
git add .
git commit -m "Update for new database schema"
git push
ssh railway@93.127.198.125 "cd /home/railway/rail-data-app && git pull && pm2 restart rtis"
```

---

## Git Best Practices

### Commit Messages

**Good:**
```
✓ "Add RTIS access button to division dashboard"
✓ "Fix session timeout issue in auth middleware"
✓ "Update brake test logic for IGP station"
```

**Bad:**
```
✗ "changes"
✗ "fix"
✗ "update"
```

---

### Branches (Optional)

**For major features, use branches:**

```bash
# Create feature branch
git checkout -b feature/tsr-implementation

# Make changes
git add .
git commit -m "Add TSR table and loading logic"

# Push branch
git push origin feature/tsr-implementation

# Merge when ready (on GitHub via Pull Request or locally)
git checkout main
git merge feature/tsr-implementation
git push origin main
```

---

### .gitignore

**Make sure sensitive files are ignored:**

**bbtro/.gitignore:**
```
node_modules/
.env
*.log
```

**rail-data-app/.gitignore:**
```
.venv/
.env
__pycache__/
*.pyc
*.pdf
*.xlsx
*.csv
!base-data/*.csv
```

---

## Deployment Checklist

### Before Deploying bbtro:

- [ ] Test locally: `node server.js`
- [ ] Check for console errors
- [ ] Verify .env variables loaded
- [ ] Run `npm install` if package.json changed
- [ ] Commit with meaningful message
- [ ] Push to GitHub
- [ ] Pull on server
- [ ] Restart PM2 process
- [ ] Check PM2 logs for errors
- [ ] Test in browser

---

### Before Deploying rail-data-app:

- [ ] Test locally: `python3 app.py`
- [ ] Verify SSH tunnel works (local) or MySQL connection (server)
- [ ] Run `pip install -r requirements.txt` if requirements changed
- [ ] Test critical endpoints (upload, analyze, PDF)
- [ ] Commit with meaningful message
- [ ] Push to GitHub
- [ ] Pull on server
- [ ] Activate venv and install requirements
- [ ] Restart PM2 process
- [ ] Check PM2 logs for errors
- [ ] Test in browser

---

## Rollback Strategy

### If deployment breaks:

**Quick rollback:**
```bash
# On server
cd /var/www/html  # or /home/railway/rail-data-app

# Revert to previous commit
git log --oneline  # Find previous commit hash
git reset --hard COMMIT_HASH

# Restart
pm2 restart railway-system  # or rail-analysis
```

**Example:**
```bash
cd /var/www/html
git log --oneline
# Output:
# a1b2c3d Add new feature (broken)
# e4f5g6h Previous working version

git reset --hard e4f5g6h
pm2 restart railway-system
```

---

## Common Issues

### Issue: Git pull shows conflicts

**Solution:**
```bash
# Stash local changes
git stash

# Pull latest
git pull origin main

# Reapply stashed changes (if needed)
git stash pop

# Or discard local changes entirely
git reset --hard origin/main
```

---

### Issue: PM2 process won't start after pull

**Check:**
```bash
# View full logs
pm2 logs railway-system --lines 100

# Common issues:
# 1. Syntax error in code
# 2. Missing npm packages
# 3. Wrong .env configuration
# 4. Port already in use

# Fix and restart
pm2 restart railway-system
```

---

### Issue: Changes not appearing on website

**Checklist:**
1. Did git push succeed?
2. Did git pull succeed on server?
3. Did PM2 restart succeed?
4. Check browser cache (hard refresh: Cmd+Shift+R)
5. Check correct URL being accessed

---

## Monitoring

### Check Application Status

```bash
# On server
pm2 list                          # See all processes
pm2 logs railway-system          # Node.js logs
pm2 logs rtis           # Python logs
pm2 monit                        # Real-time monitoring

# Check if apps are responding
curl http://localhost:3000       # Node.js
curl http://localhost:8765/ui/   # Python
```

---

### Check Git Status

```bash
# On server
cd /var/www/html
git status                       # Should be clean
git log --oneline -5            # Last 5 commits

cd /home/railway/rail-data-app
git status
git log --oneline -5
```

---

## Automation Scripts (Optional)

### Create deployment scripts:

**deploy-bbtro.sh:**
```bash
#!/bin/bash
# Quick deploy script for bbtro

echo "Deploying bbtro..."
ssh railway@93.127.198.125 << 'EOF'
cd /var/www/html
git pull origin main
npm install
pm2 restart railway-system
pm2 logs railway-system --lines 20
EOF
echo "✓ Deployment complete!"
```

**deploy-python.sh:**
```bash
#!/bin/bash
# Quick deploy script for rail-data-app

echo "Deploying rail-data-app..."
ssh railway@93.127.198.125 << 'EOF'
cd /home/railway/rail-data-app
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
pm2 restart rtis
pm2 logs rtis --lines 20
EOF
echo "✓ Deployment complete!"
```

**Make executable:**
```bash
chmod +x deploy-bbtro.sh deploy-python.sh

# Usage:
./deploy-bbtro.sh
./deploy-python.sh
```

---

## Summary

**Two repos, two deployments, clean workflow:**

1. **bbtro** (Node.js) → GitHub → `/var/www/html/`
2. **rail-data-app** (Python) → GitHub → `/home/railway/rail-data-app/`

**Deploy process:**
```
Local changes → Git commit → Git push → SSH to server → Git pull → Restart PM2
```

**Simple, professional, maintainable!** ✅

---

## Next Steps

1. [ ] Create GitHub repo for rail-data-app
2. [ ] Push existing code to GitHub
3. [ ] Clone to server
4. [ ] Setup PM2 process
5. [ ] Configure Nginx routing
6. [ ] Test full workflow
7. [ ] Create deployment scripts (optional)

---

**Questions?** Refer to `AUTH_INTEGRATION_GUIDE.md` for authentication setup details.
