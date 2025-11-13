# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller specification file for CR RTIS Analysis Tool
=========================================================

Usage:
    pyinstaller RailDataApp.spec

This will create:
    - dist/RailDataApp.exe (main executable)
    - External folders: base-data/, ui/, staff CSVs

Distribution structure:
    RailDataApp.exe
    base-data/          (external - users can update)
    ui/                 (external - contains HTML/logo)
    mail_staff.csv      (external)
    cli data for upload - Sheet1.csv (external)
"""

import sys
from pathlib import Path

block_cipher = None

# Get the project directory
spec_root = Path(SPECPATH)

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Keep UI and base-data external for easy updates
        # These will be copied to dist/ folder, not bundled in EXE
        ('ui', 'ui'),
        ('base-data', 'base-data'),
    ],
    hiddenimports=[
        # Explicitly include these modules (PyInstaller sometimes misses them)
        'polars',
        'pandas',
        'fastapi',
        'uvicorn',
        'matplotlib',
        'matplotlib.backends.backend_agg',
        'reportlab',
        'reportlab.platypus',
        'reportlab.lib',
        'numpy',
        'xlsxwriter',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclude unused modules to reduce size
        'tkinter',
        'PyQt5',
        'PySide2',
        'PIL.ImageQt',
        'IPython',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='RailDataApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,  # Compress EXE (reduces size by ~30-40%)
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Show console window (useful for debugging; set False for production)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Add your icon file here if you want: icon='icon.ico'
)

# Note: This creates a one-file EXE
# All Python dependencies are bundled inside RailDataApp.exe
# base-data/ and ui/ folders remain external
