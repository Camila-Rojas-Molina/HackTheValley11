"""Patients (C1): the care-team list with its flag, one patient, their scans and the T11-L5 ladder.

M1: golden fixtures (app.contract_fixtures). Push 2 reads the database.
"""
from __future__ import annotations

from fastapi import APIRouter

from ..contract_fixtures import serve
from ..schemas import API_PREFIX, route_kwargs

router = APIRouter(prefix=API_PREFIX)


@router.get("/patients", **route_kwargs("list_patients"))
async def list_patients():
    return serve("list_patients", "/patients", "patients")


@router.get("/patients/{patient_id}", **route_kwargs("get_patient"))
async def get_patient(patient_id: str):
    return serve("get_patient", f"/patients/{patient_id}", "patient_id")


@router.get("/patients/{patient_id}/scans", **route_kwargs("list_patient_scans"))
async def list_patient_scans(patient_id: str):
    return serve("list_patient_scans", f"/patients/{patient_id}/scans", "patient_id")


@router.get("/patients/{patient_id}/levels", **route_kwargs("get_patient_levels"))
async def get_patient_levels(patient_id: str):
    return serve("get_patient_levels", f"/patients/{patient_id}/levels", "patient_id")
