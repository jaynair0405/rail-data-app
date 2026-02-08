-- ============================================================
-- Migration: Add UNIQUE constraint for race protection
-- Purpose: Prevent duplicate analyses for the same trip
-- Date: 2026-02-06
-- ============================================================

-- Business key: one analysis per trip (working_date + train + route + loco)
-- If you prefer "one per trip regardless of loco," drop loco_number from the key.

ALTER TABLE div_rtis_analyses
  ADD CONSTRAINT uniq_analysis_trip
  UNIQUE (working_date, train_number, from_station, to_station, direction, route, loco_number);

-- Note: This creates an implicit index; existing idx_from_to can be kept for
-- queries that don't include all key columns.
