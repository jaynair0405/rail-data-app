# Environment Configuration Guide

## Local Development Setup

1. **Copy the example file:**
   ```bash
   cp .env.example .env
   ```

2. **Edit `.env` with your LOCAL credentials:**
   ```bash
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_DATABASE=bbtro
   MYSQL_USER=jay
   MYSQL_PASSWORD=4310jay
   ```

3. **Test the connection:**
   ```bash
   python -c "from db_config import get_db_connection; conn = get_db_connection(); print('✓ Database connected successfully'); conn.close()"
   ```

## Server/Production Setup

### Option 1: Using .env file on server (Recommended)

1. **SSH into your server:**
   ```bash
   ssh user@your-server-ip
   ```

2. **Navigate to your app directory:**
   ```bash
   cd /path/to/rail-data-app
   ```

3. **Create `.env` file on server:**
   ```bash
   nano .env
   ```

4. **Add SERVER credentials:**
   ```bash
   MYSQL_HOST=127.0.0.1
   MYSQL_PORT=3306
   MYSQL_DATABASE=bbtro
   MYSQL_USER=production_user
   MYSQL_PASSWORD=production_password
   ```

5. **Save and exit** (Ctrl+O, Enter, Ctrl+X in nano)

6. **Set proper permissions:**
   ```bash
   chmod 600 .env
   ```

### Option 2: Using System Environment Variables

Set environment variables in your systemd service file or shell profile:

**For systemd service (`/etc/systemd/system/rtis.service`):**
```ini
[Service]
Environment="MYSQL_HOST=127.0.0.1"
Environment="MYSQL_PORT=3306"
Environment="MYSQL_DATABASE=bbtro"
Environment="MYSQL_USER=production_user"
Environment="MYSQL_PASSWORD=production_password"
```

**For shell profile (`~/.bashrc` or `~/.profile`):**
```bash
export MYSQL_HOST=127.0.0.1
export MYSQL_PORT=3306
export MYSQL_DATABASE=bbtro
export MYSQL_USER=production_user
export MYSQL_PASSWORD=production_password
```

## Security Best Practices

✅ **DO:**
- Keep `.env` in `.gitignore` (already done)
- Use different credentials for local and production
- Set `.env` file permissions to 600 (read/write for owner only)
- Use strong passwords in production
- Never commit `.env` to git

❌ **DON'T:**
- Hardcode credentials in source code
- Share `.env` file via email/chat
- Use production credentials locally
- Commit `.env` to git repository

## Troubleshooting

**Error: "Access denied for user"**
- Check username and password in `.env`
- Verify database user exists: `mysql -u username -p`

**Error: "Can't connect to MySQL server"**
- Check MYSQL_HOST and MYSQL_PORT
- Verify MySQL is running: `sudo systemctl status mysql`
- Check firewall rules

**Error: "Unknown database"**
- Verify database exists: `mysql -u username -p -e "SHOW DATABASES;"`
- Create database if needed: `CREATE DATABASE bbtro;`

## Deployment Workflow

1. **Local Development:**
   ```bash
   # Use local .env with local credentials
   python app.py
   ```

2. **Push to Git:**
   ```bash
   git add .
   git commit -m "Update code"
   git push origin main
   ```
   ⚠️ `.env` is NOT pushed (in .gitignore)

3. **Deploy to Server:**
   ```bash
   ssh user@server
   cd /path/to/rail-data-app
   git pull origin main
   # .env with production credentials already exists on server
   sudo systemctl restart rtis
   ```

## Checking Current Configuration

**See loaded values (without showing passwords):**
```python
python3 << 'PYTHON'
import os
from dotenv import load_dotenv
load_dotenv()

print(f"MYSQL_HOST: {os.getenv('MYSQL_HOST')}")
print(f"MYSQL_PORT: {os.getenv('MYSQL_PORT')}")
print(f"MYSQL_DATABASE: {os.getenv('MYSQL_DATABASE')}")
print(f"MYSQL_USER: {os.getenv('MYSQL_USER')}")
print(f"MYSQL_PASSWORD: {'***' if os.getenv('MYSQL_PASSWORD') else 'NOT SET'}")
PYTHON
```
