# RTIS Memory Optimization

## Problem

RTIS memory usage was growing from ~200MB at startup to 1.8GB+ over time, causing performance issues.

### Root Cause

The `RUNS` dictionary stored uploaded DataFrames **in RAM** for 6 hours:

```python
# Old implementation
RUNS[run_id] = {
    "df": df,  # Full DataFrame in memory
    "uploaded_at": time.time(),
    ...
}
RUN_TTL_SECONDS = 60 * 60 * 6  # 6 hours
```

Each upload added ~7-8MB to memory, accumulating throughout the day.

---

## Solution

### Changes Made (2026-04-20)

1. **Store DataFrames as temp files instead of RAM**
   - DataFrames saved as `.parquet` files in `/tmp/rtis_runs/`
   - Only file path stored in `RUNS` dict
   - DataFrame loaded on-demand when needed

2. **Reduced TTL from 6 hours to 1 hour**
   - Users typically finish analysis within 1 hour
   - Can re-upload if needed (quick operation)

3. **Added MAX_RUNS limit of 20**
   - Prevents unbounded growth
   - Oldest run evicted when limit reached

4. **Added orphan file cleanup**
   - On startup: cleans files from previous crashes
   - During purge: removes expired temp files

---

## Implementation Details

### File Storage Location
```
/tmp/rtis_runs/
├── {run_id_1}.parquet
├── {run_id_2}.parquet
└── ...
```

### Memory vs Disk Comparison

| Aspect | Before (RAM) | After (Disk) |
|--------|--------------|--------------|
| Memory usage | 1.8GB+ | ~200MB |
| Max disk usage | N/A | ~100MB (20 files x 5MB) |
| Read latency | Instant | ~100-200ms |
| Survives restart | No | Yes |

### Key Functions Modified

| Function | Change |
|----------|--------|
| `_set_user_run()` | Saves DF to parquet file, stores path |
| `_get_user_run()` | Loads DF from parquet file on demand |
| `_purge_expired_runs()` | Also deletes temp files |
| `_cleanup_orphan_files()` | New - cleans old files on startup |

---

## Configuration

```python
RUN_TTL_SECONDS = 60 * 60 * 1  # 1 hour
MAX_RUNS = 20                   # Max concurrent runs
RUNS_TEMP_DIR = "/tmp/rtis_runs"
```

To adjust, modify these constants in `app.py` (lines ~62-65).

---

## Monitoring

Check memory usage:
```bash
pm2 status
```

Check temp files:
```bash
ls -la /tmp/rtis_runs/
du -sh /tmp/rtis_runs/
```

Clean temp files manually (if needed):
```bash
rm -rf /tmp/rtis_runs/*
```

---

## Rollback

If issues occur, revert to in-memory storage:

1. `git revert HEAD` or restore from backup
2. `pm2 restart rtis`

Note: In-memory approach will have high memory usage again.
