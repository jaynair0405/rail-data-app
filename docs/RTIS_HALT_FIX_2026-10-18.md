# Local RTIS halt correction - 18 October 2026

Status: implemented and verified locally. No deployment or database writes.

## Reproduction and result

Train 12110, 16 October 2026, MMR to CSMT (UP). The supplied production PDF
contains only LS and DVL. Replaying its 17,922-row interval reproduced both
halts and every displayed approach speed exactly.

The strict detector required a crossing below 0.5 km/h followed by ten rows at
or below 0.3 km/h within a fifteen-row window. RTIS fluctuations prevented
confirmation at the missing stops, even at NK where a later 13-second low-speed
run exists. The destination filter also ended at the first CSMT reading below
1 km/h, leaving no arrival confirmation data.

The corrected filtered interval is 06:02:16 to 11:06:48 (18,021 rows). The web
braking table, PDF and full-journey chart now share these confirmed checkpoints:

| Raw CSV code | Display | Checkpoint time |
|---|---|---|
| LS | LS | 06:27:19 |
| NR | NR | 06:41:21 |
| NK | NK | 07:11:21 |
| DVL | DVL | 07:22:24 |
| IGP | IGP | 08:07:01 |
| X102 | TGR2 | 08:19:14 |
| X103 | TGR3 | 08:30:43 |
| KSRA | KSRA | 08:45:03 |
| KYN | KYN | 09:52:20 |
| TNA | TNA | 10:16:57 |
| TNA | TNA | 10:24:57 |
| DR | DR | 10:48:42 |
| CSMT | CSMT | 11:05:54 |

There are 12 distinct location codes and 13 stop events. The two TNA-coded
stops are roughly 750 m apart, separated by sustained movement. They are kept
separately, with times in the PDF table and curve legend. Their operational
classification as station/approach/signal needs reference-data review; a CSV
station code alone does not prove a scheduled station stop.

User-confirmed display mapping: X101 -> TGR1, X102 -> TGR2, X103 -> TGR3.
Raw codes are retained for geofences, ghat policy, routing and persistence.
X101 is not a stop in this journey.

## Implementation

- `rtis_halts.py` supplies the shared detector and display aliases.
- A strict low-speed checkpoint is retained when confirmed using timestamps
  and available GPS. This preserves all six original LS and DVL readings.
- Otherwise a rolling 20-second window requires speeds <=3 km/h, median
  <=1 km/h, at least one <=0.5 reading, and GPS positions within 5 m of the
  window's first position. Confirmation continues throughout the dwell.
- Missing/invalid data, non-increasing timestamps and gaps over 3 seconds
  break confirmation. Missing speed is never treated as zero for detection.
- Ten seconds of sustained travel >=5 km/h rearms detection for another stop.
  The origin dwell is not treated as an arrival. A noise-tolerant checkpoint
  is anchored back to the first nearby low reading in its continuous low-speed
  episode (within 20 m), avoiding a later dwell checkpoint solely due to jitter.
- Destination filtering examines at most 180 additional seconds, returning the
  first matching confirmed destination event's confirmation boundary.
- Chart markers, web and PDF braking tables, violations and nominated-halt
  persistence use the shared events. Existing nominated geofences still scope
  which events are saved to that separate database report.
- Short approaches remain visible in both tables; offsets without enough data
  return unavailable readings instead of a misleading closer speed.
- PDF-only spacing deduplication was removed so it cannot silently discard
  events retained by the shared detector.

Existing PSR/signal overlay edits were retained. PSR thresholds, ghat assessment
policy, CSV timestamp parsing elsewhere, and historical records were not changed.

## Validation and limits

19 regression checks pass, including the supplied CSV, preservation of LS/DVL
readings, chart/PDF/web agreement, destination retention, short approaches,
ghat classification, noisy stops, moving GPS, brief dips, missing data, duplicate
times, logging gaps and repeated stops. Browser verification confirmed 13 table
rows, TGR2/TGR3 labels, CSMT and a rendered performance chart. All six pages of
the generated PDF were visually inspected.

Run using the project's installed dependencies:

```sh
MPLCONFIGDIR=/tmp/rail-matplotlib-cache .venv/bin/python -m unittest discover -s tests -v
```

A temporary clean environment with the pinned requirements was used for this
run because the existing environment was slow to import. Database access was
explicitly disabled in the integration harness and loopback browser fixture.
Live signal/WTT lookup, production authentication and database writes were not
tested. The supplied real CSV plus synthetic negative cases are regression
coverage, not certification across all RTIS devices or operating conditions.
Review additional representative journeys before production release.

