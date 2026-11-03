"""The web app: the page, the JSON API and the step stream."""

from __future__ import annotations

import asyncio
import ipaddress
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import agent, books, close_job, db, jobs, ledger, limits, proposals, seed, services, workspace
from .config import settings
from .limits import LimitReached
from .money import rand
from .sandbox import SandboxError
from .vultr import VultrError

STATIC = Path(__file__).resolve().parent.parent / "static"

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
        "connect-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}

# Body caps by path, in bytes, checked as the body streams in (Caddy caps it again in front).
BODY_CAPS = {"/api/voice": 1024 * 1024 + 4096, "/api/statement": 20 * 1024 + 4096}
DEFAULT_CAP = 8 * 1024


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await run_in_threadpool(db.migrate)
    yield
    db.close_pool()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class BodyTooLarge(Exception):
    pass


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        cap = BODY_CAPS.get(path, DEFAULT_CAP)
        declared = request.headers.get("content-length")
        if declared and (not declared.isdigit() or int(declared) > cap):
            return _secure(JSONResponse({"error": "That upload is too large."}, status_code=413))
        if path.startswith("/api/"):
            # Same-site requests only: a custom header no cross-site form can send, and a
            # matching Origin when the browser gives one.
            if request.headers.get("x-requested-with") != "imali":
                return _secure(JSONResponse({"error": "That request was refused."}, status_code=403))
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
                return _secure(JSONResponse({"error": "That request was refused."}, status_code=403))
        seen = 0
        receive = request._receive

        async def capped():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > cap:
                    raise BodyTooLarge()
            return msg

        request._receive = capped
    try:
        response = await call_next(request)
    except BodyTooLarge:
        response = JSONResponse({"error": "That upload is too large."}, status_code=413)
    return _secure(response)


def _secure(response: Response) -> Response:
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    if settings().secure_cookies:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


@app.exception_handler(RequestValidationError)
async def _bad_request(_request: Request, _exc: RequestValidationError):
    return JSONResponse({"error": "That request could not be read."}, status_code=400)


@app.exception_handler(LimitReached)
async def _limited(_request: Request, exc: LimitReached):
    return JSONResponse({"error": exc.message}, status_code=429)


@app.exception_handler(Exception)
async def _crashed(_request: Request, _exc: Exception):
    return JSONResponse({"error": "Something went wrong. Please try again."}, status_code=500)


def client_addr(request: Request) -> str:
    """The visitor's address. Behind Caddy the socket peer is the proxy, so take the rightmost
    X-Forwarded-For entry, and only when the peer is a private address."""
    peer = request.client.host if request.client else "0.0.0.0"
    if settings().trust_proxy:
        try:
            private = ipaddress.ip_address(peer).is_private or ipaddress.ip_address(peer).is_loopback
        except ValueError:
            private = False
        xff = request.headers.get("x-forwarded-for", "")
        if private and xff:
            last = xff.split(",")[-1].strip()
            try:
                return str(ipaddress.ip_address(last))
            except ValueError:
                pass
    return peer


def current(request: Request) -> str:
    wid = workspace.read_token(request.cookies.get(workspace.COOKIE))
    if not wid:
        raise HTTPException(401, "Your copy has ended. Open a new one to carry on.")
    return wid


