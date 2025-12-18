# Changes Required in bbtro Folder

**These changes need to be copied to your actual bbtro folder**

---

## File 1: package.json

### Add Dependency

**Location:** Line 18 (in dependencies section)

```json
{
  "dependencies": {
    "bcrypt": "^6.0.0",
    "csv-parser": "^3.2.0",
    "dotenv": "^17.2.2",
    "express": "^5.1.0",
    "express-session": "^1.18.2",
    "express-mysql-session": "^3.0.3",  ← ADD THIS LINE
    "multer": "^2.0.2",
    "mysql2": "^3.14.2",
    "xlsx": "^0.18.5"
  }
}
```

---

## File 2: server.js

### Change 1: Add Import (after line 22)

**Find:**
```javascript
const session = require('express-session');
```

**Replace with:**
```javascript
const session = require('express-session');
const MySQLStore = require('express-mysql-session')(session);
```

---

### Change 2: Replace Session Configuration (around line 31-40)

**Find:**
```javascript
app.use(express.json());

app.use(session({
  //secret: 'railway-bbtro-secret-key-2025',
  secret: process.env.SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: {
      maxAge: 8 * 60 * 60 * 1000, // 8 hours
      secure: false // Keep false for development (http)
  }
}));
```

**Replace with:**
```javascript
// MySQL Session Store Configuration
const sessionStoreOptions = {
  host: process.env.DB_HOST,
  port: 3306,
  user: process.env.DB_USER,
  password: process.env.DB_PASSWORD,
  database: process.env.DB_NAME,
  clearExpired: true,
  checkExpirationInterval: 900000, // 15 minutes
  expiration: 8 * 60 * 60 * 1000, // 8 hours
  createDatabaseTable: true,
  schema: {
    tableName: 'sessions',
    columnNames: {
      session_id: 'session_id',
      expires: 'expires',
      data: 'data'
    }
  }
};

const sessionStore = new MySQLStore(sessionStoreOptions);

app.use(express.json());

// Session middleware with MySQL store
app.use(session({
  key: 'connect.sid',
  secret: process.env.SESSION_SECRET,
  store: sessionStore,
  resave: false,
  saveUninitialized: false,
  cookie: {
      maxAge: 8 * 60 * 60 * 1000, // 8 hours
      secure: false, // Keep false for development (http)
      httpOnly: true,
      sameSite: 'lax'
  }
}));
```

---

## After Making Changes

### 1. Install new dependency:
```bash
cd [your-bbtro-folder]
npm install
```

### 2. Test locally:
```bash
node server.js
```

### 3. Login and verify session in MySQL:
```sql
USE bbtro;
SELECT session_id, FROM_UNIXTIME(expires/1000) AS expires_at
FROM sessions
ORDER BY expires DESC
LIMIT 5;
```

### 4. Commit and push:
```bash
git add package.json server.js
git commit -m "Add MySQL session store for shared authentication"
git push origin main
```

---

## Reference Files

The complete modified files are in:
- `bbtro/package.json` (in rail-data-app folder - reference only)
- `bbtro/server.js` (in rail-data-app folder - reference only)

**Copy these changes to your actual bbtro folder location**
