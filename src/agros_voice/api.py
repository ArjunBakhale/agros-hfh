from __future__ import annotations

import json
from datetime import date, datetime, time
from importlib import resources
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import auth, services
from .audit import verify_chain
from .config import get_settings
from .db import with_tenant
from .script import available_scripts, load_script
from .services import Actor, DomainError

app = FastAPI(title="Agros voice outreach", docs_url="/api/docs", openapi_url="/api/openapi.json")
templates = Jinja2Templates(directory=str(resources.files("agros_voice") / "templates"))
MAX_CSV_BYTES = 1_000_000


@app.exception_handler(DomainError)
async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse({"error": exc.message}, status_code=exc.status)


@app.middleware("http")
async def _security_headers(request: Request, call_next: Any) -> Response:
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    return resp


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime | date | time):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v


def _actor_from_request(request: Request) -> Actor | None:
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return auth.decode_token(authz[7:].strip())
    token = request.cookies.get(auth.SESSION_COOKIE)
    if not token:
        return None
    actor = auth.decode_token(token)
    if actor and request.method not in ("GET", "HEAD", "OPTIONS"):
        sent = request.headers.get(auth.CSRF_HEADER, "")
        cookie = request.cookies.get(auth.CSRF_COOKIE, "")
        if not cookie or sent != cookie:
            raise HTTPException(403, "CSRF token missing or invalid")
    return actor


def require(roles: set[str]) -> Any:
    def dep(request: Request) -> Actor:
        actor = _actor_from_request(request)
        if actor is None:
            raise HTTPException(401, "authentication required")
        if actor.role not in roles:
            raise HTTPException(403, "insufficient role")
        return actor

    return Depends(dep)


Reader = Annotated[Actor, require(auth.READ_ROLES)]
Caller = Annotated[Actor, require(auth.CALL_ROLES)]
Writer = Annotated[Actor, require(auth.WRITE_ROLES)]


# ------------------------------------------------------------------ public


@app.get("/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "ok": True,
        "provider": s.voice_agent_provider,
        "voice_agent_enabled": s.voice_agent_enabled,
        "live_calls_approved": s.voice_live_calls_approved,
        "recording": s.voice_call_recording_enabled,
    }


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/voice-enrollment", status_code=303)


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request, error: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login", include_in_schema=False)
def login(email: Annotated[str, Form()], password: Annotated[str, Form()]) -> Response:
    actor = auth.authenticate(email, password)
    if not actor:
        return RedirectResponse("/login?error=Invalid+email+or+password", status_code=303)
    resp = RedirectResponse("/voice-enrollment", status_code=303)
    _set_session(resp, actor)
    return resp


class TokenRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/token")
def api_token(body: TokenRequest) -> dict[str, str]:
    actor = auth.authenticate(body.email, body.password)
    if not actor:
        raise HTTPException(401, "invalid credentials")
    return {"access_token": auth.issue_token(actor), "token_type": "bearer"}


def _set_session(resp: Response, actor: Actor) -> None:
    secure = get_settings().cookie_secure
    resp.set_cookie(
        auth.SESSION_COOKIE,
        auth.issue_token(actor),
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=auth.SESSION_TTL,
    )
    resp.set_cookie(
        auth.CSRF_COOKIE,
        auth.new_csrf(),
        httponly=False,
        secure=secure,
        samesite="strict",
        max_age=auth.SESSION_TTL,
    )


@app.post("/logout", include_in_schema=False)
def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE)
    resp.delete_cookie(auth.CSRF_COOKIE)
    return resp


