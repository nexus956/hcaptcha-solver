# language: Python, file: api_server.py
# REST API server — exposes /solve, /status, /health endpoints.
# Run: python api_server.py
# Then POST to http://localhost:8000/solve with {"url": "https://..."}

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ai_solver import HCaptchaAISolver, SolveResult, GeminiVisionClient
from config import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("api_server")

app = FastAPI(
    title="AI hCaptcha Solver API",
    description="Solves hCaptcha using Google Gemini Vision with automatic multi-key failover",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── in-memory job store ───────────────────────────────────────────────────
_jobs: dict[str, dict] = {}


# ─── request/response models ───────────────────────────────────────────────
class SolveRequest(BaseModel):
    url: str = config.DEMO_URL
    retries: int = 5
    headless: bool = True
    verify_token: bool = False
    gemini_key: Optional[str] = None


class SolveResponse(BaseModel):
    job_id: str
    status: str
    url: str
    created_at: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str          # pending | running | done | failed
    url: str
    created_at: str
    finished_at: Optional[str] = None
    elapsed_s: Optional[float] = None
    success: Optional[bool] = None
    token: Optional[str] = None
    error: Optional[str] = None
    attempts: Optional[list] = None
    verify_result: Optional[dict] = None


# ─── background solve worker ──────────────────────────────────────────────
async def _run_solve(job_id: str, req: SolveRequest):
    _jobs[job_id]["status"] = "running"
    start = time.perf_counter()

    try:
        solver = HCaptchaAISolver(
            max_retries=req.retries,
            headless=req.headless,
            gemini_api_key=req.gemini_key,
        )
        result: SolveResult = await solver.solve(req.url)

        verify_result = None
        if req.verify_token and result.token:
            from captcha_service import verify_token
            vr = verify_token(result.token)
            verify_result = {"success": vr.success, "raw": vr.raw, "error": vr.error}

        _jobs[job_id].update({
            "status": "done" if result.success else "failed",
            "finished_at": datetime.utcnow().isoformat(),
            "elapsed_s": round(time.perf_counter() - start, 3),
            "success": result.success,
            "token": result.token,
            "error": result.error,
            "attempts": result.to_dict()["attempts"],
            "verify_result": verify_result,
        })

    except Exception as exc:
        logger.exception("Job %s crashed: %s", job_id, exc)
        _jobs[job_id].update({
            "status": "failed",
            "finished_at": datetime.utcnow().isoformat(),
            "elapsed_s": round(time.perf_counter() - start, 3),
            "success": False,
            "error": str(exc),
        })


# ─── routes ───────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """Check if the server and Gemini API keys are configured and valid."""
    keys = config.get_gemini_keys()
    client = GeminiVisionClient(keys)
    gemini_ok = await client.is_available()
    await client.close()
    return {
        "status": "ok",
        "gemini_available": gemini_ok,
        "keys_configured": len(keys),
        "model": "gemini-3.5-flash-lite",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.post("/solve", response_model=SolveResponse)
async def solve(req: SolveRequest, background_tasks: BackgroundTasks):
    """
    Submit an hCaptcha solve job.
    Returns a job_id to poll with GET /jobs/{job_id}.
    """
    job_id = str(uuid.uuid4())[:8]
    created_at = datetime.utcnow().isoformat()

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "url": req.url,
        "created_at": created_at,
    }

    background_tasks.add_task(_run_solve, job_id, req)

    logger.info("Queued job %s for URL %s", job_id, req.url)
    return SolveResponse(job_id=job_id, status="pending", url=req.url, created_at=created_at)


@app.post("/solve/sync")
async def solve_sync(req: SolveRequest):
    """
    Synchronously solve hCaptcha and wait for result.
    """
    solver = HCaptchaAISolver(
        max_retries=req.retries,
        headless=req.headless,
        gemini_api_key=req.gemini_key,
    )
    result = await solver.solve(req.url)

    verify_result = None
    if req.verify_token and result.token:
        from captcha_service import verify_token
        vr = verify_token(result.token)
        verify_result = {"success": vr.success, "raw": vr.raw, "error": vr.error}

    return {
        **result.to_dict(),
        "verify_result": verify_result,
    }


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    """Poll the status of a solve job."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return JobStatusResponse(**_jobs[job_id])


@app.get("/jobs")
async def list_jobs():
    """List all jobs (last 50)."""
    jobs = list(_jobs.values())[-50:]
    return {"total": len(_jobs), "jobs": jobs}


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Remove a job from the store."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    del _jobs[job_id]
    return {"deleted": job_id}


@app.get("/", response_class=HTMLResponse)
async def root():
    """Redirect to API docs."""
    return HTMLResponse('<meta http-equiv="refresh" content="0; url=/docs">')


# ─── entry point ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=False)
