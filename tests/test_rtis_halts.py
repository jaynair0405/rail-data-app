"""Offline regressions: python3 -m unittest discover -s tests -v."""
import csv
from datetime import datetime, timedelta
from pathlib import Path
import unittest

from rtis_halts import detect_halts, departure_anchor, halt_label


class HaltDetectionTests(unittest.TestCase):
    def test_departure_anchor_is_last_standstill_not_the_arrival_or_dwell(self):
        speeds = [30.0] * 15 + [0.0] * 120 + [1.8, 3, 6]
        times = [datetime(2026, 10, 17) + timedelta(seconds=i) for i in range(len(speeds))]
        for origin in ['IGP', 'MMR', 'DR']:
            self.assertEqual(departure_anchor(speeds, times, 135, [origin] * len(speeds), origin,
                                             [19] * len(speeds), [73] * len(speeds)), 134)

    def test_departure_anchor_does_not_cross_missing_departure_data(self):
        speeds = [0.0] * 30 + [25.0]
        times = [datetime(2026, 10, 17) + timedelta(seconds=i) for i in range(30)]
        times.append(times[-1] + timedelta(seconds=60))
        self.assertEqual(departure_anchor(speeds, times, 30, ['IGP'] * 31, 'IGP'), 30)

    def test_departure_anchor_does_not_promote_a_brief_zero_to_standstill(self):
        speeds = [20.0] * 30 + [0.0, 1.8]
        times = [datetime(2026, 10, 17) + timedelta(seconds=i) for i in range(len(speeds))]
        self.assertEqual(departure_anchor(speeds, times, 31, ['MMR'] * len(speeds), 'MMR'), 31)

    def journey(self, tail, interval=1, moving_gps=False):
        speeds = [30.0] * 15 + tail
        times = [datetime(2026, 10, 16) + timedelta(seconds=i * interval) for i in range(len(speeds))]
        lats = [19.0 + (i * 0.000004 if moving_gps else 0) for i in range(len(speeds))]
        return speeds, times, lats, [73.0] * len(speeds)

    def test_noisy_stationary_stop(self):
        self.assertEqual(len(detect_halts(*self.journey([0.2, 0.7, 1.2, 0.4] * 30))), 1)

    def test_stable_stop_without_gps(self):
        speeds, times, _, _ = self.journey([0.1] * 30)
        self.assertEqual(len(detect_halts(speeds, times)), 1)

    def test_later_stationary_window_is_not_missed(self):
        events = detect_halts(*self.journey([0.4, 0.2, 0.6] * 10 + [0.1] * 30))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['method'], 'strict')

    def test_slow_movement_with_speed_noise_is_not_a_stop(self):
        self.assertEqual(detect_halts(*self.journey([0.4, 1.4, 1.8, 1.2] * 30, moving_gps=True)), [])

    def test_station_crawl_with_gps_drift_is_a_stop(self):
        # 11041 CCH 20 Oct 2026: about 1 km/h with GPS creeping ~0.4 m/s.
        speeds, times, lats, lons = self.journey([0.2, 0.7, 1.2, 0.4] * 10, moving_gps=True)
        self.assertEqual(detect_halts(speeds, times, lats, lons), [])
        events = detect_halts(speeds, times, lats, lons, [False] * 15 + [True] * 40)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['method'], 'gps_stationary')

    def test_station_crawl_at_one_kmh_is_not_a_stop(self):
        speeds, times, lats, lons = self.journey(([1.0] * 9 + [0.5]) * 6, moving_gps=True)
        self.assertEqual(detect_halts(speeds, times, lats, lons, [True] * len(speeds)), [])

    def test_station_fence_does_not_relax_speed_conditions(self):
        speeds, times, lats, lons = self.journey([1.5, 2.0, 1.8] * 15, moving_gps=True)
        self.assertEqual(detect_halts(speeds, times, lats, lons, [True] * len(speeds)), [])

    def test_brief_dip_is_not_a_stop(self):
        self.assertEqual(detect_halts(*self.journey([0.1] * 8 + [30.0] * 30)), [])

    def test_origin_stationary_period_is_not_an_arrival(self):
        times = [datetime(2026, 10, 16) + timedelta(seconds=i) for i in range(60)]
        self.assertEqual(detect_halts([0.1] * 60, times, [19] * 60, [73] * 60), [])

    def test_missing_speed_does_not_become_zero(self):
        self.assertEqual(detect_halts(*self.journey([None] * 30)), [])

    def test_time_gap_cannot_confirm_stop(self):
        self.assertEqual(detect_halts(*self.journey([0.1] * 30, interval=5)), [])

    def test_duplicate_times_cannot_confirm_stop(self):
        speeds, times, lats, lons = self.journey([0.1] * 30)
        times[15:] = [times[15]] * 30
        self.assertEqual(detect_halts(speeds, times, lats, lons), [])

    def test_identical_retransmissions_do_not_delay_checkpoint(self):
        speeds, times, lats, lons = self.journey([0.1] * 30)
        baseline = detect_halts(speeds, times, lats, lons)
        repeated = [[], [], [], []]
        for i in range(len(speeds)):
            for target, source in zip(repeated, [speeds, times, lats, lons]):
                target.extend([source[i], source[i]])
        events = detect_halts(*repeated)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['index'], 2 * baseline[0]['index'])
        self.assertEqual(events[0]['confirmed_index'], 2 * baseline[0]['confirmed_index'])

    def test_conflicting_same_timestamp_readings_interrupt_confirmation(self):
        speeds, times, lats, lons = self.journey([0.1] * 30)
        repeated = [[], [], [], []]
        for i in range(len(speeds)):
            repeated[0].extend([speeds[i], speeds[i] + 0.01])
            repeated[1].extend([times[i], times[i]])
            repeated[2].extend([lats[i], lats[i]])
            repeated[3].extend([lons[i], lons[i]])
        self.assertEqual(detect_halts(*repeated), [])

    def test_rearm_after_sustained_departure(self):
        tail = [0.2, 0.7] * 30 + [30.0] * 20 + [0.2, 0.7] * 30
        self.assertEqual(len(detect_halts(*self.journey(tail))), 2)

    def test_invalid_gps_cannot_confirm_noisy_stop(self):
        speeds, times, lats, lons = self.journey([0.2, 0.7] * 30)
        self.assertEqual(detect_halts(speeds, times, [None] * len(lats), lons), [])

    def test_raw_codes_have_display_names(self):
        self.assertEqual([halt_label(c) for c in ['X101', 'X102', 'X103', 'NK']], ['TGR1', 'TGR2', 'TGR3', 'NK'])

    def test_repeated_halt_labels_can_show_time(self):
        self.assertEqual(halt_label('TNA', '2026-10-16 10:24:57'), 'TNA (10:24)')

    def test_supplied_journey(self):
        files = list((Path(__file__).resolve().parents[1] / 'issue-review').glob('*12110*22636*.csv'))
        if not files:
            self.skipTest('Private telemetry is not stored in Git')
        with files[0].open() as f:
            rows = [r for r in csv.DictReader(f) if '2026-10-16 06:02:16' <= r['Logging Time'] <= '2026-10-16 11:08:09']
        events = detect_halts(*[[r[k] for r in rows] for k in ['Speed', 'Logging Time', 'Latitude', 'Longitude']])
        codes = [rows[e['index']]['last/cur stationCode'] for e in events]
        self.assertEqual(codes, ['LS', 'NR', 'NK', 'DVL', 'IGP', 'X102', 'X103', 'KSRA', 'KYN', 'TNA', 'TNA', 'DR', 'CSMT'])
        for code, expected in [('LS', '06:27:19'), ('DVL', '07:22:24')]:
            e = events[codes.index(code)]
            self.assertTrue(rows[e['index']]['Logging Time'].endswith(expected))


if __name__ == '__main__':
    unittest.main()