@app.post("/api/voice-enrollment/webhooks/{provider}")
async def webhook(
    provider: str,
    request: Request,
    x_agros_signature: Annotated[str | None, Header()] = None,
    x_agros_timestamp: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    body = await request.body()
    if len(body) > 64_000:
        raise HTTPException(413, "payload too large")
    return services.ingest_webhook(provider, body, x_agros_signature, x_agros_timestamp)


# ------------------------------------------------------------------ dashboard


@app.get("/voice-enrollment", response_class=HTMLResponse, include_in_schema=False)
def dashboard_page(request: Request) -> Response:
    actor = _actor_from_request(request)
    if actor is None:
        return RedirectResponse("/login", status_code=303)
    data = services.dashboard(actor)
    from .providers.fixture import PERSONAS

    s = get_settings()
    scripts = {sid: load_script(sid).raw for sid in available_scripts()}
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "d": data,
            "actor": actor,
            "settings": s,
            "personas": list(PERSONAS),
            "scripts": scripts,
            "can_call": actor.role in auth.CALL_ROLES,
            "can_write": actor.role in auth.WRITE_ROLES,
        },
    )


# ------------------------------------------------------------------ JSON API


@app.get("/api/voice-enrollment/dashboard")
def api_dashboard(actor: Reader) -> Any:
    return _jsonable(services.dashboard(actor))


class CampaignIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    script_id: str = "hfh_dialysis_education_demo"
    max_attempts: int = Field(default=1, ge=1, le=10)


@app.post("/api/voice-enrollment/campaigns", status_code=201)
def api_create_campaign(body: CampaignIn, actor: Writer) -> Any:
    try:
        return _jsonable(
            services.create_campaign(actor, body.name, body.script_id, body.max_attempts)
        )
    except FileNotFoundError as exc:
        raise HTTPException(422, "unknown script") from exc


class StatusIn(BaseModel):
    status: str


@app.post("/api/voice-enrollment/campaigns/{campaign_id}/status")
def api_campaign_status(campaign_id: str, body: StatusIn, actor: Writer) -> Any:
    return _jsonable(services.set_campaign_status(actor, campaign_id, body.status))


@app.post("/api/voice-enrollment/campaigns/{campaign_id}/contacts")
async def api_import(campaign_id: str, file: UploadFile, actor: Writer) -> Any:
    raw = await file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        raise HTTPException(413, "CSV too large")
    rows, errors = services.parse_contacts_csv(raw.decode("utf-8-sig", errors="replace"))
    if not rows and errors:
        raise HTTPException(422, {"errors": errors})
    result = services.import_contacts(actor, campaign_id, rows)
    return {**result, "errors": errors}


class CallIn(BaseModel):
    persona: str | None = None


@app.post("/api/voice-enrollment/contacts/{contact_id}/calls")
def api_request_call(
    contact_id: str,
    actor: Caller,
    body: CallIn | None = None,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    attempt, created = services.request_call(
        actor, contact_id, idempotency_key or "", body.persona if body else None
    )
    return JSONResponse(_jsonable(attempt), status_code=201 if created else 200)


@app.get("/api/voice-enrollment/attempts/{attempt_id}")
def api_attempt(attempt_id: str, actor: Reader) -> Any:
    return _jsonable(services.get_attempt(actor, attempt_id))


class DispositionIn(BaseModel):
    status: str
    reason: str


@app.post("/api/voice-enrollment/contacts/{contact_id}/disposition")
def api_disposition(contact_id: str, body: DispositionIn, actor: Writer) -> Any:
    return _jsonable(services.override_disposition(actor, contact_id, body.status, body.reason))


@app.post("/api/voice-enrollment/handoffs/{handoff_id}")
def api_handoff(handoff_id: str, body: StatusIn, actor: Caller) -> Any:
    return _jsonable(services.resolve_handoff(actor, handoff_id, body.status))


@app.get("/api/voice-enrollment/export.csv")
def api_export(actor: Writer, campaign_id: str | None = None) -> PlainTextResponse:
    return PlainTextResponse(
        services.export_outcomes_csv(actor, campaign_id),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="voice-outcomes.csv"'},
    )


@app.get("/api/voice-enrollment/audit/verify")
def api_audit_verify(actor: Writer) -> dict[str, Any]:
    with with_tenant(actor.tenant_id) as conn:
        ok, count, broken = verify_chain(conn, actor.tenant_id)
    return {"ok": ok, "events": count, "first_broken": broken}


@app.get("/api/voice-enrollment/scripts/{script_id}")
def api_script(script_id: str, actor: Reader) -> Any:
    try:
        return load_script(script_id).raw
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "script not found") from exc


_ = json