@app.exception_handler(HTTPException)
async def _http(_request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def _set_cookie(resp: Response, wid: str, expires) -> None:
    resp.set_cookie(
        workspace.COOKIE, workspace.make_token(wid, expires), max_age=settings().copy_minutes * 60,
        httponly=True, secure=settings().secure_cookies, samesite="strict", path="/",
    )


# --- pages -----------------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/healthz", include_in_schema=False)
async def healthz():
    def ping():
        with db.tx() as c:
            c.execute("SELECT 1")
    await run_in_threadpool(ping)
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- the copy --------------------------------------------------------------------------------

@app.post("/api/copy")
async def open_copy(request: Request):
    addr = client_addr(request)
    old = workspace.read_token(request.cookies.get(workspace.COOKIE))

    def make():
        with db.tx() as c:
            return workspace.open_copy(c, addr, replacing=old)
    wid, expires = await run_in_threadpool(make)
    resp = JSONResponse({"ok": True, "expires_at": expires.isoformat()})
    _set_cookie(resp, wid, expires)
    return resp


@app.delete("/api/copy")
async def leave(request: Request):
    wid = workspace.read_token(request.cookies.get(workspace.COOKIE))
    if wid:
        def drop():
            with db.tx() as c:
                c.execute("DELETE FROM workspaces WHERE id = %s", (wid,))
        await run_in_threadpool(drop)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(workspace.COOKIE, path="/")
    return resp


@app.get("/api/state")
async def state(request: Request):
    wid = current(request)

    def read():
        with db.tx() as c:
            ws = workspace.get(c, wid)
            if ws is None:
                return None
            pending = c.execute(
                "SELECT kind, count(*) AS n FROM proposals WHERE workspace_id = %s AND status = 'pending' GROUP BY kind",
                (wid,),
            ).fetchall()
            job = c.execute(
                "SELECT id, status FROM jobs WHERE workspace_id = %s AND kind = 'close_month' ORDER BY id DESC LIMIT 1",
                (wid,),
            ).fetchone()
            post = None
            if job:
                post = c.execute(
                    "SELECT id, status FROM jobs WHERE parent_id = %s ORDER BY id DESC LIMIT 1", (job["id"],)
                ).fetchone()
            return {
                "shop": ws["shop_name"], "owner": seed.OWNER_FIRST_NAME,
                "expires_at": ws["expires_at"].isoformat(),
                "period": {"start": ws["period_start"].isoformat(), "end": ws["period_end"].isoformat(),
                           "label": f"{ws['period_end']:%B %Y}"},
                "bank": ledger.balance_of(c, wid, ledger.BANK),
                "cash": ledger.balance_of(c, wid, ledger.CASH),
                "debtors": ledger.balance_of(c, wid, ledger.DEBTORS),
                "creditors": ledger.balance_of(c, wid, ledger.CREDITORS),
                "pending": {r["kind"]: r["n"] for r in pending},
                "close": dict(job) if job else None,
                "post": dict(post) if post else None,
            }
    data = await run_in_threadpool(read)
    if data is None:
        raise HTTPException(401, "Your copy has ended. Open a new one to carry on.")
    return data


# --- your agent ------------------------------------------------------------------------------

class Message(BaseModel):
    text: str = Field(min_length=1, max_length=600)


def _history(c, wid):
    return c.execute(
        "SELECT role, body FROM messages WHERE workspace_id = %s ORDER BY id DESC LIMIT 8", (wid,)
    ).fetchall()[::-1]


def _talk(wid: str, addr: str, text: str, via: str) -> dict:
    lim = settings().limits
    h = limits.addr_hash(addr)
    with db.tx() as c:
        limits.check(c, [(f"msg:ws:{wid}", lim.messages_per_copy, limits.COPY),
                         (f"msg:day:{h}", lim.messages_per_day, limits.DAY)],
                     "Your agent has answered as many messages as this copy allows. Start over for a fresh copy.")
        history = _history(c, wid)
        c.execute("INSERT INTO messages (workspace_id, role, body, via) VALUES (%s, 'owner', %s, %s)", (wid, text, via))
    a = agent.Agent(wid, services.factory(wid))
    reply = a.reply(text, history)
    with db.tx() as c:
        c.execute("INSERT INTO messages (workspace_id, role, body) VALUES (%s, 'agent', %s)", (wid, reply))
        drafted = [_proposal_view(p) for p in c.execute(
            "SELECT * FROM proposals WHERE id = ANY(%s) ORDER BY id", (a.drafted,)).fetchall()] if a.drafted else []
    return {"reply": reply, "drafted": drafted, "job_id": a.started_job}


@app.get("/api/messages")
async def messages(request: Request):
    wid = current(request)

    def read():
        with db.tx() as c:
            rows = c.execute(
                "SELECT m.id, m.role, m.body, m.via, m.at FROM messages m WHERE m.workspace_id = %s ORDER BY m.id DESC LIMIT 40",
                (wid,),
            ).fetchall()
            return [{**r, "at": r["at"].isoformat()} for r in rows[::-1]]
    return {"messages": await run_in_threadpool(read)}


@app.post("/api/messages")
async def say(request: Request, body: Message):
    wid = current(request)
    text = " ".join(body.text.split())
    if not text:
        raise HTTPException(400, "Type what happened first.")
    return await run_in_threadpool(_talk, wid, client_addr(request), text, "text")


@app.post("/api/voice")
async def voice(request: Request, file: UploadFile = File(...)):
    wid = current(request)
    addr = client_addr(request)
    data = await file.read(settings().limits.voice_max_bytes + 1)
    await file.close()
    lim = settings().limits

    def work():
        h = limits.addr_hash(addr)
        with db.tx() as c:
            limits.check(c, [(f"voice:ws:{wid}", lim.voice_per_copy, limits.COPY),
                             (f"voice:day:{h}", lim.voice_per_day, limits.DAY)],
                         "Your agent has heard as many voice notes as this copy allows.")
        from .sandbox import prepare_voice

        wav, seconds = prepare_voice(data)
        svc = services.factory(wid)
        try:
            transcript = svc.vultr.transcribe(wav)
        finally:
            del wav  # the recording is never stored; the sandbox has already deleted its files
        if not transcript:
            return {"transcript": "", "reply": "I could not make out any words in that voice note.", "drafted": []}
        out = _talk(wid, addr, transcript[:600], "voice")
        return {"transcript": transcript[:600], "seconds": seconds, **out}
    try:
        return await run_in_threadpool(work)
    except SandboxError as exc:
        raise HTTPException(400, exc.message) from None
    except VultrError:
        raise HTTPException(503, "The voice note could not be turned into words just now. Please type it instead.") from None


# --- proposals -------------------------------------------------------------------------------

def _proposal_view(p: dict) -> dict:
    pl = p["payload"] or {}
    return {
        "id": p["id"], "kind": p["kind"], "status": p["status"], "title": p["title"], "detail": p["detail"],
        "job_id": p["job_id"], "created_at": p["created_at"].isoformat(),
        "entry": pl.get("entry") or [], "applies": pl.get("applies") or [], "note": pl.get("note", ""),
        "line": pl.get("line"), "amount_cents": pl.get("amount_cents"), "flag": pl.get("flag", ""),
        "outcome": pl.get("outcome", ""), "choices": pl.get("choices") or [], "text": pl.get("text", ""),
        "party": pl.get("party", ""), "entry_date": pl.get("entry_date"), "ranked": pl.get("ranked") or {},
        "chosen_account": pl.get("chosen_account"),
    }


@app.get("/api/proposals")
async def list_proposals(request: Request, status: str = "pending", kind: str = "", job_id: int = 0,
                         offset: int = 0, limit: int = 30, order: str = "date"):
    wid = current(request)
    statuses = {"pending": ["pending"], "decided": ["approved", "rejected", "posted"], "all": ["pending", "approved", "rejected", "posted"]}
    st = statuses.get(status, ["pending"])
    kinds = [k for k in kind.split(",") if k in ("record", "match", "flag", "followup")] or ["record", "match", "flag", "followup"]
    limit = max(1, min(limit, 60))
    offset = max(0, offset)
    order_sql = {"date": "coalesce(payload->>'entry_date', '') , id", "amount": "abs(coalesce((payload->>'amount_cents')::bigint, 0)) DESC, id",
                 "newest": "id DESC"}.get(order, "id")

    def read():
        with db.tx() as c:
            args = (wid, st, kinds) + ((job_id,) if job_id else ())
            where = "workspace_id = %s AND status = ANY(%s) AND kind = ANY(%s)" + (" AND job_id = %s" if job_id else "")
            total = c.execute(f"SELECT count(*) AS n FROM proposals WHERE {where}", args).fetchone()["n"]
            rows = c.execute(f"SELECT * FROM proposals WHERE {where} ORDER BY {order_sql} OFFSET %s LIMIT %s",
                             args + (offset, limit)).fetchall()
            names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM accounts WHERE workspace_id = %s", (wid,)).fetchall()}
            return {"total": total, "offset": offset, "items": [_proposal_view(r) for r in rows], "accounts": names}
    return await run_in_threadpool(read)


class Decision(BaseModel):
    account: str | None = Field(default=None, max_length=8)
    reason: str = Field(default="", max_length=200)


@app.post("/api/proposals/{pid}/approve")
async def approve(request: Request, pid: int, body: Decision | None = None):
    wid = current(request)

    def work():
        with db.tx() as c:
            return proposals.approve(c, wid, pid, account=(body.account if body else None))
    try:
        return await run_in_threadpool(work)
    except proposals.ProposalError as exc:
        raise HTTPException(409, exc.args[0]) from None


@app.post("/api/proposals/{pid}/reject")
async def reject(request: Request, pid: int, body: Decision | None = None):
    wid = current(request)

    def work():
        with db.tx() as c:
            return proposals.reject(c, wid, pid, (body.reason if body else ""))
    try:
        return await run_in_threadpool(work)
    except proposals.ProposalError as exc:
        raise HTTPException(409, exc.args[0]) from None


class Bulk(BaseModel):
    job_id: int
    which: str = Field(pattern="^(matched|suggested)$")


@app.post("/api/proposals/approve-all")
async def approve_all(request: Request, body: Bulk):
    """Approve every pending match of one kind in a close: the owner's own choice, one tap."""
    wid = current(request)

    def work():
        with db.tx() as c:
            rows = c.execute(
                "SELECT id FROM proposals WHERE workspace_id = %s AND job_id = %s AND kind = 'match' AND status = 'pending' "
                "AND (payload->>'outcome' = 'expense') = %s ORDER BY id",
                (wid, body.job_id, body.which == "suggested"),
            ).fetchall()
            for r in rows:
                proposals.approve(c, wid, r["id"])
            return {"approved": len(rows)}
    return await run_in_threadpool(work)


# --- the month-end close ---------------------------------------------------------------------

@app.post("/api/statement")
async def upload_statement(request: Request, file: UploadFile = File(...)):
    wid = current(request)
    data = await file.read(settings().limits.csv_max_bytes + 1)
    name = "".join(ch for ch in (file.filename or "statement.csv") if ch.isalnum() or ch in "-_.")[:60] or "statement.csv"
    await file.close()

    def work():
        with db.tx() as c:
            limits.check(c, [(f"upload:ws:{wid}", settings().limits.uploads_per_copy, limits.COPY)],
                         "This copy has taken as many statements as it can. Start over for a fresh copy.")
        from .sandbox import read_statement

        st = read_statement(data)
        with db.tx() as c:
            if c.execute("SELECT 1 FROM statements WHERE workspace_id = %s AND sha256 = %s", (wid, st["sha256"])).fetchone():
                return {"ok": True, "same": True}
            sid = close_job.store_statement(c, wid, name, st)
            workspace.audit(c, wid, "owner", "uploaded_statement", "statement", sid, {"lines": len(st["lines"])})
        return {"ok": True, "statement_id": sid, "lines": len(st["lines"]),
                "period": [st["period_start"], st["period_end"]], "balance_checked": st["balance_checked"]}
    try:
        return await run_in_threadpool(work)
    except SandboxError as exc:
        raise HTTPException(400, exc.message) from None


@app.get("/api/statement/october.csv")
async def october_csv(request: Request):
    current(request)
    return Response(seed.statement_csv(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="statement-october-2026.csv"'})


@app.post("/api/close")
async def close(request: Request):
    wid = current(request)
    addr = client_addr(request)

    def work():
        with db.tx() as c:
            job_id, existing = jobs.start_close(c, wid, addr)
            if not existing:
                workspace.audit(c, wid, "owner", "started_close", "job", job_id)
            return {"job_id": job_id, "existing": existing}
    try:
        return await run_in_threadpool(work)
    except SandboxError as exc:
        raise HTTPException(400, exc.message) from None


@app.post("/api/jobs/{job_id}/post")
async def post_approved(request: Request, job_id: int):
    wid = current(request)

    def work():
        with db.tx() as c:
            pid = jobs.start_post(c, wid, job_id)
            workspace.audit(c, wid, "owner", "asked_to_post", "job", job_id)
            return {"job_id": pid}
    try:
        return await run_in_threadpool(work)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def _events(wid: str, job_id: int, after: int) -> tuple[list[dict], str | None]:
    with db.tx() as c:
        job = c.execute("SELECT status FROM jobs WHERE id = %s AND workspace_id = %s", (job_id, wid)).fetchone()
        if job is None:
            return [], None
        rows = c.execute(
            "SELECT id, step, kind, message, data, at FROM job_events WHERE job_id = %s AND id > %s ORDER BY id LIMIT 200",
            (job_id, after),
        ).fetchall()
        return [{**r, "at": r["at"].isoformat()} for r in rows], job["status"]


@app.get("/api/jobs/{job_id}")
async def job(request: Request, job_id: int, after: int = 0):
    wid = current(request)

    def read():
        with db.tx() as c:
            j = c.execute(
                "SELECT id, kind, status, parent_id, summary, created_at, finished_at FROM jobs WHERE id = %s AND workspace_id = %s",
                (job_id, wid),
            ).fetchone()
            if j is None:
                return None
            child = c.execute("SELECT id, status FROM jobs WHERE parent_id = %s ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        events, _status = _events(wid, job_id, after)
        return {"job": {**j, "created_at": j["created_at"].isoformat(),
                        "finished_at": j["finished_at"].isoformat() if j["finished_at"] else None},
                "child": dict(child) if child else None, "events": events}
    data = await run_in_threadpool(read)
    if data is None:
        raise HTTPException(404, "That job is not in this copy.")
    return data


@app.get("/api/jobs/{job_id}/stream")
async def stream(request: Request, job_id: int, after: int = 0):
    """Server-sent events: each step's findings as the worker writes them."""
    wid = current(request)

    async def gen():
        last = after
        idle = 0
        while True:
            if await request.is_disconnected():
                return
            events, status = await run_in_threadpool(_events, wid, job_id, last)
            if status is None:
                yield "event: gone\ndata: {}\n\n"
                return
            for e in events:
                last = e["id"]
                yield f"id: {e['id']}\nevent: step\ndata: {json.dumps(e, default=str)}\n\n"
            if not events and status in ("done", "failed", "waiting"):
                yield f"event: end\ndata: {json.dumps({'status': status})}\n\n"
                return
            idle = idle + 1 if not events else 0
            if idle and idle % 40 == 0:
                yield ": still working\n\n"
            if idle > 1500:  # about ten minutes without a word: let the page poll instead
                return
            await asyncio.sleep(0.35)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# --- the books -------------------------------------------------------------------------------

@app.get("/api/statement/lines")
async def lines(request: Request, status: str = "", offset: int = 0, limit: int = 30, order: str = "date"):
    wid = current(request)
    limit = max(1, min(limit, 60))
    offset = max(0, offset)

    def read():
        with db.tx() as c:
            sid = c.execute("SELECT id FROM statements WHERE workspace_id = %s ORDER BY id DESC LIMIT 1", (wid,)).fetchone()
            if not sid:
                return {"total": 0, "items": [], "offset": 0}
            where = "statement_id = %s"
            args: list = [sid["id"]]
            if status in ("new", "matched", "suggested", "flagged", "posted", "set_aside"):
                where += " AND status = %s"
                args.append(status)
            elif status == "in":
                where += " AND amount_cents > 0"
            elif status == "out":
                where += " AND amount_cents < 0"
            o = {"date": "line_date, line_no", "amount": "abs(amount_cents) DESC, line_no", "newest": "line_date DESC, line_no DESC"}.get(order, "line_date, line_no")
            total = c.execute(f"SELECT count(*) AS n FROM statement_lines WHERE {where}", args).fetchone()["n"]
            rows = c.execute(
                f"SELECT id, line_no, line_date, description, reference, amount_cents, balance_cents, status, note "
                f"FROM statement_lines WHERE {where} ORDER BY {o} OFFSET %s LIMIT %s",
                args + [offset, limit],
            ).fetchall()
            return {"total": total, "offset": offset,
                    "items": [{**r, "line_date": r["line_date"].isoformat()} for r in rows]}
    return await run_in_threadpool(read)


@app.get("/api/journal")
async def journal(request: Request, offset: int = 0, limit: int = 30, source: str = "", order: str = "newest"):
    wid = current(request)
    limit = max(1, min(limit, 60))

    def read():
        with db.tx() as c:
            rows, total = books.journal(c, wid, offset=max(0, offset), limit=limit,
                                        source=source if source in ("opening", "books", "agent", "close") else None,
                                        order="oldest" if order == "oldest" else "newest")
            tb = ledger.trial_balance(c, wid)
            d, cr = ledger.totals(tb)
            return {"total": total, "offset": offset,
                    "items": [{**r, "entry_date": r["entry_date"].isoformat(), "posted_at": r["posted_at"].isoformat()} for r in rows],
                    "trial_balance": {"debit": d, "credit": cr,
                                      "accounts": [r for r in tb if r["debit"] or r["credit"]]}}
    return await run_in_threadpool(read)


@app.get("/api/debtors")
async def debtors(request: Request):
    wid = current(request)

    def read():
        with db.tx() as c:
            ws = workspace.get(c, wid)
            d = books.who_owes(c, wid, ws["period_end"])
            return {"as_at": ws["period_end"].isoformat(), "customers": d, "ageing": books.ageing(d),
                    "total": sum(x["open_cents"] for x in d), "late": sum(x["late_cents"] for x in d)}
    return await run_in_threadpool(read)


@app.get("/api/audit")
async def audit_log(request: Request, offset: int = 0, limit: int = 30):
    wid = current(request)
    limit = max(1, min(limit, 60))

    def read():
        with db.tx() as c:
            total = c.execute("SELECT count(*) AS n FROM audit_log WHERE workspace_id = %s", (wid,)).fetchone()["n"]
            rows = c.execute(
                "SELECT id, at, actor, action, object_type, object_id, detail FROM audit_log WHERE workspace_id = %s "
                "ORDER BY id DESC OFFSET %s LIMIT %s",
                (wid, max(0, offset), limit),
            ).fetchall()
            return {"total": total, "offset": offset, "items": [{**r, "at": r["at"].isoformat()} for r in rows]}
    return await run_in_threadpool(read)


@app.get("/api/format")
async def fmt(cents: int):
    """A tiny helper the page uses in tests; it formats money the same way as the server."""
    return {"text": rand(cents)}
