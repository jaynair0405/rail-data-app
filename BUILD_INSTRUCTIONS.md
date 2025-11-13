# Building Windows EXE - Step by Step Guide

## Prerequisites

1. **Windows 10 or later**
2. **Python 3.10 or later** installed
   - Download from: https://www.python.org/downloads/
   - ⚠️ During installation, check "Add Python to PATH"

---

## Quick Build (5 Steps)

### Step 1: Open PowerShell or Command Prompt
```
Press Win+R → type "cmd" → Enter
```

### Step 2: Navigate to project folder
```bash
cd C:\path\to\rail-data-app
```

### Step 3: Create virtual environment & activate
```bash
python -m venv .venv
.venv\Scripts\activate
```

You should see `(.venv)` in your prompt.

### Step 4: Install dependencies
```bash
pip install -r requirements.txt
pip install pyinstaller
```

This takes ~5 minutes (downloads libraries).

### Step 5: Build the EXE
```bash
pyinstaller RailDataApp.spec
```

This takes ~3-5 minutes. Watch for errors.

---

## Output Location

After successful build:

```
dist/
  ├── RailDataApp.exe        ← Your application!
  ├── base-data/             ← Reference data (auto-copied)
  └── ui/                    ← Web interface (auto-copied)
```

---

## Testing the EXE

### Test 1: Run from dist folder
```bash
cd dist
RailDataApp.exe
```

- Browser should open automatically to http://localhost:8765
- If it doesn't, manually open: http://localhost:8765/ui/

### Test 2: Upload a CSV file
- Click "Choose File"
- Select a telemetry CSV
- Click "Load CSV"
- Should see "CSV loaded" message

### Test 3: Generate PDF
- Enter train number
- Click "Fetch Train"
- Click "Analyze"
- Click "Export PDF"
- PDF should download

---

## Creating Distribution Package

### Step 1: Create release folder
```bash
mkdir RailDataAnalysis_v1.0.0
```

### Step 2: Copy files
```bash
copy dist\RailDataApp.exe RailDataAnalysis_v1.0.0\
xcopy dist\base-data RailDataAnalysis_v1.0.0\base-data\ /E /I
xcopy dist\ui RailDataAnalysis_v1.0.0\ui\ /E /I
copy mail_staff.csv RailDataAnalysis_v1.0.0\
copy "cli data for upload - Sheet1.csv" RailDataAnalysis_v1.0.0\
```

### Step 3: Add documentation
Create `RailDataAnalysis_v1.0.0\README.txt`:
```
CR RTIS Analysis Tool v1.0.0
============================

INSTALLATION:
1. Copy this folder to your computer
2. Double-click RailDataApp.exe
3. Browser opens automatically

USAGE:
- Upload CSV file
- Analyze train performance
- Export PDF reports

No internet required!
```

### Step 4: Zip for distribution
Right-click folder → Send to → Compressed (zipped) folder

Result: `RailDataAnalysis_v1.0.0.zip` ready for USB!

---

## Troubleshooting

### Error: "Python not found"
**Solution:** Install Python and check "Add to PATH" during installation

### Error: "pip not found"
**Solution:**
```bash
python -m ensurepip --upgrade
```

### Error: "Permission denied"
**Solution:** Run PowerShell/CMD as Administrator

### Error: "Module not found" when running EXE
**Solution:** Add module to `hiddenimports` in `RailDataApp.spec`, rebuild

### EXE is too large (>200 MB)
**Solution:** This is normal for first build. To reduce:
1. Set `upx=True` in spec file (already done)
2. Remove unused dependencies from requirements.txt

### Console window stays open
**Solution:** In `RailDataApp.spec`, change:
```python
console=False  # No console window (production)
```

### Want to change app icon
**Solution:**
1. Get a .ico file (e.g., railway-logo.ico)
2. In `RailDataApp.spec`, change:
```python
icon='railway-logo.ico'
```

---

## Rebuilding After Code Changes

Simple process:

```bash
# 1. Activate venv (if not already)
.venv\Scripts\activate

# 2. Rebuild
pyinstaller RailDataApp.spec

# 3. Test
cd dist
RailDataApp.exe
```

That's it! Takes ~2-3 minutes for rebuild.

---

## Version Management

### Update version number

In `app.py`, add near the top:
```python
__version__ = "1.0.0"
```

### Rename EXE with version
```bash
copy dist\RailDataApp.exe RailDataApp_v1.0.0.exe
```

### Keep changelog
Create `CHANGELOG.txt`:
```
v1.0.0 (2024-11-15)
- Initial release
- PDF export with smooth braking curves
- Speed profile analysis
- Brake test validation
```

---

## Advanced: GitHub Actions (Optional)

If you want automated builds on every commit:

1. Push code to GitHub
2. Create `.github/workflows/build.yml` (I can provide this)
3. Every commit triggers Windows build automatically
4. Download EXE from "Releases" tab

Let me know if you want to set this up!

---

## Size Reference

Expected EXE sizes:
- **RailDataApp.exe:** 80-120 MB
- **With UPX compression:** 60-90 MB
- **Full package (zip):** 65-95 MB

This is normal for Python apps with matplotlib + pandas + polars.

---

## Support

If build fails:
1. Check Python version: `python --version` (should be 3.10+)
2. Check pip: `pip --version`
3. Try clean build: Delete `build/` and `dist/` folders, rebuild
4. Check antivirus (sometimes blocks PyInstaller)

Still stuck? Share the error message!
