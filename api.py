"""
api.py — FastAPI service exposing the reconciliation engine and the RAG analyst.

Run:   uvicorn api:app --reload
Docs:  http://127.0.0.1:8000/docs  (interactive Swagger UI)

Endpoints
  POST /reconcile                      upload gateway + bank files -> job id + summary
  GET  /reconcile/{job_id}/report      download the colour-coded Excel report
  GET  /reconcile/{job_id}/exceptions  exception rows as JSON (optional ?status= filter)
  POST /reconcile/{job_id}/ask         ask a question in plain English (RAG over results + runbook)
  GET  /health
"""
from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import reconcile
from ai_analyst import ReconAnalyst

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
RUNS_DIR = Path("runs")
RUNBOOK = Path(__file__).parent / "knowledge" / "runbook.md"
ALLOWED = {".csv"}

app = FastAPI(title="Reconciliation API", version="1.0.0",
              description="Gateway vs bank reconciliation with an LLM/RAG exception analyst.")
JOBS: dict[str, dict] = {}   # in-memory job store (swap for Redis/DB in production)


class StatusCount(BaseModel):
    status: str
    count: int
    amount: float


class ReconResponse(BaseModel):
    job_id: str
    business_date: str
    gateway_rows: int
    match_rate: float
    exceptions: int
    summary: list[StatusCount]
    report_url: str


class AskRequest(BaseModel):
    question: str = Field(..., min_length=3, examples=["Which missing credits are above 1 lakh?"])


class AskResponse(BaseModel):
    question: str
    answer: str
    mode: str
    sources: list[dict]


def _save(upload: UploadFile, dest: Path) -> Path:
    if Path(upload.filename or "").suffix.lower() not in ALLOWED:
        raise HTTPException(400, f"{upload.filename}: only CSV files are accepted")
    with dest.open("wb") as f:
        shutil.copyfileobj(upload.file, f)
    return dest


def _job(job_id: str) -> dict:
    if job_id not in JOBS:
        raise HTTPException(404, "Unknown job id")
    return JOBS[job_id]


@app.get("/health")
def health():
    return {"status": "ok", "jobs": len(JOBS)}


@app.post("/reconcile", response_model=ReconResponse)
def run_reconciliation(gateway: UploadFile = File(...), bank: UploadFile = File(...)):
    job_id = uuid.uuid4().hex[:12]
    folder = RUNS_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    gw_path = _save(gateway, folder / "gateway.csv")
    bank_path = _save(bank, folder / "bank.csv")
    try:
        res, summary, xlsx, _ = reconcile.run(gw_path, bank_path, folder, send=False)
    except ValueError as e:                       # missing columns etc.
        raise HTTPException(422, str(e)) from e

    total = int(res["Txn ID"].notna().sum())
    matched = int(summary[summary.Status.str.startswith("Matched")].Count.sum())
    JOBS[job_id] = {"results": res, "xlsx": xlsx, "analyst": ReconAnalyst(res, RUNBOOK)}
    return ReconResponse(
        job_id=job_id,
        business_date=xlsx.stem.split("_")[-1],
        gateway_rows=total,
        match_rate=round(matched / total, 4) if total else 0.0,
        exceptions=int(len(res) - matched),
        summary=[StatusCount(status=r.Status, count=int(r.Count), amount=float(r.Amount))
                 for r in summary.itertuples()],
        report_url=f"/reconcile/{job_id}/report",
    )


@app.get("/reconcile/{job_id}/report")
def download_report(job_id: str):
    xlsx = _job(job_id)["xlsx"]
    return FileResponse(xlsx, filename=xlsx.name,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/reconcile/{job_id}/exceptions")
def list_exceptions(job_id: str, status: str | None = None, limit: int = 100):
    res = _job(job_id)["results"]
    rows = res[~res.Status.str.startswith("Matched")]
    if status:
        rows = rows[rows.Status.str.lower() == status.lower()]
    return rows.head(limit).astype(object).where(rows.head(limit).notna(), None).to_dict("records")


@app.post("/reconcile/{job_id}/ask", response_model=AskResponse)
def ask(job_id: str, body: AskRequest):
    return _job(job_id)["analyst"].ask(body.question)
