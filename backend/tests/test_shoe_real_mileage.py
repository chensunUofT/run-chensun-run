def test_shoe_mileage_excludes_hidden_sample_runs(client):
    shoe = client.post('/api/shoes', json={'name': 'Real trainer', 'initial_distance_km': 12}).json()
    for source, distance in [('manual', 8), ('demo', 5), ('Sample-import', 10)]:
        response = client.post('/api/runs', json={
            'title': 'Mileage verification', 'started_at': '2026-09-01T12:00:00Z',
            'distance_km': distance, 'duration_seconds': 2400, 'run_type': 'easy',
            'source': source, 'shoe_id': shoe['id'],
        })
        assert response.status_code == 201, response.text
    listed = next(item for item in client.get('/api/shoes').json() if item['id'] == shoe['id'])
    assert listed['total_distance_km'] == 20
    updated = client.patch(f"/api/shoes/{shoe['id']}", json={'name': 'Renamed trainer'})
    assert updated.status_code == 200
    assert updated.json()['total_distance_km'] == 20
    for source in ['manual', 'demo']:
        response = client.post('/api/runs', json={
            'title': 'Inference verification', 'started_at': '2026-09-02T12:00:00Z',
            'distance_km': 6, 'duration_seconds': 2400, 'run_type': 'easy', 'source': source,
        })
        assert response.status_code == 201
        if source == 'manual':
            real_id = response.json()['id']
    preview = client.post('/api/shoes/infer', json={'apply': False}).json()
    assert [item['run_id'] for item in preview['assignments']] == [real_id]
    assert preview['skipped_manual'] == 1
