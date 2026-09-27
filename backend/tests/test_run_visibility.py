from datetime import datetime, timezone


def test_quarantined_provider_rows_do_not_inflate_runs_stats_or_shoes(client):
    shoe = client.post('/api/shoes', json={'name': 'Daily trainer', 'brand': 'Test'}).json()
    common = {
        'title': 'Run', 'started_at': datetime.now(timezone.utc).isoformat(),
        'distance_km': 5, 'duration_seconds': 1800, 'run_type': 'easy',
        'shoe_id': shoe['id'],
    }
    active = client.post('/api/runs', json={**common, 'source': 'google_health', 'source_id': 'watch'})
    excluded = client.post('/api/runs', json={**common, 'source': 'google_health_excluded', 'source_id': 'phone'})
    assert active.status_code == excluded.status_code == 201
    runs = client.get('/api/runs').json()
    assert [run['id'] for run in runs] == [active.json()['id']]
    stats = client.get('/api/stats?period=week').json()
    assert stats['run_count'] == 1
    assert stats['total_distance_km'] == 5
    shoes = client.get('/api/shoes').json()
    assert shoes[0]['total_distance_km'] == 5
    # Quarantine is reversible: the original row and stream are not deleted.
    assert client.get(f"/api/runs/{excluded.json()['id']}").status_code == 200
    exported = client.get('/api/export').json()
    assert len(exported['runs']) == 2
