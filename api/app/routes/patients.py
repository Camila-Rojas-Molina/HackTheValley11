"""Patients (C1): the care-team list with its deterministic flag, one patient, their scans and the T11-L5 ladder.

Read from the database (app.views). Only the demo cohort is listed; the synthetic cohort is never a patient.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import views
from ..deps import ready_db
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


async def _patient(conn, patient_id: str) -> dict:
    p = await views.patient(conn, patient_id)
    if p is None:
        raise HTTPException(404, "unknown patient_id")
    return p


@router.get("/patients", **route_kwargs("list_patients"))
async def list_patients(db=Depends(ready_db)):
    async with db.connection() as conn:
        return await views.patient_list(conn)


@router.get("/patients/{patient_id}", **route_kwargs("get_patient"))
async def get_patient(patient_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        return await views.patient_detail(conn, await _patient(conn, patient_id))


@router.get("/patients/{patient_id}/scans", **route_kwargs("list_patient_scans"))
async def list_patient_scans(patient_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        await _patient(conn, patient_id)
        return {"patient_id": patient_id,
                "scans": [views.scan_ref(s) for s in await views.patient_scans(conn, patient_id)]}


@router.get("/patients/{patient_id}/levels", **route_kwargs("get_patient_levels"))
async def get_patient_levels(patient_id: str, db=Depends(ready_db)):
    async with db.connection() as conn:
        await _patient(conn, patient_id)
        return await views.ladder(conn, patient_id)
