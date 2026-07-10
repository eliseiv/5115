"""Live e2e for admin-module + website-builder against the running container.

Run: python tests/e2e_live/run_admin_website_e2e.py
Real Claude (ANTHROPIC_API_KEY from .env), real PostgreSQL/Redis (compose). Secrets never printed.
Prints PASS/FAIL per scenario and a JSON summary at the end.
"""

from __future__ import annotations

import json
import sys
import time
import uuid

sys.path.insert(0, "D:/BA/claude-ios/tests/e2e_live")
from conftest_live import (  # noqa: E402
    ENV,
    api_logs,
    auth,
    client,
    psql,
)

ADMIN_SECRET = ENV["ADMIN_API_SECRET"]
PREVIEW_SECRET = ENV.get("PREVIEW_URL_SECRET", "")

RESULTS: list[dict] = []


def rec(scenario: str, ok: bool, detail: str, blame: str | None = None) -> None:
    RESULTS.append(
        {"id": scenario, "pass": ok, "detail": detail, **({"blame": blame} if blame else {})}
    )
    print(f"[{'PASS' if ok else 'FAIL'}] {scenario}: {detail}")


def new_uid() -> str:
    return str(uuid.uuid4())


def provision_user(c, uid: str) -> None:
    """Provision the users row via an authenticated call (lazy provisioning, ADR-007)."""
    c.get("/v1/policy/effective", headers=auth(uid))


