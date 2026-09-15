"""Owner-scoped bulk updates for recorded runs."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import get_current_owner
from .db import Run, Shoe, get_db


class BulkRunUpdate(BaseModel):
    """The small, explicit patch accepted by the bulk run endpoint."""

    model_config = ConfigDict(extra="forbid")

    run_ids: list[Annotated[StrictInt, Field(gt=0)]] = Field(min_length=1, max_length=500)
    run_type: str | None = Field(default=None, min_length=1, max_length=40)
    shoe_id: StrictInt | None = Field(default=None, gt=0)

    @field_validator("run_ids")
    @classmethod
    def validate_run_ids(cls, values: list[StrictInt]) -> list[StrictInt]:
        if len(set(values)) != len(values):
            raise ValueError("run_ids must contain unique values")
        return values

    @field_validator("run_type")
    @classmethod
    def clean_run_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("run_type cannot be blank")
        return cleaned.lower()

    @model_validator(mode="after")
    def validate_patch(self) -> "BulkRunUpdate":
        fields = self.model_fields_set
        if not fields.intersection({"run_type", "shoe_id"}):
            raise ValueError("At least one update field is required")
        if "run_type" in fields and self.run_type is None:
            raise ValueError("run_type cannot be null")
        return self


class BulkRunUpdateResponse(BaseModel):
    updated_count: int = Field(ge=0)


def register_bulk_run_routes(app: FastAPI) -> None:
    """Register the atomic, owner-scoped bulk run update route."""

    @app.post(
        "/api/runs/bulk-update",
        response_model=BulkRunUpdateResponse,
        status_code=status.HTTP_200_OK,
    )
    def bulk_update_runs(
        payload: BulkRunUpdate,
        owner_id: str = Depends(get_current_owner),
        db: Session = Depends(get_db),
    ) -> BulkRunUpdateResponse:
        # Resolve every run in one owner-scoped query before changing any row.
        # A missing or foreign ID is intentionally indistinguishable from an
        # unknown ID, matching the single-run route's privacy boundary.
        runs = db.scalars(
            select(Run).where(Run.owner_id == owner_id, Run.id.in_(payload.run_ids))
        ).all()
        if len(runs) != len(payload.run_ids):
            raise HTTPException(status_code=404, detail="Run not found")

        # Validate the shoe through the same owner boundary before mutating a
        # run. An explicit null is the manual "without a shoe" assignment.
        if "shoe_id" in payload.model_fields_set and payload.shoe_id is not None:
            shoe_exists = db.scalar(
                select(Shoe.id).where(Shoe.id == payload.shoe_id, Shoe.owner_id == owner_id)
            )
            if shoe_exists is None:
                raise HTTPException(status_code=422, detail=f"shoe_id {payload.shoe_id} does not exist")

        if "run_type" in payload.model_fields_set:
            # Keep the existing update semantics: stream analysis is based on
            # activity data and is left intact while the run type is changed.
            run_type = payload.run_type
            if run_type is None:
                # This is guarded by ``BulkRunUpdate.validate_patch``. Keep the
                # check local too so the ORM never receives a nullable value.
                raise HTTPException(status_code=422, detail="run_type cannot be null")
            for run in runs:
                run.run_type = run_type
                run.run_type_assignment = "manual"

        if "shoe_id" in payload.model_fields_set:
            for run in runs:
                run.shoe_id = payload.shoe_id
                run.shoe_assignment = "manual"
                run.shoe_confidence = None
                run.shoe_reason = (
                    "Manually selected" if payload.shoe_id is not None else "Manually marked without a shoe"
                )

        # All changes share one transaction. Roll back explicitly if the
        # database rejects the flush/commit so a partial bulk update cannot be
        # observed by a later request.
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise

        return BulkRunUpdateResponse(updated_count=len(runs))
