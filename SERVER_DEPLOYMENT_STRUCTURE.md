# Server Deployment - Folder Structure

## Recommended Server Structure

```
/home/railway/
├── bbtro/                          # Node.js main application
│   ├── node_modules/
│   ├── public/
│   │   ├── div/
│   │   │   ├── spm-hub.html       # SPM Hub page
│   │   │   └── ...other division pages
│   │   └── ...
│   ├── server.js                   # Express server with reverse proxy
│   ├── package.json
│   ├── .env                        # Node.js env vars
│   └── ...
│
├── rail-data-app/                  # Python FastAPI RTIS application
│   ├── .venv/                      # Python virtual environment
│   ├── base-data/                  # Reference CSV files (committed)
│   │   ├── all_section_psr.csv
│   │   ├── train_with_from_to_stations.csv
│   │   └── ...
│   ├── ui/                         # Frontend files
│   │   ├── index.html
│   │   └── central-railway-LOGO.png
│   ├── app.py                      # FastAPI application
│   ├── auth.py                     # Authentication logic
│   ├── db_config.py                # Database configuration
│   ├── requirements.txt            # Python dependencies
│   ├── .env                        # Python env vars (SERVER CREDENTIALS)
│   └── ...
│
└── logs/                           # Optional: Centralized logs
    ├── bbtro.log
    └── rtis.log
```

---

## Server .env Files

### /home/railway/bbtro/.env
```env
# Node.js application
PORT=3000
SESSION_SECRET=your_session_secret
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=production_user
MYSQL_PASSWORD=production_password
MYSQL_DATABASE=bbtro
```

### /home/railway/rail-data-app/.env
```env
# Python FastAPI application
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=bbtro
MYSQL_USER=production_user
MYSQL_PASSWORD=production_password
```

---

## Systemd Services

### /etc/systemd/system/bbtro.service
```ini
[Unit]
Description=BBTRO Node.js Application
After=network.target mysql.service

[Service]
Type=simple
User=railway
WorkingDirectory=/home/railway/bbtro
Environment="NODE_ENV=production"
ExecStart=/usr/bin/node server.js
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/rtis.service
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

---

## Deployment Workflow

### Initial Setup (One-time)

```bash
# SSH to server
ssh railway@your-server-ip

# Create directories
mkdir -p /home/railway/bbtro
mkdir -p /home/railway/rail-data-app
mkdir -p /home/railway/logs

# Clone bbtro (Node.js app)
cd /home/railway/bbtro
git clone https://github.com/jaynair0405/bbtro.git .
npm install
cp .env.example .env
nano .env  # Add production credentials

# Clone rail-data-app (Python app)
cd /home/railway/rail-data-app
git clone https://github.com/jaynair0405/rail-data-app.git .
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env  # Add production credentials
chmod 600 .env

# Set up systemd services
sudo cp /path/to/bbtro.service /etc/systemd/system/
sudo cp /path/to/rtis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bbtro rtis
sudo systemctl start bbtro rtis
```

### Regular Updates

```bash
# Update bbtro
cd /home/railway/bbtro
git pull origin main
npm install  # If package.json changed
sudo systemctl restart bbtro

# Update rail-data-app
cd /home/railway/rail-data-app
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt  # If requirements changed
sudo systemctl restart rtis
```

---

## Port Configuration

| Service | Port | Access |
|---------|------|--------|
| bbtro (Node.js) | 3000 | Public (via nginx/Apache) |
| rtis (FastAPI) | 8765 | **localhost only** (proxied by bbtro) |
| MySQL | 3306 | localhost only |

---

## Nginx Configuration (if using)

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

---

## Security Checklist

✅ `.env` files have permissions `chmod 600`
✅ FastAPI runs on localhost:8765 (not public)
✅ Only bbtro is exposed publicly
✅ MySQL allows only localhost connections
✅ Firewall allows only ports 80, 443, 22
✅ Different credentials for dev and production
✅ Regular backups of MySQL database

---

## Verification Commands

```bash
# Check services status
sudo systemctl status bbtro
sudo systemctl status rtis

# Check if ports are listening
sudo netstat -tlnp | grep -E '3000|8765'

# Check logs
sudo journalctl -u bbtro -f
sudo journalctl -u rtis -f

# Test bbtro
curl http://localhost:3000

# Test RTIS (through proxy)
curl http://localhost:3000/spm/rtis/
```

---

## Common Issues

**Issue: "Connection refused" on port 8765**
- Check if rtis service is running: `sudo systemctl status rtis`
- Check logs: `sudo journalctl -u rtis -n 50`

**Issue: "Session not found" errors**
- Verify MySQL connection in both apps
- Check session table exists: `mysql -u user -p bbtro -e "SHOW TABLES LIKE 'sessions';"`

**Issue: Static files not loading**
- Check file permissions: `ls -la /home/railway/rail-data-app/ui/`
- Verify paths in app.py: `app.mount("/ui", StaticFiles(...))`

---

## File Permissions

```bash
# Set correct ownership
sudo chown -R railway:railway /home/railway/bbtro
sudo chown -R railway:railway /home/railway/rail-data-app

# Protect sensitive files
chmod 600 /home/railway/bbtro/.env
chmod 600 /home/railway/rail-data-app/.env

# Make sure directories are accessible
chmod 755 /home/railway/bbtro
chmod 755 /home/railway/rail-data-app
```
