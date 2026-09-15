from __future__ import annotations

from datetime import datetime, timezone

from app.db import Run, Shoe


OTHER_OWNER_ID = "00000000-0000-0000-0000-000000000002"


def _run_payload(distance_km: float, *, run_type: str = "easy") -> dict[str, object]:
    return {
        "title": "Bulk test run",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "distance_km": distance_km,
        "duration_seconds": int(distance_km * 300),
        "run_type": run_type,
        "notes": "",
        "source": "manual",
    }


def _create_run(client, distance_km: float, *, run_type: str = "easy") -> dict[str, object]:
    response = client.post("/api/runs", json=_run_payload(distance_km, run_type=run_type))
    assert response.status_code == 201, response.text
    return response.json()


def test_bulk_update_changes_type_and_shoe_and_updates_mileage(client) -> None:
    shoe_response = client.post(
        "/api/shoes",
        json={"name": "Bulk trainer", "brand": "Runwise", "initial_distance_km": 10},
    )
    assert shoe_response.status_code == 201, shoe_response.text
    shoe_id = shoe_response.json()["id"]
    first = _create_run(client, 4)
    second = _create_run(client, 6)

    response = client.post(
        "/api/runs/bulk-update",
        json={"run_ids": [first["id"], second["id"]], "run_type": "  Tempo ", "shoe_id": shoe_id},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"updated_count": 2}
    for run_id in (first["id"], second["id"]):
        updated = client.get(f"/api/runs/{run_id}")
        assert updated.status_code == 200, updated.text
        assert updated.json()["run_type"] == "tempo"
        assert updated.json()["shoe_id"] == shoe_id
        assert updated.json()["shoe_assignment"] == "manual"
        assert updated.json()["shoe_confidence"] is None
        assert updated.json()["shoe_reason"] == "Manually selected"

    listed_shoe = next(item for item in client.get("/api/shoes").json() if item["id"] == shoe_id)
    assert listed_shoe["total_distance_km"] == 20


def test_bulk_update_is_atomic_when_one_run_is_owned_by_another_user(client) -> None:
    owned = _create_run(client, 5, run_type="easy")
    with client.app.state.session_factory() as db:
        foreign = Run(
            owner_id=OTHER_OWNER_ID,
            title="Foreign run",
            started_at=datetime.now(timezone.utc),
            distance_km=5,
            duration_seconds=1500,
            run_type="easy",
            notes="",
            source="manual",
        )
        db.add(foreign)
        db.commit()
        db.refresh(foreign)
        foreign_id = foreign.id

    response = client.post(
        "/api/runs/bulk-update",
        json={"run_ids": [owned["id"], foreign_id], "run_type": "race"},
    )

    assert response.status_code == 404, response.text
    unchanged = client.get(f"/api/runs/{owned['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["run_type"] == "easy"


def test_bulk_update_rejects_foreign_shoe_before_updating_any_run(client) -> None:
    run = _create_run(client, 5, run_type="easy")
    with client.app.state.session_factory() as db:
        foreign_shoe = Shoe(owner_id=OTHER_OWNER_ID, name="Foreign shoe", brand="Other", rules={})
        db.add(foreign_shoe)
        db.commit()
        db.refresh(foreign_shoe)
        foreign_shoe_id = foreign_shoe.id

    response = client.post(
        "/api/runs/bulk-update",
        json={"run_ids": [run["id"]], "run_type": "race", "shoe_id": foreign_shoe_id},
    )

    assert response.status_code == 422, response.text
    unchanged = client.get(f"/api/runs/{run['id']}")
    assert unchanged.status_code == 200
    assert unchanged.json()["run_type"] == "easy"
    assert unchanged.json()["shoe_id"] is None


def test_bulk_update_null_shoe_sets_manual_unassigned_metadata(client) -> None:
    shoe_response = client.post("/api/shoes", json={"name": "Temporary shoe"})
    assert shoe_response.status_code == 201, shoe_response.text
    shoe_id = shoe_response.json()["id"]
    run = _create_run(client, 5)
    assigned = client.patch(f"/api/runs/{run['id']}", json={"shoe_id": shoe_id})
    assert assigned.status_code == 200, assigned.text

    response = client.post("/api/runs/bulk-update", json={"run_ids": [run["id"]], "shoe_id": None})

    assert response.status_code == 200, response.text
    assert response.json() == {"updated_count": 1}
    updated = client.get(f"/api/runs/{run['id']}")
    assert updated.status_code == 200
    assert updated.json()["shoe_id"] is None
    assert updated.json()["shoe_assignment"] == "manual"
    assert updated.json()["shoe_confidence"] is None
    assert updated.json()["shoe_reason"] == "Manually marked without a shoe"


def test_bulk_update_validates_patch_shape_and_run_type(client) -> None:
    run = _create_run(client, 5)
    cases = [
        {"run_ids": [run["id"]]},
        {"run_ids": [run["id"]], "run_type": None},
        {"run_ids": [run["id"]], "run_type": "   "},
        {"run_ids": [run["id"]], "run_type": "x" * 41},
        {"run_ids": [run["id"]], "run_type": "easy", "unexpected": True},
        {"run_ids": [run["id"], run["id"]], "run_type": "easy"},
        {"run_ids": [0], "run_type": "easy"},
    ]

    for payload in cases:
        response = client.post("/api/runs/bulk-update", json=payload)
        assert response.status_code == 422, (payload, response.text)

