# Ghat Speed Violation Policy — 800m Assessment (2026-08-06)

**Internal document — do not share with running staff.**

## Background

On 06.08.2026 a discrepancy was found between the Daily Speed Violations Report
and the individual LP report PDFs for ghat-section halts (03.08.2026 data):

| Halt | LP report showed | Daily report showed | Cause |
|---|---|---|---|
| X103 (12140, MA Ghawte) | 44.0 | 41.9 | LP PDF flagged the 1000m speed; daily report sampled 900m. Both readings were genuine — ghat descents are non-monotonic (brake/release sawtooth), so speeds at different distances differ in any order. |
| T5 (11302, K P Subramanian) | 35.6 | 45.8 | RTIS logger went silent for 34s (301m in one record). With no samples between 694m and 995m before the halt, the "900m" lookup snapped to the 694m sample (45.78 km/h) with no tolerance check — a 206m labelling error. True 900m speed was ~38.8 km/h. |

A third latent bug: the 800m rule for T5 stations only matched station codes
KAD/NNCN, but RTIS logs the halt as literally `T5`, so it silently fell back
to 900m.

## Policy (from 06.08.2026)

1. **All ghat halts are assessed at 800m before the halt, 40 km/h limit
   (violation at >= 41), for every ghat station** — no per-station distances.
2. **The assessment distance is NOT displayed anywhere staff-facing.**
   The 800m speed is shown in the braking table's "1000 m" column for ghat
   (▲) rows, in both the LP report PDF and the web braking table, with the
   footnote saying only "▲ Ghat section — 40 km/h limit". Rationale: if crews
   knew the exact checkpoint they would control speed only from 800m instead
   of through the whole approach.
3. **GPS logging gaps are interpolated.** If the nearest GPS sample is more
   than 25m short of a requested offset distance, the speed is linearly
   interpolated between the bracketing samples and the reading is marked
   `interpolated: true` with `gap_m` in the internal JSON (never rendered).
   This prevents T5-type false violations.
4. All three surfaces — daily violations detection (`_detect_violations`),
   LP report PDF (`_render_pdf_report`), and the web braking table
   (`/braking_profile`) — read the identical 800m value, so the reports can
   no longer disagree.

## Implementation (app.py)

- `GHAT_DISTANCE = 800` — the single assessment distance
- `VIOLATIONS_OFFSETS = BRAKE_OFFSETS + [800]` — adds the 800m sample
- `OFFSET_GAP_INTERPOLATION_M = 25.0` and `_offset_reading()` — gap-aware
  speed lookup shared by `_braking_profile` and `_braking_profile_full_curve`
- Table columns (`BRAKE_OFFSETS`) are unchanged; only ghat rows substitute
  the 800m reading into the 1000m column

Commits: `ab2f009` (fix + interpolation), `d9bd702` (hide 800m markers).

## Historical data

Violations saved in `div_rtis_violations` before 06.08.2026 were computed
under the old 900m/no-tolerance logic and are intentionally left as-is.
Stored LP report PDFs are historical snapshots. Re-saving an old analysis
regenerates its violations under the new policy.
