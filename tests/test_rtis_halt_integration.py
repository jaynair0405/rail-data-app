"""App regression checks with all database access disabled.

Run with the project environment: .venv/bin/python -m unittest discover -s tests -v
The private fixture CSV is optional and is never committed.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(importlib.util.find_spec('polars'), 'Requires project Python dependencies')
class AppHaltIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = list((ROOT / 'issue-review').glob('*12110*22636*.csv'))
        if not files:
            raise unittest.SkipTest('Private telemetry fixture not available')
        auth = types.ModuleType('auth')
        auth.get_current_user = lambda request: {'id': -12110}
        db = types.ModuleType('db_config')
        def blocked():
            raise AssertionError('Regression checks must never connect to a database')
        db.get_db_connection = blocked
        spec = importlib.util.spec_from_file_location('rtis_app_under_test', ROOT / 'app.py')
        cls.app = importlib.util.module_from_spec(spec)
        replacements = {'auth': auth, 'db_config': db}
        originals = {name: sys.modules.get(name) for name in replacements}
        sys.modules.update(replacements)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                spec.loader.exec_module(cls.app)
        finally:
            # Restore only the stubs. Removing newly imported dependencies
            # splits Matplotlib class identities when rendering later tests.
            for name, original in originals.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original
        cls.app._load_scheduled_halt_codes = lambda *args, **kwargs: set()
        cls.app._load_signals = lambda *args, **kwargs: []
        cls.df = cls.app.pl.read_csv(files[0])
        cls.criteria = {'from_station_equals': 'MMR', 'to_station_equals': 'CSMT',
                        'direction_equals': 'UP', 'train_number': '12110', 'coach_type': 'LHB'}
        with contextlib.redirect_stdout(io.StringIO()):
            cls.filtered = cls.app.apply_criteria(cls.df, cls.criteria)
        cls.web = cls.app._braking_profile(cls.filtered, cls.app.VIOLATIONS_OFFSETS)
        cls.pdf = cls.app._braking_profile_full_curve(cls.filtered, cls.app.VIOLATIONS_OFFSETS)

    def test_destination_has_confirmed_arrival(self):
        end = self.filtered['Logging Time'][-1]
        self.assertGreater(end, '2026-10-16 11:05:09')
        self.assertLessEqual(end, '2026-10-16 11:08:09')
        self.assertEqual(self.web[-1]['station'], 'CSMT')

    def test_web_pdf_and_chart_have_identical_stops(self):
        with contextlib.redirect_stdout(io.StringIO()):
            chart = self.app._build_chart_payload(self.filtered, self.criteria)
        expected = ['LS', 'NR', 'NK', 'DVL', 'IGP', 'X102', 'X103', 'KSRA', 'KYN', 'TNA', 'TNA', 'DR', 'CSMT']
        self.assertEqual([h['station'] for h in self.web], expected)
        self.assertEqual([m['station'] for m in chart['halt_markers']], expected)
        self.assertEqual([(h['index'], h['speeds']) for h in self.web],
                         [(h['index'], h['speeds']) for h in self.pdf])
        labels = [v for v in chart['station_labels'] if v]
        self.assertIn('TGR2', labels)
        self.assertIn('TGR3', labels)
        self.assertIn('CSMT', labels)

    def test_psr_endpoint_uses_nearest_sample_not_first_sample_in_tolerance(self):
        target_lat, target_lon = 19.7, 73.5
        # The first point is about 55 m away; the next is the actual closest pass.
        found = self.app._find_point_index(
            [target_lat + 0.0005, target_lat],
            [target_lon, target_lon],
            target_lat,
            target_lon,
        )
        self.assertEqual(found, 1)

    def test_chart_keeps_each_raw_speed_and_psr_sample_at_boundary(self):
        app = self.app
        previous = {
            'restrictions': app.SPEED_RESTRICTIONS,
            'halts': app._detect_halt_markers_from_enriched,
            'signals': app._annotate_signal_matches,
            'mps': app._section_limits_for_rows,
        }
        app.SPEED_RESTRICTIONS = [{
            'route': 'IGP-CSMT', 'direction': 'UP', 'speed_limit': 30,
            'from_lat': 19.7, 'from_lon': 73.5,
            'to_lat': 19.7, 'to_lon': 73.51,
        }]
        app._detect_halt_markers_from_enriched = lambda *args, **kwargs: []
        app._annotate_signal_matches = lambda halts, *args, **kwargs: halts
        app._section_limits_for_rows = lambda rows, *args, **kwargs: [100.0] * rows.height
        try:
            frame = app.pl.DataFrame({
                'Logging Time': ['2026-01-01 00:00:00', '2026-01-01 00:00:59',
                                 '2026-01-01 00:01:00', '2026-01-01 00:01:05'],
                'Speed': [25.0, 29.0, 29.0, 50.0],
                'Latitude': [19.7, 19.7, 19.7, 19.7],
                'Longitude': [73.5, 73.505, 73.51, 73.511],
            })
            with contextlib.redirect_stdout(io.StringIO()):
                payload = app._build_chart_payload(frame, {
                    'from_station_equals': 'IGP', 'to_station_equals': 'CSMT',
                    'direction_equals': 'UP', 'train_number': '12138',
                })
            self.assertEqual(payload['values'], [25.0, 29.0, 29.0, 50.0])
            self.assertEqual(payload['limit_values'], [30.0, 30.0, 30.0, None])
            self.assertEqual(len(payload['point_timestamps']), 4)
        finally:
            app.SPEED_RESTRICTIONS = previous['restrictions']
            app._detect_halt_markers_from_enriched = previous['halts']
            app._annotate_signal_matches = previous['signals']
            app._section_limits_for_rows = previous['mps']

    def test_existing_readings_are_preserved(self):
        expected = {'LS': [50.3, 25.3, 16.8, 12.4, 9.8, 6.1],
                    'DVL': [56.8, 29.2, 22.9, 17.1, 13.8, 8.0]}
        for station, speeds in expected.items():
            halt = next(h for h in self.web if h['station'] == station)
            self.assertEqual([round(halt['speeds'][str(o)]['speed'], 1) for o in self.app.BRAKE_OFFSETS], speeds)

    def test_short_approach_remains_visible_with_missing_readings(self):
        pl = self.app.pl
        short = self.df.filter((pl.col('Logging Time') >= '2026-10-16 06:26:00') &
                               (pl.col('Logging Time') <= '2026-10-16 06:28:10'))
        halts = self.app._braking_profile_full_curve(short, self.app.BRAKE_OFFSETS)
        self.assertEqual(len(halts), 1)
        self.assertIsNone(halts[0]['speeds']['1000']['speed'])
        self.assertEqual(halts[0]['speeds']['1000']['unavailable'], 'insufficient_approach')

    def test_ghat_classification_keeps_raw_codes(self):
        indices = self.app._compute_ghat_section_halts(self.filtered, self.pdf, 'UP')
        ghat = {self.pdf[i]['station'] for i in indices}
        self.assertTrue({'X102', 'X103'}.issubset(ghat))
        self.assertEqual(next(h['station_label'] for h in self.web if h['station'] == 'X102'), 'TGR2')

    def second_csv(self):
        fixture = ROOT / 'issue-review' / '17618.csv'
        if not fixture.exists():
            self.skipTest('Second private telemetry fixture not available')
        df = self.app._repair_shifted_rows(self.app.pl.read_csv(fixture))
        criteria = {**self.criteria, 'train_number': '17618'}
        with contextlib.redirect_stdout(io.StringIO()):
            filtered = self.app.apply_criteria(df, criteria)
        return df, filtered, criteria

    def test_17618_stop_events_agree_across_surfaces(self):
        _, filtered, criteria = self.second_csv()
        web = self.app._braking_profile(filtered, self.app.VIOLATIONS_OFFSETS)
        pdf = self.app._braking_profile_full_curve(filtered, self.app.VIOLATIONS_OFFSETS)
        with contextlib.redirect_stdout(io.StringIO()):
            chart = self.app._build_chart_payload(filtered, criteria)
        expected = ['LS', 'NR', 'ODHA', 'NK', 'NK', 'DVL', 'AV', 'GO',
                    'IGP', 'IGP', 'IGP', 'X102', 'X103', 'KSRA', 'SHAD',
                    'SHAD', 'KYN', 'TNA', 'DR', 'SNRD']
        self.assertEqual([h['station'] for h in web], expected)
        self.assertEqual([m['station'] for m in chart['halt_markers']], expected)
        self.assertEqual([(h['index'], h['speeds']) for h in web],
                         [(h['index'], h['speeds']) for h in pdf])
        self.assertEqual([h['station_label'] for h in web if h['station'].startswith('X')],
                         ['TGR2', 'TGR3'])

    def test_17618_missing_next_day_arrival_is_not_invented(self):
        df, filtered, criteria = self.second_csv()
        self.assertEqual(filtered['Logging Time'][0], '2026-10-16 17:07:24')
        self.assertEqual(filtered['Logging Time'][-1], '2026-10-16 23:59:59')
        self.assertEqual(filtered['last/cur stationCode'][-1], 'WB')
        self.assertGreater(float(filtered['Speed'][-1]), 20)
        with patch.object(self.app, '_get_user_run', return_value=({'id': -17618}, df)), contextlib.redirect_stdout(io.StringIO()):
            response = self.app.validate_route(None, criteria, run_id='fixture-17618')
        body = json.loads(response.body)
        self.assertFalse(body['valid'])
        self.assertTrue(any('Destination station CSMT not found' in e for e in body['errors']))

    def third_csv(self):
        fixture = ROOT / 'issue-review' / '12102.csv'
        if not fixture.exists():
            self.skipTest('Third private telemetry fixture not available')
        df = self.app._repair_shifted_rows(self.app.pl.read_csv(fixture))
        criteria = {**self.criteria, 'train_number': '12102',
                    'from_station_equals': 'IGP', 'to_station_equals': 'LTT'}
        with contextlib.redirect_stdout(io.StringIO()):
            filtered = self.app.apply_criteria(df, criteria)
        return df, filtered, criteria

    def test_12102_checkpoints_are_not_delayed_by_retransmissions(self):
        _, filtered, criteria = self.third_csv()
        web = self.app._braking_profile(filtered, self.app.VIOLATIONS_OFFSETS)
        pdf = self.app._braking_profile_full_curve(filtered, self.app.VIOLATIONS_OFFSETS)
        with contextlib.redirect_stdout(io.StringIO()):
            chart = self.app._build_chart_payload(filtered, criteria)
        expected = ['IGP', 'X102', 'X103', 'KSRA', 'KE', 'KDV', 'KYN', 'GC', 'VVH', 'LTT']
        self.assertEqual([h['station'] for h in web], expected)
        self.assertEqual([m['station'] for m in chart['halt_markers']], expected)
        self.assertEqual([(h['index'], h['speeds']) for h in web],
                         [(h['index'], h['speeds']) for h in pdf])
        times = {h['station']: h['logging_time'] for h in web}
        self.assertTrue(times['IGP'].endswith('09:25:30'))
        self.assertTrue(times['KYN'].endswith('12:18:43'))
        self.assertTrue(times['LTT'].endswith('13:16:18'))

    def test_12102_arrival_is_retained_and_later_yard_data_is_excluded(self):
        df, filtered, criteria = self.third_csv()
        self.assertEqual(filtered['Logging Time'][0], '2026-10-17 09:19:59')
        self.assertEqual(filtered['Logging Time'][-1], '2026-10-17 13:16:38')
        self.assertEqual(filtered['last/cur stationCode'][-1], 'LTT')
        self.assertEqual(float(filtered['Speed'][-1]), 0)
        with patch.object(self.app, '_get_user_run', return_value=({'id': -12102}, df)), contextlib.redirect_stdout(io.StringIO()):
            response = self.app.validate_route(None, criteria, run_id='fixture-12102')
        body = json.loads(response.body) if hasattr(response, 'body') else response
        self.assertTrue(body['valid'], body)

    def test_crew_origin_markers_do_not_add_braking_halts(self):
        cases = [('12138.csv', '12138', 'IGP', 'CSMT', 8),
                 ('12102.csv', '12102', 'IGP', 'LTT', 10),
                 ('17618.csv', '17618', 'MMR', 'CSMT', 20)]
        for filename, train, origin, destination, count in cases:
            with self.subTest(train=train):
                path = ROOT / 'issue-review' / filename
                if not path.exists():
                    continue
                df = self.app._repair_shifted_rows(self.app.pl.read_csv(path))
                criteria = {**self.criteria, 'train_number': train,
                            'from_station_equals': origin, 'to_station_equals': destination}
                with contextlib.redirect_stdout(io.StringIO()):
                    filtered = self.app.apply_criteria(df, criteria)
                    chart = self.app._build_chart_payload(filtered, criteria)
                self.assertEqual(chart['reference_labels'][0], origin + ' (start)')
                self.assertEqual(chart['route_markers'][0]['event_type'], 'origin')
                self.assertEqual(len(chart['halt_markers']), count)
                self.assertEqual(len(self.app._braking_profile(filtered, self.app.VIOLATIONS_OFFSETS)), count)
                if train == '12138':
                    refs = [m for m in chart['route_markers'] if m['station'] == 'X101']
                    self.assertEqual(len(refs), 1)
                    self.assertEqual(refs[0]['label'], 'TGR1 (pass)')
                    self.assertTrue(refs[0]['timestamp'].endswith('06:29:11'))
                    self.assertNotIn('X101', [h['station'] for h in chart['halt_markers']])
                if train == '12102':
                    self.assertIn('IGP', chart['station_labels'])
        with contextlib.redirect_stdout(io.StringIO()):
            chart = self.app._build_chart_payload(self.filtered, self.criteria)
        self.assertEqual(chart['reference_labels'][0], 'MMR (start)')
        self.assertEqual(len(chart['halt_markers']), 13)

    def test_route_references_do_not_duplicate_a_confirmed_ghat_stop(self):
        from datetime import datetime, timedelta
        base = datetime(2026, 10, 17, 6)
        times = [base + timedelta(seconds=i) for i in range(5)]
        frame = self.app.pl.DataFrame({'stationCode': ['IGP', 'X101', 'X101', 'X102', 'X102'],
                                      '_TS': times})
        refs = self.app._route_reference_markers(frame, ['stationCode'],
                    {'from_station_equals': 'IGP'},
                    [{'station': 'X102', 'timestamp': times[4]}])
        self.assertEqual([(m['station'], m['event_type']) for m in refs],
                         [('IGP', 'origin'), ('X101', 'passing')])
        self.assertEqual(refs[1]['source'], 'CSV_CODE_ENTRY')
        refs = self.app._route_reference_markers(frame, ['stationCode'],
                    {'from_station_equals': 'MMR'}, [])
        self.assertFalse(any(m['event_type'] == 'origin' for m in refs))

    def test_12138_chart_begins_at_recorded_standstill_before_departure(self):
        fixture = ROOT / 'issue-review' / '12138.csv'
        if not fixture.exists():
            self.skipTest('Private 12138 fixture not available')
        df = self.app._repair_shifted_rows(self.app.pl.read_csv(fixture))
        criteria = {**self.criteria, 'train_number': '12138',
                    'from_station_equals': 'IGP', 'to_station_equals': 'CSMT'}
        with contextlib.redirect_stdout(io.StringIO()):
            filtered = self.app.apply_criteria(df, criteria)
            chart = self.app._build_chart_payload(filtered, criteria)
        self.assertEqual(filtered['Logging Time'][0], '2026-10-17 06:24:13')
        self.assertEqual(float(filtered['Speed'][0]), 0)
        self.assertGreater(float(filtered['Speed'][2]), 1)
        self.assertEqual(chart['values'][0], 0)
        self.assertTrue(chart['start_sample']['timestamp'].endswith('06:24:13'))
        self.assertEqual(len(chart['halt_markers']), 8)

    def test_missing_start_speed_is_not_plotted_as_zero(self):
        frame = self.app.pl.DataFrame({
            'Logging Time': ['2026-10-17 06:00:00', '2026-10-17 06:00:01', '2026-10-17 06:01:00'],
            'Speed': [None, 12.0, 20.0], 'distFromPrevLatLng': [0.0, 3.0, 200.0],
            'last/cur stationCode': ['IGP', 'IGP', 'IGP'],
            'Latitude': [19.6946] * 3, 'Longitude': [73.5604] * 3})
        with contextlib.redirect_stdout(io.StringIO()):
            chart = self.app._build_chart_payload(frame, {'from_station_equals': 'IGP'})
        self.assertIsNone(chart['values'][0])
        self.assertIsNone(chart['start_sample']['speed'])
        self.assertGreater(chart['values'][1], 0)
        self.assertIsNotNone(self.app._render_speed_chart_image(chart))


if __name__ == '__main__':
    unittest.main()
