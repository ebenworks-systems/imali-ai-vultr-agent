"""The web app: headers, request checks, limits, the copy and the close over HTTP."""

from __future__ import annotations

import pytest
from fakes import FakeLLM, FakeVultr, say, services, tool_call
from fastapi.testclient import TestClient

from app import db, seed, services as svc_mod, worker

pytestmark = pytest.mark.db

H = {"X-Requested-With": "imali"}


@pytest.fixture()
def client(db_url, monkeypatch):
    monkeypatch.setattr(svc_mod, "factory", services())
    with db.tx() as c:
        c.execute("DELETE FROM rate_counters")
    from app.main import app

    with TestClient(app, base_url="https://testserver") as cl:
        yield cl
        cl.delete("/api/copy", headers=H)


def _open(cl) -> None:
    r = cl.post("/api/copy", headers=H)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("path", ["/", "/healthz", "/api/state", "/no-such-page", "/static/index.html"])
def test_security_headers_on_every_path(client, path):
    r = client.get(path)
    for h in ("content-security-policy", "x-content-type-options", "x-frame-options", "referrer-policy",
              "permissions-policy"):
        assert h in r.headers, (path, h)
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_a_write_without_the_app_header_or_from_another_site_is_refused(client):
    assert client.post("/api/copy").status_code == 403
    r = client.post("/api/copy", headers={**H, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_bodies_over_the_cap_are_refused(client):
    _open(client)
    r = client.post("/api/messages", headers={**H, "Content-Type": "application/json"}, content=b"{" + b" " * 9000 + b"}")
    assert r.status_code == 413
    big = b"Date,Description,Amount,Balance\n" + b"x" * 30_000
    r = client.post("/api/statement", headers=H, files={"file": ("s.csv", big, "text/csv")})
    assert r.status_code == 413


def test_errors_never_echo_what_was_sent(client):
    _open(client)
    secret = "SELECT-ME-<script>"
    r = client.post("/api/messages", headers=H, json={"text": secret * 100})
    assert r.status_code == 400 and secret not in r.text
    r = client.post("/api/statement", headers=H, files={"file": ("s.csv", f"Date,Description\n{secret},x\n".encode(), "text/csv")})
    assert r.status_code == 400 and secret not in r.text


def test_no_copy_means_no_books(client):
    r = client.get("/api/state")
    assert r.status_code == 200 and r.json() == {"open": False}
    client.cookies.set("imali_copy", "forged.123.sig")
    assert client.get("/api/journal").status_code == 401


def test_a_copy_opens_with_the_books_and_start_over_replaces_it(client):
    _open(client)
    first = client.cookies.get("imali_copy")
    s = client.get("/api/state").json()
    assert s["shop"] == seed.SHOP and s["period"]["label"] == "October 2026"
    assert s["bank"] > 0 and s["debtors"] > 0
    _open(client)
    assert client.cookies.get("imali_copy") != first
    with db.tx() as c:
        wid = first.split(".")[0]
        assert c.execute("SELECT 1 FROM workspaces WHERE id = %s", (wid,)).fetchone() is None


def test_copies_per_address_are_limited(client):
    for _ in range(6):
        _open(client)
    r = client.post("/api/copy", headers=H)
    assert r.status_code == 429 and "as many copies" in r.json()["error"]


def test_chat_drafts_a_card_and_approve_posts_it(client, monkeypatch):
    llm = FakeLLM([
        tool_call("draft_record", {"kind": "expense", "amount": "850", "expense_account": "6300",
                                   "paid_with": "cash", "description": "diesel for the bakkie"}),
        say("I drafted R850.00 for fuel. It waits for your approval."),
    ])
    monkeypatch.setattr(svc_mod, "factory", services(FakeVultr(), llm))
    _open(client)
    r = client.post("/api/messages", headers=H, json={"text": "Paid 850 cash for diesel for the bakkie"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["drafted"] and body["drafted"][0]["status"] == "pending"
    pid = body["drafted"][0]["id"]
    r = client.post(f"/api/proposals/{pid}/approve", headers=H, json={})
    assert r.json()["status"] == "posted"
    assert client.post(f"/api/proposals/{pid}/approve", headers=H, json={}).status_code == 409
    j = client.get("/api/journal?source=agent").json()
    assert j["total"] == 1 and j["trial_balance"]["debit"] == j["trial_balance"]["credit"]
    msgs = client.get("/api/messages").json()["messages"]
    assert [m["role"] for m in msgs] == ["owner", "agent"]


def test_one_copy_cannot_touch_another_copys_items(client, monkeypatch):
    llm = FakeLLM([
        tool_call("draft_record", {"kind": "cash_sale", "amount": "100", "paid_with": "cash", "description": "nails"}),
        say("Drafted."),
    ])
    monkeypatch.setattr(svc_mod, "factory", services(FakeVultr(), llm))
    _open(client)
    pid = client.post("/api/messages", headers=H, json={"text": "Sold nails for 100 cash"}).json()["drafted"][0]["id"]
    other = TestClient(client.app, base_url="https://testserver")
    assert other.post("/api/copy", headers=H).status_code == 200
    assert other.post(f"/api/proposals/{pid}/approve", headers=H, json={}).status_code == 409
    other.delete("/api/copy", headers=H)


def test_the_close_over_http_streams_its_steps(client):
    _open(client)
    r = client.post("/api/close", headers=H)
    job_id = r.json()["job_id"]
    assert worker.run_one()
    with client.stream("GET", f"/api/jobs/{job_id}/stream") as s:
        text = "".join(s.iter_text())
    assert "event: step" in text and "event: end" in text and '"status": "waiting"' in text
    lines = client.get("/api/statement/lines?status=flagged").json()
    assert lines["total"] == 6
    page = client.get("/api/statement/lines?limit=10&offset=10").json()
    assert page["total"] == len(seed.STATEMENT_ROWS) and len(page["items"]) == 10
    props = client.get(f"/api/proposals?job_id={job_id}&kind=match&limit=5").json()
    assert len(props["items"]) == 5 and props["total"] == len(seed.STATEMENT_ROWS) - 6
    r = client.post("/api/proposals/approve-all", headers=H, json={"job_id": job_id, "which": "matched"})
    assert r.json()["approved"] > 0


def test_a_new_statement_upload_is_read_and_a_repeat_is_noticed(client):
    _open(client)
    csv = seed.statement_csv()
    r = client.post("/api/statement", headers=H, files={"file": ("my bank.csv", csv, "text/csv")})
    assert r.status_code == 200 and r.json()["lines"] == len(seed.STATEMENT_ROWS)
    r = client.post("/api/statement", headers=H, files={"file": ("again.csv", csv, "text/csv")})
    assert r.json().get("same") is True


def test_the_voice_route_refuses_what_is_not_audio(client):
    _open(client)
    r = client.post("/api/voice", headers=H, files={"file": ("note.webm", b"not audio at all" * 50, "audio/webm")})
    err = r.json()["error"]
    assert r.status_code == 400 and ("could not be read" in err or "format" in err)
