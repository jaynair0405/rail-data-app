# First-Time Deployment Guide - rail-data-app

## Prerequisites
- Server already has bbtro running
- MySQL database running
- You have SSH access to server
- Git is installed on server

---

## 🚀 Step-by-Step Deployment

### 1️⃣ SSH to Server
```bash
ssh railway@your-server-ip
# or
ssh railway@crtms.in
```

### 2️⃣ Navigate to Home Directory
```bash
cd /home/railway
pwd  # Should show: /home/railway
```

### 3️⃣ Clone the Repository
```bash
git clone https://github.com/jaynair0405/rail-data-app.git
```

Expected output:
```
Cloning into 'rail-data-app'...
remote: Enumerating objects: ...
```

### 4️⃣ Enter the Directory
```bash
cd rail-data-app
ls -la  # Verify files are there
```

You should see:
- app.py
- auth.py
- db_config.py
- requirements.txt
- .env.example
- base-data/
- ui/

### 5️⃣ Create Python Virtual Environment
```bash
python3 -m venv .venv
```

This creates a `.venv` folder (might take 30 seconds).

### 6️⃣ Activate Virtual Environment
```bash
source .venv/bin/activate
```

Your prompt should change to show `(.venv)`:
```
(.venv) railway@server:~/rail-data-app$
```

### 7️⃣ Upgrade pip and Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

This will install:
- fastapi
- uvicorn
- polars
- matplotlib
- reportlab
- mysql-connector-python
- python-dotenv
- etc.

(Takes 2-5 minutes)

### 8️⃣ Create .env File with Server Credentials
```bash
cp .env.example .env
nano .env
```

In nano, edit the file to:
```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=your_server_mysql_user
MYSQL_PASSWORD=your_server_mysql_password
```

**Important:** Use your **SERVER** MySQL credentials (same as bbtro uses).

**Save and exit:**
- Press `Ctrl+O` (save)
- Press `Enter` (confirm)
- Press `Ctrl+X` (exit)

### 9️⃣ Set Secure Permissions
```bash
chmod 600 .env
ls -l .env
```

Should show: `-rw------- 1 railway railway`

### 🔟 Test the Application Manually (Optional)
```bash
# Still in virtual environment
source .venv/bin/activate
uvicorn app:app --host 127.0.0.1 --port 8765
```

You should see:
```
INFO:     Started server process
INFO:     Uvicorn running on http://127.0.0.1:8765
```

**Test it:**
Open another terminal and SSH again:
```bash
curl http://localhost:8765/
```

Should return: `{"detail":"Not Found"}` (this is OK - means it's running)

**Stop the test server:**
Press `Ctrl+C`

---

## 🔧 Set Up Systemd Service (Auto-Start)

### 1️⃣ Create Service File
```bash
sudo nano /etc/systemd/system/rtis.service
```

### 2️⃣ Paste This Configuration
```ini
[Unit]
Description=RTIS FastAPI Application
After=network.target mysql.service

[Service]
Type=simple
User=railway
WorkingDirectory=/home/railway/rail-data-app
Environment="PATH=/home/railway/rail-data-app/.venv/bin"
ExecStart=/home/railway/rail-data-app/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Save and exit:** `Ctrl+O`, `Enter`, `Ctrl+X`

### 3️⃣ Enable and Start Service
```bash
sudo systemctl daemon-reload
sudo systemctl enable rtis
sudo systemctl start rtis
```

### 4️⃣ Check Status
```bash
sudo systemctl status rtis
```

Should show:
```
● rtis.service - RTIS FastAPI Application
   Loaded: loaded
   Active: active (running)
```

Press `q` to exit.

---

## ✅ Verification Steps

### 1. Check if Service is Running
```bash
sudo systemctl status rtis
```

### 2. Check if Port 8765 is Listening
```bash
sudo netstat -tlnp | grep 8765
```

Should show:
```
tcp  0  0  127.0.0.1:8765  0.0.0.0:*  LISTEN  12345/python
```

### 3. Check Logs
```bash
sudo journalctl -u rtis -n 50
```

Should NOT show errors.

### 4. Test Direct Access (on server)
```bash
curl http://localhost:8765/
```

Should return JSON response (not error).

### 5. Test via bbtro Proxy
```bash
curl http://localhost:3000/spm/rtis/
```

Should return redirect or HTML.

---

## 🔄 Future Updates (After Initial Setup)

When you push changes to GitHub, update the server:

```bash
# SSH to server
ssh railway@your-server-ip

# Navigate to directory
cd /home/railway/rail-data-app

# Pull latest changes
git pull origin main

# Activate virtual environment (if dependencies changed)
source .venv/bin/activate
pip install -r requirements.txt

# Restart service
sudo systemctl restart rtis

# Check status
sudo systemctl status rtis
```

---

## 🐛 Troubleshooting

### Service won't start
```bash
# Check detailed logs
sudo journalctl -u rtis -n 100 --no-pager

# Check if port is already in use
sudo lsof -i :8765
```

### Database connection errors
```bash
# Verify .env file
cat .env

# Test MySQL connection
mysql -h 127.0.0.1 -u your_user -p bbtro -e "SELECT 1;"
```

### Permission errors
```bash
# Fix ownership
sudo chown -R railway:railway /home/railway/rail-data-app

# Fix .env permissions
chmod 600 /home/railway/rail-data-app/.env
```

---

## 📝 Quick Reference

| Task | Command |
|------|---------|
| Start service | `sudo systemctl start rtis` |
| Stop service | `sudo systemctl stop rtis` |
| Restart service | `sudo systemctl restart rtis` |
| Check status | `sudo systemctl status rtis` |
| View logs | `sudo journalctl -u rtis -f` |
| Update code | `cd ~/rail-data-app && git pull origin main && sudo systemctl restart rtis` |

---

## ✅ Success Checklist

- [ ] Repository cloned to `/home/railway/rail-data-app`
- [ ] Virtual environment created at `.venv/`
- [ ] Dependencies installed
- [ ] `.env` file created with server credentials
- [ ] `.env` permissions set to 600
- [ ] Systemd service file created
- [ ] Service enabled and started
- [ ] Service status shows "active (running)"
- [ ] Port 8765 listening on localhost
- [ ] No errors in logs
- [ ] bbtro can proxy to rtis

---

## 🎯 Final Test

From your browser (anywhere):
1. Go to: `https://crtms.in/` (or your domain)
2. Login to bbtro
3. Navigate to SPM Hub
4. Click on RTIS
5. Should redirect to RTIS interface

If this works: **🎉 Deployment Successful!**
