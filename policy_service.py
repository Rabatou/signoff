"""SignOff policy co-signer.

An independent service that holds the second treasury key. It receives a
payment already signed by the agent, checks it against the operator's
policy, and only then adds its own signature and submits to XRPL.

The agent never sees this key, the policy file, or the admin token.
Run: uvicorn policy_service:app --port 8000
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from xrpl.core.binarycodec import decode
from xrpl.models.requests import Tx
from xrpl.models.transactions.transaction import Transaction
from xrpl.transaction import multisign, sign, simulate, submit_and_wait
from xrpl.wallet import Wallet

from common import (
    DATA,
    asset_currency,
    asset_issuer,
    get_client,
    load_json,
    need,
    save_json,
    tx_link,
)
from policy_rules import DEFAULT_MAX_FEE_DROPS, evaluate, invoice_hash, parse_amount

TREASURY = need("TREASURY_ADDRESS")
AGENT_ADDRESS = need("AGENT_ADDRESS")
ADMIN_TOKEN = need("ADMIN_TOKEN")
POLICY_WALLET = Wallet.from_seed(need("POLICY_SEED"))
DB_PATH = DATA / "journal.db"

client = get_client()
lock = threading.Lock()  # serialise check + submit so the daily cap can't be raced
app = FastAPI(title="SignOff policy co-signer")


# ---------------------------------------------------------------- journal
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            invoice_id TEXT,
            destination TEXT,
            amount TEXT,
            decision TEXT NOT NULL,      -- APPROVED / REJECTED
            status TEXT NOT NULL,        -- REJECTED / SUBMITTING / UNCERTAIN / EXECUTED / FAILED
            reasons TEXT,
            justification TEXT,
            tx_hash TEXT,
            ledger_result TEXT
        )""")
    return conn


# Statuses whose amount may have left the treasury: they count against the daily cap.
COMMITTED = ("EXECUTED", "SUBMITTING", "UNCERTAIN")


def ledger_outcome(tx_hash: str) -> str | None:
    """Final TransactionResult of a validated tx, or None if not (yet) known."""
    try:
        res = client.request(Tx(transaction=tx_hash)).result
    except Exception:
        return None
    if res.get("validated"):
        return res.get("meta", {}).get("TransactionResult")
    return None


def reconcile() -> None:
    """Resolve submissions whose outcome was unknown (timeout, crash) against the ledger."""
    with db() as conn:
        rows = conn.execute(
            "SELECT id, tx_hash FROM journal WHERE status IN ('SUBMITTING','UNCERTAIN')"
        ).fetchall()
    for r in rows:
        result = ledger_outcome(r["tx_hash"])
        if result is not None:
            update(r["id"], status="EXECUTED" if result == "tesSUCCESS" else "FAILED",
                   ledger_result=result)


def invoice_already_paid(invoice_id: str) -> bool:
    marks = ", ".join("?" for _ in COMMITTED)
    with db() as conn:
        row = conn.execute(
            f"SELECT 1 FROM journal WHERE invoice_id = ? AND status IN ({marks}) LIMIT 1",
            (invoice_id, *COMMITTED),
        ).fetchone()
    return row is not None


def simulation_error(tx: Transaction) -> str | None:
    """Dry-run the payment on the ledger. Returns the failure code, or None if it would pass.

    Catches missing trust lines, insufficient funds or reserve, etc. before any
    signature or fee. If the node cannot simulate, we do not block on it.
    """
    try:
        result = simulate(tx, client).result.get("engine_result")
    except Exception as exc:
        code = getattr(exc, "args", [{}])[0]
        result = code.get("engine_result") if isinstance(code, dict) else None
        if result is None:
            return None
    if result and result.startswith(("tec", "tem")):  # definitive failures only
        return result
    return None


def spent_today() -> Decimal:
    today = datetime.now(timezone.utc).date().isoformat()
    marks = ", ".join("?" for _ in COMMITTED)
    with db() as conn:
        rows = conn.execute(
            f"SELECT amount FROM journal WHERE status IN ({marks}) AND ts LIKE ?",
            (*COMMITTED, today + "%"),
        ).fetchall()
    return sum((Decimal(r["amount"]) for r in rows), Decimal(0))


def log(**fields) -> int:
    fields.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with db() as conn:
        cur = conn.execute(f"INSERT INTO journal ({cols}) VALUES ({marks})", tuple(fields.values()))
        return cur.lastrowid


