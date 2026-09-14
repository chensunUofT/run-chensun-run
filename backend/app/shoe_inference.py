"""Deterministic shoe assignment suggestions.

This module is intentionally rules based. A prediction is a transparent
candidate match with a confidence score derived from matching metadata; it is
never presented as a machine-learning result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True, slots=True)
class ShoeCandidate:
    shoe_id: int
    shoe_name: str
    score: float
    confidence: float
    reason: str


def _rules(shoe: Any) -> dict[str, Any]:
    value = getattr(shoe, "rules", None)
    return value if isinstance(value, dict) else {}


def _eligible(shoe: Any, run_date: date, today: date) -> tuple[bool, str]:
    purchase_date = getattr(shoe, "purchase_date", None)
    if purchase_date is not None and purchase_date > run_date:
        return False, "Shoe was purchased after this run"
    status = str(getattr(shoe, "status", "active") or "active").lower()
    # Retired shoes remain useful for historical runs, but a newly inferred
    # assignment never chooses one for today's or future runs.
    if status == "retired" and run_date >= today:
        return False, "Retired shoes are only eligible for historical runs"
    if status not in {"active", "retired"}:
        return False, "Shoe is not active or historically retired"
    return True, ""


def _candidate_for_run(shoe: Any, run: Any, run_date: date, today: date, mileage: float) -> ShoeCandidate | None:
    eligible, why = _eligible(shoe, run_date, today)
    if not eligible:
        return None
    rules = _rules(shoe)
    distance = float(getattr(run, "distance_km", 0.0))
    pace = float(getattr(run, "duration_seconds", 0.0)) / distance if distance > 0 else 0.0
    min_distance = rules.get("min_distance_km")
    max_distance = rules.get("max_distance_km")
    min_pace = rules.get("min_pace_seconds")
    max_pace = rules.get("max_pace_seconds")
    if min_distance is not None and distance < float(min_distance):
        return None
    if max_distance is not None and distance > float(max_distance):
        return None
    if min_pace is not None and pace < float(min_pace):
        return None
    if max_pace is not None and pace > float(max_pace):
        return None
    run_types = {str(item).lower() for item in (rules.get("run_types") or [])}
    run_type = str(getattr(run, "run_type", "run") or "run").lower()
    if run_types and run_type not in run_types:
        return None

    matched = 0
    reasons: list[str] = []
    if min_distance is not None or max_distance is not None:
        matched += 1
        reasons.append("distance rule matched")
    if min_pace is not None or max_pace is not None:
        matched += 1
        reasons.append("pace rule matched")
    if run_types:
        matched += 1
        reasons.append(f"run type {run_type} matched")
    if getattr(shoe, "purchase_date", None) is not None:
        matched += 1
        reasons.append("purchase date is eligible")
    priority = int(rules.get("priority") or 0)
    # Priority dominates, while lower existing mileage is a deterministic
    # tie breaker that naturally spreads runs across a rotation.
    score = float(priority) * 1000.0 - float(mileage)
    if getattr(shoe, "purchase_date", None) is not None:
        score += 0.001
    confidence = min(0.99, 0.55 + 0.08 * matched + 0.01 * max(-10, min(priority, 10)))
    if not reasons:
        reasons.append("eligible active shoe; no restrictive rules configured")
    return ShoeCandidate(
        shoe_id=int(shoe.id),
        shoe_name=str(getattr(shoe, "name", "")),
        score=round(score, 3),
        confidence=round(confidence, 3),
        reason="; ".join(reasons),
    )


def suggest_for_run(
    run: Any,
    shoes: list[Any],
    *,
    mileage_by_shoe: dict[int, float] | None = None,
    run_date: date,
    today: date,
) -> list[ShoeCandidate]:
    mileage = mileage_by_shoe or {}
    candidates = [
        candidate
        for shoe in shoes
        if (candidate := _candidate_for_run(shoe, run, run_date, today, float(mileage.get(int(shoe.id), 0.0)))) is not None
    ]
    return sorted(candidates, key=lambda item: (-item.score, item.shoe_id))
