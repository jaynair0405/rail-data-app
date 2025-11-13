# GitHub Actions - Automated EXE Build (Optional)

## What is GitHub Actions?

GitHub's cloud service that automatically builds your EXE whenever you push code.

**Benefits:**
- ✅ Build from anywhere (no Windows machine needed)
- ✅ Automatic on every commit
- ✅ Build history
- ✅ Free for public repos (2000 minutes/month for private)

**When to use:**
- You don't always have Windows access
- Multiple developers
- Want automated releases

---

## Setup (One-time, ~15 minutes)

### Step 1: Push code to GitHub

If not already done:

```bash
# On your Mac
cd /Users/neeraja/Desktop/rail-data-app

# Initialize repo (if not done)
git remote add origin https://github.com/YOUR_USERNAME/rail-data-app.git
git branch -M main
git push -u origin main
```

### Step 2: Create workflow file

Create `.github/workflows/build-windows.yml`:

```yaml
name: Build Windows EXE

on:
  push:
    branches: [ main ]
    tags:
      - 'v*'  # Triggers on version tags like v1.0.0
  workflow_dispatch:  # Manual trigger

jobs:
  build:
    runs-on: windows-latest

    steps:
    - name: Checkout code
      uses: actions/checkout@v4

    - name: Set up Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'

    - name: Install dependencies
      run: |
        python -m pip install --upgrade pip
        pip install -r requirements.txt
        pip install pyinstaller

    - name: Build EXE with PyInstaller
      run: |
        pyinstaller RailDataApp.spec

    - name: Get version from git tag
      id: get_version
      shell: bash
      run: |
        if [[ $GITHUB_REF == refs/tags/* ]]; then
          echo "version=${GITHUB_REF#refs/tags/}" >> $GITHUB_OUTPUT
        else
          echo "version=dev-$(date +'%Y%m%d-%H%M%S')" >> $GITHUB_OUTPUT
        fi

    - name: Create distribution package
      shell: bash
      run: |
        mkdir -p release
        cp dist/RailDataApp.exe release/
        cp -r dist/base-data release/
        cp -r dist/ui release/
        cp mail_staff.csv release/
        cp "cli data for upload - Sheet1.csv" release/

        # Create README
        cat > release/README.txt << 'EOF'
        CR RTIS Analysis Tool ${{ steps.get_version.outputs.version }}
        ============================

        INSTALLATION:
        1. Copy this folder to your computer
        2. Double-click RailDataApp.exe
        3. Browser opens automatically to http://localhost:8765

        REQUIREMENTS:
        - Windows 10 or later
        - No internet required (works offline)

        USAGE:
        - Upload CSV telemetry file
        - Analyze train performance
        - Export PDF reports with braking analysis
        EOF

    - name: Create ZIP archive
      shell: bash
      run: |
        cd release
        7z a ../RailDataAnalysis_${{ steps.get_version.outputs.version }}.zip .

    - name: Upload artifact
      uses: actions/upload-artifact@v4
      with:
        name: RailDataApp-${{ steps.get_version.outputs.version }}
        path: RailDataAnalysis_${{ steps.get_version.outputs.version }}.zip

    - name: Create Release (on tag)
      if: startsWith(github.ref, 'refs/tags/')
      uses: softprops/action-gh-release@v1
      with:
        files: RailDataAnalysis_${{ steps.get_version.outputs.version }}.zip
        draft: false
        prerelease: false
      env:
        GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### Step 3: Commit and push

```bash
git add .github/workflows/build-windows.yml
git commit -m "Add GitHub Actions build workflow"
git push
```

---

## Using GitHub Actions

### Automatic Build on Every Push

Just push code:
```bash
git add .
git commit -m "Fixed bug in braking analysis"
git push
```

GitHub automatically:
1. Starts Windows VM
2. Installs Python + dependencies
3. Builds EXE
4. Creates ZIP package
5. Uploads to "Actions" artifacts

### Download Built EXE

1. Go to your GitHub repo
2. Click "Actions" tab
3. Click latest workflow run
4. Scroll down to "Artifacts"
5. Download `RailDataApp-dev-YYYYMMDD-HHMMSS.zip`

---

## Creating Versioned Releases

### Step 1: Tag your version

```bash
git tag -a v1.0.0 -m "Release version 1.0.0"
git push origin v1.0.0
```

### Step 2: GitHub Actions automatically:
- Builds EXE
- Creates release page
- Attaches ZIP file

### Step 3: Share release link

Users can download from:
```
https://github.com/YOUR_USERNAME/rail-data-app/releases/latest
```

---

## Build Status Badge (Optional)

Add to README.md:

```markdown
![Build Status](https://github.com/YOUR_USERNAME/rail-data-app/workflows/Build%20Windows%20EXE/badge.svg)
```

Shows green checkmark if builds succeed!

---

## Cost (Free Tier)

| Account Type | Build Minutes/Month | Cost |
|--------------|---------------------|------|
| Public repo | Unlimited | FREE |
| Private repo | 2000 minutes | FREE |
| After limit | 2000 more | $0.008/min |

**Your usage:** ~5 minutes per build
- 20 builds/month = 100 minutes (well within free tier)

---

## Troubleshooting

### Build fails on GitHub but works locally

**Check:**
1. All dependencies in `requirements.txt`?
2. Paths are relative (not absolute)?
3. No hardcoded Windows paths?

### Can't find artifact

- Wait for workflow to complete (green checkmark)
- Refresh page
- Check "Actions" tab → specific run → "Artifacts" section

### Want to trigger manual build

1. Go to "Actions" tab
2. Select "Build Windows EXE" workflow
3. Click "Run workflow" button
4. Select branch
5. Click green "Run workflow"

---

## Comparison: GitHub Actions vs Local Build

| Aspect | Local Build | GitHub Actions |
|--------|-------------|----------------|
| **Setup time** | 5 min | 15 min (one-time) |
| **Build time** | 3-5 min | 5-7 min |
| **Windows required** | Yes | No |
| **Internet required** | For dependencies | Always |
| **Cost** | Free | Free (public repo) |
| **History** | No | Yes (all builds saved) |
| **Multiple devs** | Each needs Windows | Share one workflow |

---

## Recommendation

**Start with local Windows build**, then:
- If you push code 2+ times per day → Add GitHub Actions
- If multiple people work on code → Add GitHub Actions
- If you travel and don't have Windows → Add GitHub Actions
- If it's just you, occasional updates → Stick with local

---

## Need Help Setting Up?

Let me know and I can:
1. Create the workflow file for you
2. Help debug build errors
3. Set up automated version numbering
4. Configure release notes generation

GitHub Actions is powerful but not essential for your use case!
