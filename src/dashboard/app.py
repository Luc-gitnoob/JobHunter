"""FastAPI dashboard for JobHunter — view, filter, and manage job matches."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, desc, case, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.db import AsyncSessionLocal, init_db
from src.database.models import Job, PollLog

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent
STATIC_DIR = DASHBOARD_DIR / "static"
TEMPLATE_DIR = DASHBOARD_DIR / "templates"
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

app = FastAPI(title="JobHunter Dashboard", version="1.0.0")

# Mount static files
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Jinja2 templates
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@app.on_event("startup")
async def startup():
    await init_db()


# ===================== HELPERS =====================

def _parse_analysis(analysis_str: str) -> dict:
    """Safely parse match_analysis JSON string."""
    if not analysis_str:
        return {}
    try:
        return json.loads(analysis_str)
    except (json.JSONDecodeError, TypeError):
        return {}


# ===================== ROUTES =====================

@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    status: Optional[str] = None,
    min_score: Optional[int] = None,
    company: Optional[str] = None,
    page: int = Query(default=1, ge=1),
):
    """Main dashboard — shows all jobs with filters."""
    per_page = 25
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        # Base query
        query = select(Job).where(Job.is_filtered_out == False)

        # Apply filters
        if status:
            query = query.where(Job.status == status)
        if min_score is not None:
            query = query.where(Job.match_score >= min_score)
        if company:
            query = query.where(Job.company_name.ilike(f"%{company}%"))

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await session.execute(count_query)
        total = total_result.scalar() or 0

        # Fetch page
        query = query.order_by(
            desc(Job.match_score),
            desc(Job.discovered_at),
        ).offset(offset).limit(per_page)
        result = await session.execute(query)
        jobs = result.scalars().all()

        # Stats
        stats = await _get_stats(session)

        # Get unique companies for filter dropdown
        companies_result = await session.execute(
            select(Job.company_name).distinct().order_by(Job.company_name)
        )
        companies = [r[0] for r in companies_result.all()]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "jobs": jobs,
            "stats": stats,
            "companies": companies,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
            "current_status": status,
            "current_min_score": min_score,
            "current_company": company,
            "parse_analysis": _parse_analysis,
        },
    )


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    """Detailed view of a single job."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job:
            return HTMLResponse("Job not found", status_code=404)

        analysis = _parse_analysis(job.match_analysis)

    return templates.TemplateResponse(
        request=request,
        name="job_detail.html",
        context={
            "job": job,
            "analysis": analysis,
        },
    )


@app.post("/job/{job_id}/status")
async def update_job_status(job_id: int, status: str = Form(...)):
    """Update a job's application status."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status=status,
                status_updated_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()

    return RedirectResponse(url=f"/job/{job_id}", status_code=303)


@app.post("/job/{job_id}/notes")
async def update_job_notes(job_id: int, notes: str = Form(...)):
    """Update a job's notes."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(notes=notes)
        )
        await session.commit()

    return RedirectResponse(url=f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}/resume")
async def download_resume(job_id: int):
    """Download the tailored resume (PDF or TEX) for a job."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job or not job.resume_path:
            return HTMLResponse("Resume not found", status_code=404)

        resume_path = Path(job.resume_path)
        if not resume_path.exists():
            return HTMLResponse("Resume file missing", status_code=404)

    is_pdf = resume_path.suffix.lower() == ".pdf"
    media_type = "application/pdf" if is_pdf else "text/plain; charset=utf-8"

    return FileResponse(
        path=str(resume_path),
        filename=resume_path.name,
        media_type=media_type,
    )


@app.get("/job/{job_id}/tex", response_class=PlainTextResponse)
async def view_resume_tex(job_id: int):
    """View the raw LaTeX source code for this job's resume."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if not job or not job.resume_path:
            return PlainTextResponse("Resume not found", status_code=404)

        path = Path(job.resume_path)
        if path.suffix.lower() == ".tex" and path.exists():
            return PlainTextResponse(path.read_text(encoding="utf-8"))

        tex_alt = path.with_suffix(".tex")
        if tex_alt.exists():
            return PlainTextResponse(tex_alt.read_text(encoding="utf-8"))

    return PlainTextResponse("LaTeX source file missing", status_code=404)


@app.get("/logs", response_class=HTMLResponse)
async def poll_logs(request: Request, page: int = Query(default=1, ge=1)):
    """View polling history and errors."""
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        count_result = await session.execute(select(func.count(PollLog.id)))
        total = count_result.scalar() or 0

        result = await session.execute(
            select(PollLog)
            .order_by(desc(PollLog.started_at))
            .offset(offset)
            .limit(per_page)
        )
        logs = result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={
            "logs": logs,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": max(1, (total + per_page - 1) // per_page),
        },
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    """Analytics and statistics page."""
    async with AsyncSessionLocal() as session:
        stats = await _get_stats(session)

        # Jobs by company (top 20)
        company_stats = await session.execute(
            select(
                Job.company_name,
                func.count(Job.id).label("total"),
                func.avg(Job.match_score).label("avg_score"),
                func.count(case((Job.match_score >= 70, 1))).label("high_matches"),
            )
            .where(Job.is_filtered_out == False)
            .group_by(Job.company_name)
            .order_by(desc("high_matches"))
            .limit(20)
        )
        companies = company_stats.all()

        # Recent high-score jobs
        top_jobs_result = await session.execute(
            select(Job)
            .where(Job.match_score >= 70, Job.is_filtered_out == False)
            .order_by(desc(Job.match_score), desc(Job.discovered_at))
            .limit(10)
        )
        top_jobs = top_jobs_result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="stats.html",
        context={
            "stats": stats,
            "companies": companies,
            "top_jobs": top_jobs,
        },
    )


async def _get_stats(session: AsyncSession) -> dict:
    """Calculate dashboard statistics."""
    total = await session.execute(
        select(func.count(Job.id)).where(Job.is_filtered_out == False)
    )
    scored = await session.execute(
        select(func.count(Job.id)).where(
            Job.match_score.isnot(None),
            Job.is_filtered_out == False,
        )
    )
    high_match = await session.execute(
        select(func.count(Job.id)).where(
            Job.match_score >= 70,
            Job.is_filtered_out == False,
        )
    )
    applied = await session.execute(
        select(func.count(Job.id)).where(Job.status == "applied")
    )
    filtered = await session.execute(
        select(func.count(Job.id)).where(Job.is_filtered_out == True)
    )

    return {
        "total_jobs": total.scalar() or 0,
        "scored_jobs": scored.scalar() or 0,
        "high_matches": high_match.scalar() or 0,
        "applied": applied.scalar() or 0,
        "filtered_out": filtered.scalar() or 0,
    }
