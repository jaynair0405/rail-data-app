# GPS / Distance Spike Handling

## Background
Some raw RTIS telemetry streams contain long GPS dropouts. When the receiver comes back online it back-fills cumulative distance (`distFromPrevLatLng`) by comparing the restart coordinates to the last known point. This often produces a single record with an unrealistic jump (e.g., ~9 000 km) even though the train actually covered only a few kilometers. Similar glitches in the OBU firmware can temporarily report 200–250 km/h while the train is cruising at 80 km/h. These spikes drag the speed-profile chart's Y-axis to huge ranges, compressing the real operating speed into a small band and misplacing halt detection markers that rely on the cumulative distance.

## Goals
- Ignore bogus distance deltas introduced by GPS restarts so cumulative distance stays realistic and halts remain correctly spaced (500 m rule).
- Clamp outlier speed samples to a “continuous” speed that reflects the current motion so the chart axis remains useful.
- Capture enough metadata (e.g., flagged rows, corrected deltas) so downstream modules such as braking profile, halt mapping, and PDF exports can explain when auto-fixes were applied.

## Detection Signals
1. **Time gap** – `Logging Time` jumps forward by several minutes with no intermediate rows.
2. **Distance spike** – `distFromPrevLatLng` (or fallback `distFromSpeed`) exceeds a configurable threshold (e.g., >2 km between consecutive samples or >3× rolling median) inside a short time window.
3. **Speed spike** – instantaneous speed deviates dramatically from the rolling median/mean (e.g., >60 km/h difference) without matching distance progression.
4. **Station continuity** – station codes remain unchanged while distance jumps thousands of meters, hinting that the reading is not a real halt.

## Proposed Remediation
- **Distance normalization**
  - Track cumulative distance using “trusted deltas” only.
  - When a spike is detected, replace the raw step with an extrapolated value derived from the last known good average speed × elapsed time.
  - Store both `raw_distance_step` and `corrected_distance_step` for audit.
- **Speed smoothing**
  - Maintain a rolling median speed (e.g., 5–10 samples).
  - If `abs(spike_speed - median) > threshold`, clamp to the median or linearly interpolate between surrounding good samples.
  - Emit a flag (e.g., `speed_corrected=True`) so charts can display optional annotations.
- **Segment flagging**
  - Whenever we auto-correct a block, emit a summary entry (`start_ts`, `end_ts`, reason) to surface in QA dashboards or downloadable logs.

## Next Steps
1. Build a preprocessing pass that consumes raw CSV rows and outputs “sanitized” rows with the corrections above.
2. Integrate the pass before `_detect_halt_markers_from_enriched` and `_braking_profile` so all downstream features benefit.
3. Provide unit tests using synthetic datasets that mimic:
   - GPS dropout with a huge distance spike.
   - Sudden 250 km/h speed spike mid-cruise.
   - Combined dropout + spike to ensure the logic handles both gracefully.

This document will guide the upcoming implementation so we can discuss thresholds, metadata schema, and UI surfacing before touching the code.
