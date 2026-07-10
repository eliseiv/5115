"""Live e2e helpers: env loading, JWT (RS256) and StoreKit (HS256) token minting,
DB inspection, log scanning. NOT unit tests — real HTTP against the running container.

Secrets are never printed. Tokens are minted in-memory.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import httpx
import jwt

BASE = "http://127.0.0.1:8000"
ROOT = "D:/BA/claude-ios"
COMPOSE = [
    "docker",
    "compose",
    "-f",
    f"{ROOT}/docker-compose.yml",
    "-f",
    f"{ROOT}/docker-compose.e2e.yml",
]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    with open(f"{ROOT}/.env", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k] = v
    return env


ENV = load_env()
with open(f"{ROOT}/.secrets/e2e/jwt_private_key.pem", "rb") as _pk:
    PRIV_KEY = _pk.read()
STOREKIT_SECRET = ENV["STOREKIT_TEST_SECRET"]
BUNDLE_ID = ENV["APPSTORE_BUNDLE_ID"]


def mint_jwt(
    user_id: str,
    *,
    device_id: str = "dev-e2e",
    exp_delta: int = 3600,
    iss: str | None = None,
    aud: str | None = None,
) -> str:
    return jwt.encode(
        {
            "sub": user_id,
            "iss": iss if iss is not None else ENV["JWT_ISSUER"],
            "aud": aud if aud is not None else ENV["JWT_AUDIENCE"],
            "exp": int(time.time()) + exp_delta,
            "device_id": device_id,
        },
        PRIV_KEY,
        algorithm="RS256",
    )


def storekit_tx(
    *,
    transaction_id: str,
    expires_ms: int,
    product_id: str = "pro.monthly",
    original_tx: str | None = None,
    revocation_ms: int | None = None,
    secret: str | None = None,
    bundle_id: str | None = None,
) -> dict:
    payload = {
        "transactionId": transaction_id,
        "originalTransactionId": original_tx or transaction_id,
        "productId": product_id,
        "expiresDate": expires_ms,
        "environment": "sandbox",
        "bundleId": bundle_id if bundle_id is not None else BUNDLE_ID,
    }
    if revocation_ms is not None:
        payload["revocationDate"] = revocation_ms
    token = jwt.encode(
        payload, secret if secret is not None else STOREKIT_SECRET, algorithm="HS256"
    )
    return token


def auth(user_id: str, **kw) -> dict[str, str]:
    h = {
        "Authorization": "Bearer " + mint_jwt(user_id, **kw),
        "Content-Type": "application/json",
        "X-Device-Id": kw.get("device_id", "dev-e2e"),
    }
    return h


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=120.0)


def psql(sql: str) -> str:
    cmd = COMPOSE + [
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "claude_ios",
        "-tАc".replace("А", ""),
        sql,
    ]
    # build properly
    cmd = COMPOSE + [
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "claude_ios",
        "-tAc",
        sql,
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return (out.stdout or "").strip()


def api_logs(tail: int = 2000) -> str:
    cmd = COMPOSE + ["logs", "--no-color", "--tail", str(tail), "api"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return (out.stdout or "") + (out.stderr or "")


def mint_session(user_id: str, *, mode: str = "credits", project_id: str = "p1") -> str:
    """Insert a valid chat_sessions row owned by user_id and return its id.

    /v1/wallet/consume validates sessionId before debit (wallet-ledger/02): an unknown id → 404,
    a foreign id → 403. To exercise the balance/idempotency paths we need a REAL owned session.
    We create it directly in the DB (the user row must already exist — e.g. provisioned by a prior
    subscription/sync) to avoid spending a credit or a real Claude call just to obtain a sessionId.
    """
    sid = str(uuid.uuid4())
    psql(
        "INSERT INTO chat_sessions (id, user_id, project_id, mode) "
        f"VALUES ('{sid}', '{user_id}', '{project_id}', '{mode}')"
    )
    return sid
