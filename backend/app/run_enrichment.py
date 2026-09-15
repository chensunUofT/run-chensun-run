"""Repeat-safe movement, training type and shoe enrichment for private runs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import gzip
import json
import math
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .activity_analysis import analyze_activity
from .db import Run, RunStream, Shoe
from .shoe_inference import suggest_for_run
from .training_classification import classify_training
from .fitness import compute_fitness


def _ascent(samples: list[dict], metrics: dict) -> tuple[float | None, str]:
    value = metrics.get("elevationGainMillimeters")
    try:
        meters = float(value) / 1000
        if math.isfinite(meters) and meters >= 0:
            return meters, "provider"
    except (TypeError, ValueError):
        pass
    values = [p.get("altitude_m", p.get("elevation_m")) for p in samples]
    values = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(v)]
    if len(values) < 3:
        return None, "missing"
    smooth = [median(values[max(0, i-2):i+3]) for i in range(len(values))]
    anchor, gain = smooth[0], 0.0
    for value in smooth[1:]:
        # Small altitude jitter does not count as repeated climbing.
        change = value - anchor
        if abs(change) >= 3:
            gain += max(0, change)
            anchor = value
    return round(gain, 1), "smoothed_altitude_estimate"


def enrich_run(db: Session, run: Run, *, provider_active_seconds: float | None = None) -> dict:
    """Enrich without committing; the importer owns the transaction.

    Only observed stops are removed. Missing telemetry remains in the elapsed
    duration, so a short route excerpt cannot create an implausibly fast run.
    Provider active duration is a conservative fallback cap when available.
    """
    db.flush()
    stream = db.scalar(select(RunStream).where(RunStream.owner_id == run.owner_id, RunStream.run_id == run.id))
    samples, laps, analysis = [], [], {}
    previous = dict(stream.analysis or {}) if stream is not None else {}
    telemetry_quality = previous.get("telemetry_quality") or {}
    max_gap = min(65.0, max(1.0, float(telemetry_quality.get("analysis_max_gap_seconds") or 30)))
    if provider_active_seconds is None:
        provider_active_seconds = previous.get("provider_active_seconds")
    if stream is not None:
        stored = json.loads(gzip.decompress(stream.payload_gzip))
        samples = [dict(p, lat=p.get("latitude", p.get("lat")), lon=p.get("longitude", p.get("lon"))) for p in stored.get("samples", [])]
        laps = stored.get("laps", [])
        analysis = analyze_activity(samples, laps or None, max_gap_seconds=max_gap)
        analysis = {**previous, **analysis}
        observed_moving = analysis.get("moving_seconds")
        if len(samples) >= 2 and analysis.get("moving_time_available"):
            stopped = max(0.0, float(analysis.get("stopped_seconds") or 0))
            estimate = max(0.0, float(run.duration_seconds) - stopped)
            if isinstance(provider_active_seconds, (int, float)) and math.isfinite(provider_active_seconds) and 0 <= provider_active_seconds <= run.duration_seconds:
                estimate = min(estimate, provider_active_seconds)
            run.moving_seconds = min(run.duration_seconds, max(0, round(estimate)))
            coverage = min(1.0, float(analysis.get("elapsed_seconds") or 0) / max(1, run.duration_seconds))
            analysis.update(
                observed_moving_seconds=observed_moving,
                moving_seconds=run.moving_seconds,
                elapsed_seconds=run.duration_seconds,
                stopped_seconds=run.duration_seconds - run.moving_seconds,
                moving_pace_seconds=run.moving_seconds / (run.distance_km * 1000) if run.distance_km > 0 else None,
                moving_pace_seconds_per_km=run.moving_seconds / run.distance_km if run.distance_km > 0 else None,
                stream_coverage_ratio=round(coverage, 4),
                provider_active_seconds=provider_active_seconds,
                unknown_time_policy="retain_as_moving",
            )
            if coverage < 0.95 or float(analysis.get("unclassified_seconds") or 0) > 0:
                analysis["moving_time_source"] = "observed_stops_with_conservative_gap_fallback"
                analysis["quality_flags"] = list(dict.fromkeys([*analysis.get("quality_flags", []), "unknown_time_retained"]))
                confidence = min(0.59, float(analysis.get("moving_time_confidence") or 0) * coverage)
                analysis["moving_time_confidence"] = round(confidence, 3)
                analysis["moving_time_confidence_label"] = "low"
                analysis["moving_time_reason"] = "Only observed stops were removed; unknown time was retained because telemetry is incomplete."
            if telemetry_quality.get("coarse"):
                analysis["moving_time_confidence"] = min(0.55, float(analysis.get("moving_time_confidence") or 0))
                analysis["moving_time_confidence_label"] = "low"
                analysis["moving_time_resolution_seconds"] = previous.get("telemetry_interval_seconds")
                analysis["moving_time_source"] = "interval_distance_estimate"
                analysis["quality_flags"] = list(dict.fromkeys([*analysis.get("quality_flags", []), "coarse_distance_intervals"]))
        else:
            run.moving_seconds = round(provider_active_seconds) if isinstance(provider_active_seconds, (int, float)) and math.isfinite(provider_active_seconds) and 0 < provider_active_seconds <= run.duration_seconds else None
    label = str(run.run_type or "run").lower()
    manual = run.run_type_assignment == "manual"
    # Legacy specific labels have no origin column; protect them on migration.
    preserve = manual or (run.run_type_assignment != "inferred" and label not in {"run", "running", "easy"})
    decision = classify_training(samples, laps, analysis, existing_label=label, manual=manual, preserve_existing=preserve, max_gap_seconds=max_gap)
    if not manual and not decision.get("preserved"):
        run.run_type = decision["run_type"]
        run.run_type_assignment = "inferred"
    if stream is not None:
        analysis["training_classification"] = decision
        gain, gain_source = _ascent(samples, analysis.get("provider_metrics_summary") or {})
        analysis["total_ascent_m"] = gain
        analysis["ascent_source"] = gain_source
        analysis["fitness"] = compute_fitness(run, analysis.get("weather"), analysis)
        stream.analysis = analysis

    shoe_changed = False
    if run.shoe_assignment != "manual":
        shoes = list(db.scalars(select(Shoe).where(Shoe.owner_id == run.owner_id)).all())
        rows = db.execute(select(Run.shoe_id, func.sum(Run.distance_km)).where(
            Run.owner_id == run.owner_id, Run.id != run.id,
            Run.started_at < run.started_at, Run.shoe_id.is_not(None),
            ~func.lower(Run.source).contains("demo"), ~func.lower(Run.source).contains("sample"),
        ).group_by(Run.shoe_id)).all()
        mileage = {int(shoe_id): float(total) for shoe_id, total in rows}
        started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        offset = run.source_utc_offset_seconds or 0
        activity_date = started.astimezone(timezone(timedelta(seconds=offset))).date()
        candidates = suggest_for_run(run, shoes, mileage_by_shoe=mileage, run_date=activity_date, today=datetime.now(timezone.utc).date())
        if candidates:
            best = candidates[0]
            shoe_changed = run.shoe_id != best.shoe_id
            run.shoe_id, run.shoe_assignment = best.shoe_id, "inferred"
            run.shoe_confidence, run.shoe_reason = best.confidence, best.reason
    return {"run_id": run.id, "run_type": run.run_type, "type_changed": label != run.run_type, "shoe_changed": shoe_changed, "moving_seconds": run.moving_seconds, "has_stream": stream is not None}
