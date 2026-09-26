import csv
import gzip
import io
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import strava_archive as parser


def export(rows=None, filename=''):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(['Activity ID', 'Activity Date', 'Activity Type', 'Elapsed Time', 'Distance', 'Distance', 'Moving Time', 'Filename'])
    writer.writerows(rows or [['123', 'Sep 20, 2026, 7:00:00 AM', 'Run', '1800', '3.1', '5000', '1700', filename]])
    return stream.getvalue().encode()


def archive(csv_bytes, **members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('activities.csv', csv_bytes)
        for name, data in members.items():
            z.writestr(name, data)
    return stream.getvalue()


def test_duplicate_distance_uses_si_and_run_filter():
    rows = [[str(i), '2026-09-20T07:00:00Z', sport, '1800', '3.1', '5000', '', ''] for i, sport in enumerate(['Run', 'Walk', 'Trail Run', 'VirtualRun', 'Ride'])]
    result = parser.parse_strava_archive(export(rows))
    assert result['skipped'] == 2
    assert len(result['activities']) == 3
    assert all(r['distance_km'] == 5 and r['duration_seconds'] == 1800 for r in result['activities'])
    assert result['activities'][0]['started_at'].tzinfo == timezone.utc


def test_duplicate_activity_id_skipped():
    row = ['1', '2026-09-20T07:00:00Z', 'Run', '1800', '3', '5000', '', '']
    assert parser.parse_strava_archive(export([row, row]))['skipped'] == 1


@pytest.mark.parametrize('name', ['../activities.csv', '/activities.csv', 'C:/activities.csv'])
def test_unsafe_archive_member_rejected(name):
    with pytest.raises(ValueError, match='unsafe'):
        parser.parse_strava_archive(archive(export(), **{name: b'x'}))


def test_expansion_limit(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_ARCHIVE_BYTES', 10)
    with pytest.raises(ValueError, match='expanded'):
        parser.parse_strava_archive(archive(export()))


def test_gzip_bound_and_corrupt_details_keep_summary(monkeypatch):
    monkeypatch.setattr(parser, 'MAX_MEMBER_BYTES', 1024)
    result = parser.parse_strava_archive(archive(export(filename='a.gpx.gz'), **{'a.gpx.gz': gzip.compress(b'x' * 2000)}))
    assert len(result['activities']) == 1
    assert not result['activities'][0]['samples']
    assert 'Decompressed' in result['warnings'][0]


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-16', 'utf-32'])
def test_xml_entities_rejected_all_encodings(encoding):
    raw = '<!DOCTYPE gpx [<!ENTITY a "bad">]><gpx>&a;</gpx>'.encode(encoding)
    with pytest.raises(ValueError, match='entities'):
        parser._xml(raw)


def test_gpx_distance_and_segment_gap():
    raw = b'<gpx><trk><trkseg><trkpt lat="40" lon="-70"><time>2026-09-20T07:00:00Z</time><extensions><hr>140</hr></extensions></trkpt><trkpt lat="40.001" lon="-70"><time>2026-09-20T07:01:00Z</time></trkpt></trkseg><trkseg><trkpt lat="45" lon="-75"><time>2026-09-20T07:05:00Z</time></trkpt></trkseg></trk></gpx>'
    points, laps = parser._activity_file('a.gpx.gz', gzip.compress(raw))
    assert 110 < points[1]['distance_m'] < 112
    assert points[2]['distance_m'] == points[1]['distance_m']
    assert points[2]['unknown_before']
    assert points[0]['heart_rate'] == 140
    assert laps == []


def test_tcx_lap_normalization_and_nonfinite_values():
    raw = b'<TrainingCenterDatabase><Activities><Activity Sport="Running"><Lap StartTime="2026-09-20T07:00:00Z"><TotalTimeSeconds>60</TotalTimeSeconds><DistanceMeters>200</DistanceMeters><Track><Trackpoint><Time>2026-09-20T07:00:00Z</Time><DistanceMeters>0</DistanceMeters><AltitudeMeters>NaN</AltitudeMeters></Trackpoint><Trackpoint><Time>2026-09-20T07:01:00Z</Time><DistanceMeters>200</DistanceMeters></Trackpoint></Track></Lap></Activity></Activities></TrainingCenterDatabase>'
    points, laps = parser._activity_file('a.tcx', raw)
    assert points[1]['distance_m'] == 200
    assert 'altitude_m' not in points[0]
    assert laps == [{'start_seconds': 0, 'end_seconds': 60, 'elapsed_seconds': 60, 'distance_m': 200, 'label': 'Lap 1'}]


def test_fit_record_lap_contract(monkeypatch):
    import fitdecode
    stamp = datetime(2026, 9, 20, 7, tzinfo=timezone.utc)
    def frame(name, **values):
        return SimpleNamespace(name=name, frame_type=fitdecode.FIT_FRAME_DATA, fields=[SimpleNamespace(name=k, value=v) for k, v in values.items()])
    class Reader:
        def __init__(self, *args, **kwargs):
            assert kwargs['check_crc'] == fitdecode.CrcCheck.RAISE
        def __enter__(self):
            return iter([frame('record', timestamp=stamp, distance=0, enhanced_speed=3.0, speed=2.0, position_lat=2**30), frame('lap', start_time=stamp, total_elapsed_time=60, total_distance=180)])
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(fitdecode, 'FitReader', Reader)
    points, laps = parser._fit(b'fixture')
    assert points[0]['latitude'] == 90
    assert points[0]['speed_mps'] == 3
    assert laps[0]['start_seconds'] == 0 and laps[0]['end_seconds'] == 60
    assert laps[0]['distance_m'] == 180


def test_nested_gzip_aggregate_budget(monkeypatch):
    raw = b'<gpx>' + b' ' * 100 + b'</gpx>'
    monkeypatch.setattr(parser, 'MAX_DETAIL_BYTES', len(raw))
    rows = [[str(i), '2026-09-20T07:00:00Z', 'Run', '1800', '3.1', '5000', '', f'{i}.gpx.gz'] for i in range(2)]
    result = parser.parse_strava_archive(archive(export(rows), **{f'{i}.gpx.gz': gzip.compress(raw) for i in range(2)}))
    assert len(result['activities']) == 2
    assert any('total detail limit' in warning for warning in result['warnings'])
