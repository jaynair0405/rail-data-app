"""Shared, offline RTIS stop detection. Raw station codes remain unchanged."""

from collections import deque
from datetime import datetime
import math
from statistics import median


HALT_NAMES = {"X101": "TGR1", "X102": "TGR2", "X103": "TGR3"}


def halt_label(code, time=None):
    code = str(code or "").strip()
    label = HALT_NAMES.get(code.upper(), code)
    when = timestamp(time) if time is not None else None
    return f"{label} ({when:%H:%M})" if when is not None else label


def timestamp(value):
    if isinstance(value, datetime):
        return value
    value = str(value or "").strip()
    if len(value) > 10 and value[4:5] == "-":
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%y %H:%M:%S",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%y %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def separation(a, b):
    """Distance in metres between two validated (latitude, longitude) points."""
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2
    h += math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(min(1.0, math.sqrt(h)))


def departure_anchor(speeds, times, departure, stations=None, origin=None,
                     latitudes=None, longitudes=None):
    """Last observed stationary point before a selected departure, not its dwell.

    Look back at most 60 seconds within the same origin visit. Earlier rows
    establish standstill only; they are not included in the returned journey.
    Return the original departure if source evidence is missing or interrupted.
    """
    if departure is None or departure <= 0:
        return departure
    depart_time = timestamp(times[departure])
    if depart_time is None:
        return departure

    def point(i):
        if latitudes is None or longitudes is None:
            return None
        lat, lon = number(latitudes[i]), number(longitudes[i])
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon

    # Work in original row coordinates, while identical retransmissions do
    # not provide extra duration or interrupt a real stationary period.
    preceding = []
    next_time = depart_time
    next_idx = departure
    for i in range(departure - 1, -1, -1):
        time = timestamp(times[i])
        speed = number(speeds[i])
        if (time is None or speed is None or not 0 <= speed <= 3
                or (stations is not None and str(stations[i] or '').strip().upper() != origin)):
            break
        gap = (next_time - time).total_seconds()
        if not 0 <= gap <= 3 or not 0 <= (depart_time - time).total_seconds() <= 60:
            break
        if gap == 0:
            if speed == number(speeds[next_idx]) and point(i) == point(next_idx):
                continue
            break
        preceding.append((i, time, speed, point(i)))
        next_time, next_idx = time, i

    for end, candidate in enumerate(preceding):
        idx, end_time, speed, _ = candidate
        if speed > 0.5:
            continue
        window = []
        for record in preceding[end:]:
            window.append(record)
            duration = (end_time - record[1]).total_seconds()
            if duration < 10:
                continue
            values = [row[2] for row in window]
            points = [row[3] for row in window]
            has_gps = all(p is not None for p in points)
            stable_gps = has_gps and all(separation(points[0], p) <= 5 for p in points)
            strict = max(values) <= 0.3 and (stable_gps or not has_gps)
            noisy = duration >= 20 and median(values) <= 1 and stable_gps
            if strict or noisy:
                return idx
            if duration >= 20:
                break
    return departure