Review PDF: `output/pdf/12110-16-10-2026-local-halt-review.pdf`.
Browser evidence: `output/pdf/12110-local-halt-table.jpg`.

Release files, after review: `app.py`, `rtis_halts.py`, `ui/index.html`.
The new module must accompany `app.py`. No Git commit or push was made.

## Additional validation: 12102.csv

The 12102 fixture exposed repeated, identical timestamp/speed/GPS observations
that reset confirmation and delayed checkpoints. `rtis_halts.py` now ignores
these identical retransmissions for confirmation, mapping results back to the
original dataframe indices. Conflicting same-time readings still break a
confirmation window, and repeated rows do not supply elapsed time. Raw source
rows and the existing distance calculation remain unchanged.

The updated suite has 25 passing checks across 12110, 17618 and 12102 plus
synthetic negatives. 12102 has ten stop events and a confirmed LTT arrival at
13:16:18 (selected interval ends at 13:16:38). Minute-scale checkpoint delays
at IGP/KYN/LTT are resolved. Details: `output/12102-halt-review.md`.

## Crew origin and passing route references

The chart previously showed only newly confirmed stop events, so a crew-origin
station could disappear when slicing began at departure. Chart payloads now
keep `route_markers` / `reference_labels` separate from `halt_markers` /
`station_labels`. The origin is added only if the first observed station code
matches the selected origin. It is labelled `(start)` and is not a braking halt.

Confirmed cases: 12138, 12102 and 12106 start at IGP; 17618 and 12110 start at
MMR. X101 in 12138 spans 06:29:11-06:30:11 with minimum speed 13.95 km/h, so it
is a passing reference, not an inferred halt. X101/X102/X103 visits without a
confirmed stop are shown as TGR1/TGR2/TGR3 `(pass)`, at the first CSV code
observation for that visit. This timestamp is a CSV reference and does not
claim a surveyed physical passing point. A confirmed ghat stop gets no duplicate
passing reference. References use blue dotted lines in browser and native charts.

All five supplied charts were regenerated with origin markers. Existing stop
counts remain 8 (12138), 10 (12102), 7 (12106), 20 (17618), 13 (12110).
27 checks pass, including origin/reference separation, observed-origin guards
and preservation of confirmed stop counts and speeds. Updated chart artifacts
end in `-speed-chart-with-origin.png`; earlier chart images remain available as
snapshots. Production remains unchanged.

Browser verification of the origin/reference change confirmed both labels on
the 12138 performance chart and exactly eight braking-table rows. The speed
trace is explicitly blue in the UI, matching the native chart, so it remains
distinguishable from the green MPS/PSR area. Evidence:
`output/charts/12138-browser-chart-with-origin.jpg`. Database access was disabled
in the temporary loopback fixture; the fixture server was stopped after review.

## Current crew departure boundary (supersedes the earlier start timestamps)

The user clarified that the incoming arrival and station dwell must remain
excluded. The selected/validated departure is unchanged, but a generic
`departure_anchor` now inspects up to 60 seconds of preceding same-origin
evidence to recover only the last verified stationary reading. Earlier rows
are used for verification only and are not returned in the analysis. Gaps,
invalid readings, another station or intervening movement prevent recovery.
Without adequate evidence the existing observed departure is retained.

The first chart point now uses the actual first source speed, rather than the
last speed in that minute, which may already be accelerating. `start_sample`
records its exact timestamp and speed. Missing/non-finite chart speeds remain
gaps; no zero is generated for appearance.

| Train | Crew origin | First source time | First source speed km/h |
|---|---|---|---|
| 12138 | IGP | 17 Oct 06:24:13 | 0.00 |
| 12102 | IGP | 17 Oct 09:19:59 | 0.00 |
| 12106 | IGP | 17 Oct 04:42:44 | 0.46 |
| 17618 | MMR | 16 Oct 17:07:24 | 0.24 |
| 12110 | MMR | 16 Oct 06:02:12 | 0.15 |

The residual near-zero observations are retained as recorded; they are not
rounded to zero in the source or chart data. Stop counts and the original
LS/DVL approach readings remain unchanged. The 32-check suite passes, including
origin recovery for arbitrary station names, no recovery across data gaps,
rejection of a brief isolated zero, first-source-point plotting, missing-speed
rendering and the five existing fixture counts. The browser fixture confirmed
eight braking rows for 12138 after this change. The temporary server is stopped
after verification. New image snapshots end in `-crew-departure.png`.