def update(row_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE journal SET {sets} WHERE id = ?", (*fields.values(), row_id))


# ---------------------------------------------------------------- policy
def load_policy() -> dict:
    policy = load_json("policy.json")
    if policy is None:
        raise HTTPException(500, "data/policy.json missing — run setup.py")
    return policy


class CosignRequest(BaseModel):
    tx_blob: str = Field(..., description="Payment signed by the agent key (multisign)")
    invoice_id: str = Field(..., min_length=1, max_length=64)
    justification: str | None = None


class PolicyUpdate(BaseModel):
    paused: bool
    per_tx_max: str
    daily_max: str
    allowlist: dict[str, str]
    require_memo: bool = True
    max_fee_drops: int = DEFAULT_MAX_FEE_DROPS


def check_admin(token: str | None) -> None:
    if token != ADMIN_TOKEN:
        raise HTTPException(401, "Operator token required.")


# ---------------------------------------------------------------- routes
@app.get("/health")
def health():
    return {"ok": True, "treasury": TREASURY, "policy_signer": POLICY_WALLET.address}


@app.get("/policy")
def get_policy():
    return load_policy()


@app.put("/policy")
def put_policy(body: PolicyUpdate, x_admin_token: str | None = Header(default=None)):
    check_admin(x_admin_token)
    for name in ("per_tx_max", "daily_max"):
        try:
            value = Decimal(getattr(body, name))
        except InvalidOperation:
            raise HTTPException(400, f"{name} must be a number")
        if not value.is_finite() or value < 0:
            raise HTTPException(400, f"{name} must be a finite number >= 0")
    if body.max_fee_drops < 0:
        raise HTTPException(400, "max_fee_drops must be >= 0")
    policy = body.model_dump()
    policy["updated_by"] = "operator"
    policy["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json("policy.json", policy)
    return policy


@app.get("/journal")
def journal(limit: int = 100):
    with db() as conn:
        rows = conn.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"]) if d["reasons"] else []
        d["explorer"] = tx_link(d["tx_hash"]) if d["tx_hash"] else None
        out.append(d)
    return out


@app.post("/cosign")
def cosign(req: CosignRequest):
    try:
        decoded = decode(req.tx_blob)
    except Exception as exc:
        raise HTTPException(400, f"Cannot decode tx_blob: {exc}")

    try:
        _, _, value = parse_amount(decoded.get("Amount"))
        amount = str(value)
    except Exception:
        amount = None

    with lock:
        reconcile()
        policy = load_policy()
        violations = evaluate(decoded, policy, {
            "treasury": TREASURY,
            "agent_address": AGENT_ADDRESS,
            "asset_currency": asset_currency(),
            "asset_issuer": asset_issuer(),
            "spent_today": spent_today(),
            "invoice_hash": invoice_hash(req.invoice_id),
            "invoice_already_paid": invoice_already_paid(req.invoice_id),
        })
        unsigned = Transaction.from_xrpl({k: v for k, v in decoded.items() if k != "Signers"})
        if not violations:
            sim = simulation_error(unsigned)
            if sim:
                violations.append(f"Ledger simulation failed ({sim}); nothing was signed or spent.")
        if violations:
            log(invoice_id=req.invoice_id, destination=decoded.get("Destination"), amount=amount,
                decision="REJECTED", status="REJECTED", reasons=json.dumps(violations),
                justification=req.justification)
            raise HTTPException(403, {"decision": "REJECTED", "violations": violations})

        # Rebuild the exact transaction the agent signed, add the policy signature.
        agent_signed = Transaction.from_xrpl(decoded)
        policy_signed = sign(unsigned, POLICY_WALLET, multisign=True)
        full = multisign(unsigned, [agent_signed, policy_signed])
        tx_hash = full.get_hash()

        # Record the hash BEFORE submitting, so a crash can be reconciled.
        row = log(invoice_id=req.invoice_id, destination=decoded.get("Destination"), amount=amount,
                  decision="APPROVED", status="SUBMITTING", justification=req.justification,
                  tx_hash=tx_hash)
        try:
            resp = submit_and_wait(full, client)
            result = resp.result["meta"]["TransactionResult"]
        except Exception as exc:
            # The tx may still have been validated: check the ledger before deciding.
            result = ledger_outcome(tx_hash)
            if result is None:
                # Unknown outcome: keep counting it against the daily cap until reconciled.
                update(row, status="UNCERTAIN", ledger_result=str(exc)[:300])
                raise HTTPException(502, {"decision": "APPROVED", "status": "UNCERTAIN",
                                          "error": str(exc), "tx_hash": tx_hash})

        status = "EXECUTED" if result == "tesSUCCESS" else "FAILED"
        update(row, status=status, ledger_result=result)

    return {"decision": "APPROVED", "status": status, "ledger_result": result,
            "tx_hash": tx_hash, "explorer": tx_link(tx_hash)}