def detect_halts(speeds, times, latitudes=None, longitudes=None, in_station=None):
    """Return one event per stop, rearmed by 10 seconds of travel >=5 km/h.

    Preserve a strict low-speed checkpoint when available. Otherwise require
    20 seconds at <=3 km/h, median <=1 km/h, at least one <=0.5 reading, and
    all GPS positions within 5 metres of the window's first position. Missing
    data, conflicting/reversed time and gaps over 3 seconds break confirmation.
    Identical retransmissions at the same timestamp are one observation. GPS noise
    is never accumulated as distance to decide whether another stop occurred.

    ``in_station`` optionally marks rows inside a station geofence. There some
    devices report a slow crawl through the whole dwell, so the noise-tolerant
    window accepts GPS drift up to 20 metres if its median speed is below
    0.9 km/h; the other speed conditions are unchanged.
    """
    size = len(speeds)
    if len(times) != size:
        raise ValueError("Speeds and timestamps must have equal lengths")
    speeds = [number(v) for v in speeds]
    times = [timestamp(v) for v in times]
    points = [None] * size
    if latitudes is not None and longitudes is not None:
        if len(latitudes) != size or len(longitudes) != size:
            raise ValueError("GPS and speed arrays must have equal lengths")
        for i, (lat, lon) in enumerate(zip(latitudes, longitudes)):
            lat, lon = number(lat), number(lon)
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                points[i] = (lat, lon)
    if in_station is not None and len(in_station) != size:
        raise ValueError("Station geofence flags and speed arrays must have equal lengths")
    in_station = [bool(v) for v in in_station] if in_station is not None else [False] * size

    # Some devices repeat an unchanged reading every few seconds. Such a row
    # supplies no additional elapsed time, but must not reset a genuine dwell.
    # Keep original row indices for braking distances, charts and persistence.
    original_indices = []
    for i in range(size):
        if original_indices:
            previous = original_indices[-1]
            if (times[i] is not None and times[i] == times[previous]
                    and speeds[i] is not None and speeds[i] == speeds[previous]
                    and points[i] == points[previous]):
                continue
        original_indices.append(i)
    speeds = [speeds[i] for i in original_indices]
    times = [times[i] for i in original_indices]
    points = [points[i] for i in original_indices]
    in_station = [in_station[i] for i in original_indices]
    size = len(original_indices)

    def elapsed(a, b):
        if times[a] is None or times[b] is None:
            return None
        try:
            return (times[b] - times[a]).total_seconds()
        except TypeError:
            return None

    def strict_confirmation(start):
        run_start = None
        for j in range(start, size):
            duration = elapsed(start, j)
            if duration is None or duration > 15 or speeds[j] is None:
                break
            if j > start and not (0 < (elapsed(j - 1, j) or 0) <= 3):
                break
            if speeds[j] <= 0.3:
                if run_start is None:
                    run_start = j
                if elapsed(run_start, j) >= 10:
                    # When GPS is present, reject a steadily crawling train.
                    if all(points[k] is not None for k in range(run_start, j + 1)):
                        if any(separation(points[run_start], points[k]) > 5 for k in range(run_start, j + 1)):
                            return None
                    return j
            else:
                run_start = None
        return None

    events = []
    active = None
    armed = False
    moving_since = None
    window = deque()
    for i, speed in enumerate(speeds):
        continuous = i == 0 or 0 < (elapsed(i - 1, i) or 0) <= 3
        if not continuous or speed is None or times[i] is None:
            window.clear()
            moving_since = None
        if speed is None or times[i] is None:
            continue
        if speed >= 5:
            if moving_since is None:
                moving_since = i
            if elapsed(moving_since, i) >= 10:
                armed = True
                active = None
        else:
            moving_since = None

        # A strict checkpoint can replace the noise-tolerant checkpoint during
        # the same stop, preserving existing LS/DVL approach readings.
        if i and continuous and speed <= 0.5 and speeds[i - 1] is not None and speeds[i - 1] > 0.5:
            if armed or (active is not None and active["method"] != "strict"):
                confirmed = strict_confirmation(i)
                if confirmed is not None:
                    event = {"index": i, "confirmed_index": confirmed, "method": "strict"}
                    if active is not None:
                        active.update(event)
                    else:
                        events.append(event)
                        active = event
                    armed = False

        if speed > 3 or points[i] is None:
            window.clear()
            continue
        window.append(i)
        while len(window) > 1 and elapsed(window[1], i) >= 20:
            window.popleft()
        if not armed or elapsed(window[0], i) < 20:
            continue
        values = [speeds[j] for j in window]
        if min(values) > 0.5 or median(values) > 1:
            continue
        spread = max(separation(points[window[0]], points[j]) for j in window)
        if spread > 5 and not (spread <= 20 and all(in_station[j] for j in window)
                               and median(values) < 0.9):
            continue
        # Confirmation may come late in a noisy dwell. Anchor the braking
        # approach to its first low reading, rather than adding GPS jitter
        # accumulated while waiting for a sufficiently stable window.
        begin = window[0]
        while begin > 0:
            previous = begin - 1
            if (speeds[previous] is None or speeds[previous] > 3
                    or points[previous] is None
                    or not 0 < (elapsed(previous, begin) or 0) <= 3
                    or separation(points[previous], points[window[0]]) > 20):
                break
            begin = previous
        first_low = next(j for j in range(begin, i + 1) if speeds[j] <= 0.5)
        active = {"index": first_low, "confirmed_index": i, "method": "gps_stationary"}
        events.append(active)
        armed = False
    return [{**event, "index": original_indices[event["index"]],
             "confirmed_index": original_indices[event["confirmed_index"]]}
            for event in events]
