from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import time

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import current_user, hash_password, login_user, logout_user, verify_password
from app.config import BASE_DIR, get_settings
from app.database import SessionLocal, get_db, init_db
from app.jobs import create_job, run_job_sync
from app.models import AgentRun, Digest, DigestJob, PreferenceProfile, User, UserFeedback
from app.seed import seed_sources
from app.tools.x_accounts import normalize_x_handles

settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    db = SessionLocal()
    try:
        seed_sources(db)
    finally:
        db.close()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.app_secret_key,
    same_site="lax",
    https_only=settings.app_env == "production",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return str(token)


def check_csrf(request: Request, submitted: str) -> None:
    expected = request.session.get("csrf_token", "")
    if not expected or not secrets.compare_digest(str(expected), submitted):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def context(request: Request, db: Session, **extra: object) -> dict[str, object]:
    return {
        "request": request,
        "app_name": settings.app_name,
        "user": current_user(request, db),
        "csrf_token": csrf_token(request),
        **extra,
    }


def redirect_login() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


def get_active_job(db: Session, user_id: int) -> DigestJob | None:
    return db.scalar(
        select(DigestJob)
        .where(
            DigestJob.user_id == user_id,
            DigestJob.status.in_(["PENDING", "RUNNING"]),
        )
        .order_by(DigestJob.created_at.desc(), DigestJob.id.desc())
        .limit(1)
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "app": settings.app_name}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    active_job = get_active_job(db, user.id)
    digests = db.scalars(
        select(Digest)
        .where(Digest.user_id == user.id)
        .order_by(Digest.digest_date.desc(), Digest.version.desc())
        .limit(30)
    ).all()
    jobs = db.scalars(
        select(DigestJob)
        .where(DigestJob.user_id == user.id)
        .order_by(DigestJob.created_at.desc())
        .limit(10)
    ).all()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context(request, db, digests=digests, jobs=jobs, active_job=active_job),
    )


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> Response:
    return templates.TemplateResponse(request, "register.html", context(request, db))


@app.post("/register")
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf)
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        return templates.TemplateResponse(
            request,
            "register.html",
            context(request, db, error="请输入有效邮箱，密码至少 8 位。"),
            status_code=400,
        )
    user = User(
        email=email,
        password_hash=hash_password(password),
        timezone=settings.default_timezone,
    )
    user.preference = PreferenceProfile(
        max_items=settings.default_max_items,
        coverage_hours=settings.default_coverage_hours,
        schedule_time=time(settings.default_digest_hour, settings.default_digest_minute),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse(
            request,
            "register.html",
            context(request, db, error="该邮箱已经注册。"),
            status_code=400,
        )
    db.refresh(user)
    login_user(request, user)
    return RedirectResponse("/preferences", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> Response:
    return templates.TemplateResponse(request, "login.html", context(request, db))


@app.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf)
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            context(request, db, error="邮箱或密码错误。"),
            status_code=400,
        )
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request, csrf: str = Form(...)) -> Response:
    check_csrf(request, csrf)
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/preferences", response_class=HTMLResponse)
def preferences_page(request: Request, db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    return templates.TemplateResponse(
        request,
        "preferences.html",
        context(request, db, preference=user.preference),
    )


def split_values(raw: str) -> list[str]:
    values = (value.strip() for value in raw.replace("，", ",").split(","))
    return list(dict.fromkeys(value for value in values if value))


@app.post("/preferences")
def update_preferences(
    request: Request,
    topics: str = Form(""),
    keywords: str = Form(""),
    excluded_keywords: str = Form(""),
    x_account_1: str = Form(""),
    x_account_2: str = Form(""),
    x_account_3: str = Form(""),
    max_items: int = Form(10),
    coverage_hours: int = Form(24),
    schedule_time: str = Form("08:00"),
    weekend_enabled: str | None = Form(None),
    enabled: str | None = Form(None),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf)
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    try:
        hour, minute = (int(part) for part in schedule_time.split(":"))
        schedule = time(hour, minute)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="时间格式必须为 HH:MM") from None
    preference = user.preference or PreferenceProfile(user_id=user.id)
    preference.topics = split_values(topics)
    preference.keywords = split_values(keywords)
    preference.excluded_keywords = split_values(excluded_keywords)
    try:
        preference.x_accounts = normalize_x_handles([x_account_1, x_account_2, x_account_3])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    preference.max_items = min(max(max_items, 3), 30)
    preference.coverage_hours = min(max(coverage_hours, 6), 168)
    preference.schedule_time = schedule
    preference.weekend_enabled = weekend_enabled == "on"
    preference.enabled = enabled == "on"
    preference.version += 1
    db.add(preference)
    db.commit()
    return RedirectResponse("/preferences?saved=1", status_code=303)


@app.post("/run-now")
def run_now(
    request: Request,
    background: BackgroundTasks,
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf)
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    running = get_active_job(db, user.id)
    job = running or create_job(user.id)
    if running is None:
        background.add_task(run_job_sync, job.id)
    return RedirectResponse(f"/jobs/{job.id}", status_code=303)


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    job = db.scalar(select(DigestJob).where(DigestJob.id == job_id, DigestJob.user_id == user.id))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    run = db.scalar(
        select(AgentRun)
        .where(AgentRun.job_id == job.id)
        .order_by(AgentRun.started_at.desc())
    )
    digest = db.scalar(select(Digest).where(Digest.run_id == run.id)) if run else None
    return templates.TemplateResponse(
        request,
        "job.html",
        context(request, db, job=job, run=run, digest=digest),
    )


@app.get("/digests/{digest_id}", response_class=HTMLResponse)
def digest_detail(digest_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    digest = db.scalar(select(Digest).where(Digest.id == digest_id, Digest.user_id == user.id))
    if digest is None:
        raise HTTPException(status_code=404, detail="简报不存在")
    grouped: dict[str, list[object]] = {}
    for item in digest.items:
        grouped.setdefault(item.category, []).append(item)
    return templates.TemplateResponse(
        request,
        "digest.html",
        context(request, db, digest=digest, grouped=grouped),
    )


@app.post("/feedback")
def feedback(
    request: Request,
    article_id: int = Form(...),
    digest_id: int = Form(...),
    feedback_type: str = Form(...),
    csrf: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf)
    user = current_user(request, db)
    if user is None:
        return redirect_login()
    digest = db.scalar(select(Digest).where(Digest.id == digest_id, Digest.user_id == user.id))
    if digest is None or feedback_type not in {"bookmark", "like", "not_interested"}:
        raise HTTPException(status_code=400, detail="反馈无效")
    existing = db.scalar(
        select(UserFeedback).where(
            UserFeedback.user_id == user.id,
            UserFeedback.article_id == article_id,
            UserFeedback.feedback_type == feedback_type,
        )
    )
    if existing is None:
        db.add(
            UserFeedback(
                user_id=user.id,
                article_id=article_id,
                digest_id=digest_id,
                feedback_type=feedback_type,
            )
        )
        db.commit()
    return RedirectResponse(f"/digests/{digest_id}", status_code=303)
