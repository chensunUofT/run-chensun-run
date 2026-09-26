"""Bounded, offline ingestion of owner-downloaded Strava export archives.

No files are extracted to disk and no Strava API calls are made. Duplicate CSV
headers are intentional in Strava exports: the second Distance is meters,
whereas the first is the display distance in the account's preferred units.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import gzip
import io
import math
from pathlib import PurePosixPath
import zipfile
import xml.etree.ElementTree as ET

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_MEMBER_BYTES = 24 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ROWS = 10000
MAX_SAMPLES = 50000
MAX_DETAIL_BYTES = 128 * 1024 * 1024
MAX_TOTAL_SAMPLES = 200000


def _number(value):
    try:
        result = float(str(value).replace(',', '').strip())
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _date(value: str) -> datetime:
    value = value.strip()
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        parsed = None
        for pattern in ('%b %d, %Y, %I:%M:%S %p', '%b %d, %Y, %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
            try:
                parsed = datetime.strptime(value, pattern)
                break
            except ValueError:
                pass
        if parsed is None:
            raise ValueError('Unsupported Activity Date format')
    # Strava's bulk-export Activity Date is UTC, unlike its localized title.
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _safe_path(name: str) -> str:
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or any(':' in part for part in path.parts):
        raise ValueError('Archive contains an unsafe path')
    return str(path)


def _xml(raw: bytes):
    # XML may use UTF-16/32, whose interleaved NULs must not bypass this guard.
    declarations = raw.replace(b'\x00', b'').upper()
    if b'<!DOCTYPE' in declarations or b'<!ENTITY' in declarations:
        raise ValueError('XML entities are not supported')
    return ET.fromstring(raw)


def _gpx(raw: bytes):
    root = _xml(raw)
    points = []
    distance = 0.0
    previous = None
    for segment in root.findall('.//{*}trkseg'):
        previous = None
        for node in segment.findall('{*}trkpt'):
            lat, lon = _number(node.get('lat')), _number(node.get('lon'))
            stamp = node.findtext('{*}time')
            if not stamp or lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                continue
            if previous:
                a, b = math.radians(previous[0]), math.radians(lat)
                dlat, dlon = b-a, math.radians(lon-previous[1])
                hav = math.sin(dlat/2)**2 + math.cos(a)*math.cos(b)*math.sin(dlon/2)**2
                distance += 6371000 * 2 * math.asin(min(1, math.sqrt(hav)))
            point = {'timestamp': _date(stamp).isoformat(), 'latitude': lat, 'longitude': lon, 'distance_m': distance}
            if previous is None and points:
                point['unknown_before'] = True
            for child in node.iter():
                name = child.tag.split('}')[-1]
                key = {'ele': 'altitude_m', 'hr': 'heart_rate', 'cad': 'cadence'}.get(name)
                if key and (value := _number(child.text)) is not None:
                    point[key] = value
            points.append(point)
            previous = (lat, lon)
            if len(points) > MAX_SAMPLES:
                raise ValueError('Activity exceeds sample limit')
    return points, []


def _fit(raw: bytes):
    import fitdecode
    points, laps = [], []
    with fitdecode.FitReader(io.BytesIO(raw), check_crc=fitdecode.CrcCheck.RAISE) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            values = {field.name: field.value for field in frame.fields}
            stamp = values.get('timestamp')
            if frame.name == 'record' and isinstance(stamp, datetime):
                point = {'timestamp': stamp.replace(tzinfo=timezone.utc).isoformat() if stamp.tzinfo is None else stamp.isoformat()}
                for source, key in [('distance', 'distance_m'), ('enhanced_altitude', 'altitude_m'), ('altitude', 'altitude_m'), ('heart_rate', 'heart_rate'), ('cadence', 'cadence'), ('enhanced_speed', 'speed_mps'), ('speed', 'speed_mps')]:
                    number = _number(values.get(source))
                    if number is not None and key not in point:
                        point[key] = number
                for source, key in [('position_lat', 'latitude'), ('position_long', 'longitude')]:
                    number = _number(values.get(source))
                    if number is not None:
                        point[key] = number * 180 / 2**31
                points.append(point)
                if len(points) > MAX_SAMPLES:
                    raise ValueError('Activity exceeds sample limit')
            elif frame.name == 'lap':
                start = values.get('start_time')
                elapsed = _number(values.get('total_elapsed_time'))
                if isinstance(start, datetime) and elapsed and elapsed > 0:
                    laps.append({'_start': start, 'elapsed_seconds': elapsed, 'distance_m': _number(values.get('total_distance')), 'label': f'Lap {len(laps)+1}'})
                    if len(laps) > 2000:
                        raise ValueError('Activity exceeds lap limit')
    if points:
        origin = _date(points[0]['timestamp'])
        for lap in laps:
            stamp = lap.pop('_start')
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            lap['start_seconds'] = max(0, (stamp-origin).total_seconds())
            lap['end_seconds'] = lap['start_seconds'] + lap['elapsed_seconds']
    else:
        laps = []
    return points, laps


def _activity_file(name: str, raw: bytes, remaining_bytes=None):
    limit = min(MAX_MEMBER_BYTES, remaining_bytes[0]) if remaining_bytes is not None else MAX_MEMBER_BYTES
    if name.lower().endswith('.gz'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as source:
            raw = source.read(limit + 1)
        if len(raw) > limit:
            if remaining_bytes is not None:
                remaining_bytes[0] = max(0, remaining_bytes[0] - len(raw))
            raise ValueError('Decompressed activity exceeds file limit')
        name = name[:-3]
    if remaining_bytes is not None:
        remaining_bytes[0] = max(0, remaining_bytes[0] - len(raw))
    if len(raw) > limit:
        raise ValueError('Activity details exceed total expanded limit')
    if name.lower().endswith('.fit'):
        return _fit(raw)
    if name.lower().endswith('.gpx'):
        return _gpx(raw)
    if name.lower().endswith('.tcx'):
        root = _xml(raw)
        if sum(1 for node in root.iter() if node.tag.split('}')[-1] == 'Trackpoint') > MAX_SAMPLES:
            raise ValueError('Activity exceeds sample limit')
        if sum(1 for node in root.iter() if node.tag.split('}')[-1] == 'Lap') > 2000:
            raise ValueError('Activity exceeds lap limit')
        from .google_health import _parse_tcx_document
        document = _parse_tcx_document(raw)
        points = []
        for source in document['points']:
            if not source.get('timestamp'):
                continue
            point = {'timestamp': _date(source['timestamp']).isoformat()}
            for key, source_key in [('latitude', 'lat'), ('longitude', 'lon'), ('distance_m', 'distance_m'), ('altitude_m', 'altitude_m'), ('heart_rate', 'heart_rate')]:
                value = _number(source.get(source_key))
                if value is not None:
                    point[key] = value
            points.append(point)
        laps = []
        if points:
            origin = _date(points[0]['timestamp'])
            for source in document.get('laps', []):
                if not source.get('start_time'):
                    continue
                start = (_date(source['start_time']) - origin).total_seconds()
                elapsed = _number(source.get('elapsed_seconds'))
                if elapsed is None and source.get('end_time'):
                    elapsed = (_date(source['end_time']) - _date(source['start_time'])).total_seconds()
                if elapsed is None or elapsed <= 0:
                    continue
                laps.append({'start_seconds': max(0, start), 'end_seconds': max(0, start) + elapsed,
                             'elapsed_seconds': elapsed, 'distance_m': _number(source.get('distance_m')),
                             'label': f'Lap {len(laps)+1}'})
        return points, laps
    raise ValueError('Unsupported original activity format')


def parse_strava_archive(contents: bytes) -> dict:
    """Return activities, skipped and warnings; raise ValueError on bad input."""
    if len(contents) > MAX_UPLOAD_BYTES:
        raise ValueError('Upload exceeds 100 MiB limit')
    archive = None
    members = {}
    csv_name = ''
    try:
        if contents.startswith(b'PK'):
            try:
                archive = zipfile.ZipFile(io.BytesIO(contents))
                infos = archive.infolist()
                if len(infos) > 30000 or sum(info.file_size for info in infos) > MAX_ARCHIVE_BYTES:
                    raise ValueError('Archive exceeds expanded size or member limit')
                for info in infos:
                    name = _safe_path(info.filename)
                    if name in members:
                        raise ValueError('Archive contains duplicate paths')
                    members[name] = info
                csv_files = [name for name in members if PurePosixPath(name).name.lower() == 'activities.csv']
                if len(csv_files) != 1:
                    raise ValueError('ZIP must contain exactly one activities.csv')
                csv_name = csv_files[0]
                if members[csv_name].file_size > MAX_MEMBER_BYTES:
                    raise ValueError('activities.csv exceeds file limit')
                with archive.open(members[csv_name]) as handle:
                    csv_raw = handle.read(MAX_MEMBER_BYTES + 1)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                raise ValueError('Cannot read ZIP archive') from exc
        else:
            csv_raw = contents
        if len(csv_raw) > MAX_MEMBER_BYTES:
            raise ValueError('activities.csv exceeds file limit')
        try:
            reader = csv.reader(io.StringIO(csv_raw.decode('utf-8-sig'), newline=''))
            headers = [name.strip() for name in next(reader)]
        except (UnicodeError, StopIteration, csv.Error) as exc:
            raise ValueError('Expected a UTF-8 Strava activities.csv') from exc
        for required in ('Activity ID', 'Activity Date', 'Activity Type', 'Elapsed Time'):
            if required not in headers:
                raise ValueError(f'Missing Strava export column: {required}')
        activities, warnings, skipped, seen = [], [], 0, set()
        remaining_details = [MAX_DETAIL_BYTES]
        total_samples = 0
        for row_number, row in enumerate(reader, start=2):
            if row_number > MAX_ROWS + 1:
                raise ValueError('Export exceeds activity row limit')
            if not row or not any(row):
                continue
            def values(name):
                return [row[index].strip() for index, header in enumerate(headers) if header == name and index < len(row)]
            def first(name):
                return next((value for value in values(name) if value), '')
            if first('Activity Type').replace(' ', '').lower() not in {'run', 'running', 'trailrun', 'virtualrun', 'treadmillrun'}:
                skipped += 1
                continue
            try:
                if len(row) != len(headers):
                    raise ValueError('CSV row has an unexpected column count')
                identity = first('Activity ID')
                if not identity or len(identity) > 200:
                    raise ValueError('Missing or invalid Activity ID')
                if identity in seen:
                    skipped += 1
                    continue
                stamp = _date(first('Activity Date'))
                elapsed = _number(first('Elapsed Time'))
                moving = _number(first('Moving Time'))
                # The raw second Distance column is always SI. Never guess
                # whether the account's formatted display distance uses miles.
                distances = values('Distance')
                meters = _number(distances[1]) if len(distances) > 1 else _number(first('Distance (m)'))
                km = meters / 1000 if meters is not None else _number(first('Distance (km)'))
                if km is None:
                    raise ValueError('Raw distance in meters is missing; display-distance units are ambiguous')
                if not (0 < km <= 1000) or elapsed is None or not (0 < elapsed <= 86400):
                    raise ValueError('Invalid distance or elapsed time')
                if moving is not None and not (0 <= moving <= elapsed):
                    raise ValueError('Moving time exceeds elapsed time')
                average_hr = _number(first('Average Heart Rate'))
                record = {'source_id': identity, 'title': first('Activity Name')[:120] or 'Run', 'started_at': stamp,
                          'distance_km': km, 'duration_seconds': round(elapsed), 'moving_seconds': round(moving) if moving is not None else None,
                          'avg_hr': round(average_hr) if average_hr is not None and 20 <= average_hr <= 260 else None,
                          'source_utc_offset_seconds': None, 'samples': [], 'laps': [], 'run_type': 'run',
                          'metadata': {'activity_id': identity, 'activity_type': first('Activity Type'), 'date_basis': 'export_utc', 'moving_time_source': 'strava_export' if moving is not None else 'unavailable'}}
                filename = first('Filename')
                if archive and filename:
                    try:
                        if remaining_details[0] <= 0 or total_samples >= MAX_TOTAL_SAMPLES:
                            raise ValueError('Archive exceeds total detail limit; summary retained')
                        filename = _safe_path(filename)
                        base = str(PurePosixPath(csv_name).parent / filename)
                        member = members.get(filename) or members.get(base)
                        if member is None:
                            raise ValueError('Original activity file is absent')
                        if member.file_size > MAX_MEMBER_BYTES:
                            raise ValueError('Original activity exceeds file limit')
                        with archive.open(member) as handle:
                            raw = handle.read(MAX_MEMBER_BYTES + 1)
                        if len(raw) > MAX_MEMBER_BYTES:
                            raise ValueError('Original activity exceeds file limit')
                        points, laps = _activity_file(filename, raw, remaining_details)
                        total_samples += len(points)
                        if total_samples > MAX_TOTAL_SAMPLES:
                            raise ValueError('Archive exceeds total sample limit; summary retained')
                        record['samples'], record['laps'] = points, laps
                        record['metadata']['original_filename'] = filename
                        if points and points[0].get('timestamp'):
                            original_start = _date(points[0]['timestamp'])
                            if abs((original_start-stamp).total_seconds()) > 300:
                                warnings.append(f'Activity {identity}: original file time differs from CSV; retained the CSV time.')
                    except Exception as exc:
                        # Keep a valid summary when an optional original is
                        # corrupt, unsupported, encrypted or incomplete.
                        warnings.append(f'Activity {identity}: detail unavailable ({type(exc).__name__}: {str(exc)[:140]}).')
                elif not filename or not archive:
                    record['metadata']['details_missing'] = True
                seen.add(identity)
                activities.append(record)
            except (ValueError, OverflowError) as exc:
                skipped += 1
                warnings.append(f'Row {row_number}: {exc}')
        if any(not item['samples'] for item in activities):
            warnings.append('Some activities have summaries only; detailed curves and workout intervals are unavailable for those records.')
        return {'activities': activities, 'skipped': skipped, 'warnings': warnings[:500]}
    except csv.Error as exc:
        raise ValueError('Malformed Strava CSV') from exc
    finally:
        if archive:
            archive.close()