def main() -> None:  # noqa: C901
    c = client()
    admin_h = {"X-Admin-Token": ADMIN_SECRET, "Content-Type": "application/json"}

    # ============================== ADMIN LIVE ==============================
    # ADM-LIVE-1: grant increases balance + audit admin_grant, no double-charge on replay.
    uid = new_uid()
    provision_user(c, uid)
    key = f"e2e-grant-{uuid.uuid4().hex[:8]}"
    body = {"userId": uid, "amount": 42, "idempotencyKey": key, "reason": "e2e support grant"}
    r1 = c.post("/v1/admin/wallet/grant", headers=admin_h, content=json.dumps(body))
    ok1 = r1.status_code == 200 and r1.json().get("newBalance") == 42
    bal = psql(f"SELECT balance FROM wallets WHERE user_id='{uid}'")
    audit_grant = psql(
        f"SELECT count(*) FROM audit_logs WHERE user_id='{uid}' AND event_type='admin_grant'"
    )
    rec(
        "ADM-LIVE-1 grant+balance+audit",
        ok1 and bal == "42" and audit_grant == "1",
        f"status={r1.status_code} newBalance={r1.json().get('newBalance')} db_balance={bal} "
        f"admin_grant_audit={audit_grant}",
        blame=None if (ok1 and bal == "42" and audit_grant == "1") else "code",
    )

    # ADM-LIVE-2: idempotent replay → no second credit.
    r2 = c.post("/v1/admin/wallet/grant", headers=admin_h, content=json.dumps(body))
    replay = r2.json().get("idempotentReplay") is True
    bal2 = psql(f"SELECT balance FROM wallets WHERE user_id='{uid}'")
    credits = psql(
        f"SELECT count(*) FROM ledger_transactions WHERE user_id='{uid}' AND type='credit'"
    )
    rec(
        "ADM-LIVE-2 idempotent replay",
        r2.status_code == 200 and replay and bal2 == "42" and credits == "1",
        f"status={r2.status_code} idempotentReplay={r2.json().get('idempotentReplay')} "
        f"db_balance={bal2} credit_rows={credits}",
        blame=None if (replay and bal2 == "42" and credits == "1") else "code",
    )

    # ADM-LIVE-3: no token → 401.
    r3 = c.post(
        "/v1/admin/wallet/grant",
        headers={"Content-Type": "application/json"},
        content=json.dumps({"userId": uid, "amount": 1, "idempotencyKey": "x", "reason": "x"}),
    )
    rec(
        "ADM-LIVE-3 no token 401",
        r3.status_code == 401,
        f"status={r3.status_code}",
        blame=None if r3.status_code == 401 else "code",
    )

    # ADM-LIVE-4: user JWT (no X-Admin-Token) → 401/403.
    r4 = c.post(
        "/v1/admin/wallet/grant",
        headers=auth(uid),
        content=json.dumps({"userId": uid, "amount": 1, "idempotencyKey": "y", "reason": "x"}),
    )
    rec(
        "ADM-LIVE-4 user JWT not admin",
        r4.status_code in (401, 403),
        f"status={r4.status_code}",
        blame=None if r4.status_code in (401, 403) else "code",
    )

    # ===== WEBSITE-BUILDER LIVE (real Claude) =====
    wuid = new_uid()
    provision_user(c, wuid)
    # Grant credits so the chat run is allowed in mode=credits.
    c.post(
        "/v1/admin/wallet/grant",
        headers=admin_h,
        content=json.dumps(
            {
                "userId": wuid,
                "amount": 100,
                "idempotencyKey": f"wb-{uuid.uuid4().hex[:8]}",
                "reason": "e2e website builder",
            }
        ),
    )
    bal_before = psql(f"SELECT balance FROM wallets WHERE user_id='{wuid}'")
    project_id = f"e2e-site-{uuid.uuid4().hex[:8]}"

    run_body = {
        "userId": wuid,
        "projectId": project_id,
        "message": (
            "Сделай простой одностраничный лендинг: создай файл index.html с заголовком "
            "и парой абзацев, используя инструменты сайта (site.write_file). Затем получи "
            "ссылку предпросмотра через site.preview."
        ),
        "mode": "credits",
    }
    rr = c.post("/v1/chat/run", headers=auth(wuid), content=json.dumps(run_body))
    run_ok = rr.status_code == 200
    run_json = rr.json() if run_ok else {}
    status = run_json.get("status")

    # WB-LIVE-1: Claude drove site.* server-side tool-loop and the backend persisted site_files.
    pid_row = psql(
        f"SELECT id FROM projects WHERE user_id='{wuid}' AND external_project_id='{project_id}'"
    )
    file_count = psql(
        f"SELECT count(*) FROM site_files sf JOIN projects p ON sf.project_id=p.id "
        f"WHERE p.user_id='{wuid}' AND p.external_project_id='{project_id}'"
    )
    has_html = psql(
        f"SELECT count(*) FROM site_files sf JOIN projects p ON sf.project_id=p.id "
        f"WHERE p.user_id='{wuid}' AND p.external_project_id='{project_id}' "
        f"AND sf.content_type LIKE 'text/html%'"
    )
    wb1_ok = (
        run_ok
        and bool(pid_row)
        and file_count.isdigit()
        and int(file_count) >= 1
        and has_html != "0"
    )
    rec(
        "WB-LIVE-1 site.write_file persisted via server-side loop",
        wb1_ok,
        f"run_status={rr.status_code}/{status} project={'yes' if pid_row else 'no'} "
        f"site_files={file_count} html_files={has_html}",
        blame=None if wb1_ok else "code",
    )

    # WB-LIVE-2: final answer is assistant_message (server-side tools never round-trip to iOS).
    rec(
        "WB-LIVE-2 final assistant_message (no client round-trip)",
        status == "assistant_message",
        f"status={status}",
        blame=None if status == "assistant_message" else "code",
    )

    # WB-LIVE-3: exactly 1 credit charged for the whole message (ADR-006).
    bal_after = psql(f"SELECT balance FROM wallets WHERE user_id='{wuid}'")
    try:
        charged = int(bal_before) - int(bal_after)
    except ValueError:
        charged = -1
    rec(
        "WB-LIVE-3 exactly 1 credit per message",
        charged == 1,
        f"balance_before={bal_before} balance_after={bal_after} charged={charged}",
        blame=None if charged == 1 else "code",
    )

    # WB-LIVE-4: obtain a real preview URL and GET it → 200 HTML with sandbox headers.
    # The signed URL is built with the SAME HMAC secret the container uses (PREVIEW_URL_SECRET in
    # the e2e .env) and the same canonical form as app/website/signed_url.py — this exercises the
    # real /v1/preview endpoint's verification (server signs identically).
    preview_url = None
    pick = psql(
        f"SELECT sf.path FROM site_files sf JOIN projects p ON sf.project_id=p.id "
        f"WHERE p.user_id='{wuid}' AND p.external_project_id='{project_id}' "
        f"AND sf.content_type LIKE 'text/html%' ORDER BY sf.path LIMIT 1"
    )

    def _build_preview_token(project_uuid: str, owner: str, ttl: int = 900) -> str:
        import base64
        import hashlib
        import hmac

        exp = int(time.time()) + ttl
        canonical = f"{project_uuid}|{owner}|{exp}".encode()
        mac = hmac.new(PREVIEW_SECRET.encode(), canonical, hashlib.sha256).digest()
        b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")  # noqa: E731
        return f"{b64(str(exp).encode('ascii'))}.{b64(mac)}"

    if pid_row and pick and PREVIEW_SECRET:
        token = _build_preview_token(pid_row, wuid)
        preview_url = f"/v1/preview/{pid_row}/{token}/{pick}"

    if preview_url:
        gr = c.get(preview_url)
        csp = gr.headers.get("content-security-policy", "")
        ct = gr.headers.get("content-type", "")
        ok_prev = (
            gr.status_code == 200
            and "sandbox" in csp
            and gr.headers.get("x-content-type-options") == "nosniff"
            and "no-store" in gr.headers.get("cache-control", "")
            and "set-cookie" not in {k.lower() for k in gr.headers}
            and ct.startswith("text/html")
            and len(gr.content) > 0
        )
        rec(
            "WB-LIVE-4 GET preview URL → 200 HTML + sandbox headers",
            ok_prev,
            f"status={gr.status_code} ct={ct} sandbox={'sandbox' in csp} bytes={len(gr.content)}",
            blame=None if ok_prev else "code",
        )

        # WB-LIVE-5: tampered token → 403/404.
        parts = preview_url.rsplit("/", 2)  # .../{token}/{path}
        if len(parts) == 3:
            tampered = f"{parts[0]}/{parts[1][:-2]}xx/{parts[2]}" if len(parts[1]) > 2 else parts[0]
            tr = c.get(tampered)
            rec(
                "WB-LIVE-5 tampered preview token → 403/404",
                tr.status_code in (403, 404),
                f"status={tr.status_code}",
                blame=None if tr.status_code in (403, 404) else "code",
            )
        # WB-LIVE-6: foreign/garbage project id → 404.
        fr = c.get(
            f"/v1/preview/{uuid.uuid4()}/{parts[1] if len(parts) == 3 else 'x.y'}/index.html"
        )
        rec(
            "WB-LIVE-6 unknown project preview → 404",
            fr.status_code == 404,
            f"status={fr.status_code}",
            blame=None if fr.status_code == 404 else "code",
        )
    else:
        rec(
            "WB-LIVE-4 GET preview URL → 200 HTML + sandbox headers",
            False,
            "no signed preview URL captured from site.preview tool output/logs",
            blame="test",
        )

    # ============================== SECRET LEAK SCAN ==============================
    logs = api_logs(tail=3000)
    leak = (ADMIN_SECRET in logs) or (PREVIEW_SECRET and PREVIEW_SECRET in logs)
    rec(
        "SEC-LIVE-1 admin/preview secrets absent from logs",
        not leak,
        "no admin/preview secret found in api logs" if not leak else "SECRET FOUND IN LOGS",
        blame=None if not leak else "code",
    )

    # ============================== SUMMARY ==============================
    passed = sum(1 for r in RESULTS if r["pass"])
    total = len(RESULTS)
    summary = {"total": total, "passed": passed, "failed": total - passed, "results": RESULTS}
    print("\n=== LIVE E2E SUMMARY (admin + website-builder) ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
