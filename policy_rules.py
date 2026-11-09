"""Pure policy rules for the SignOff co-signer.

No XRPL or network imports here on purpose: this module only looks at a
decoded transaction (XRPL JSON, CamelCase fields) and returns the list of
violated rules. That keeps it easy to unit-test and easy to audit.
"""
from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any

TF_PARTIAL_PAYMENT = 0x00020000
DROPS_PER_XRP = Decimal(1_000_000)
DEFAULT_MAX_FEE_DROPS = 1000  # 0.001 XRP; a 2-signer payment normally costs ~30 drops

# Fields that can change how much is really delivered or spent.
# The agent has no reason to use them, so the co-signer refuses them.
FORBIDDEN_FIELDS = ("SendMax", "DeliverMin", "Paths")


def invoice_hash(invoice_id: str) -> str:
    """XRPL InvoiceID for an invoice: SHA-256 of its ID (same binding as x402 on XRPL)."""
    return hashlib.sha256(invoice_id.encode("utf-8")).hexdigest().upper()


def currency_code(code: str) -> str:
    """Return the on-ledger form of a currency code (3 chars or 40-hex)."""
    if code == "XRP" or len(code) == 3:
        return code
    if len(code) == 40:
        return code.upper()
    return code.encode("ascii").hex().upper().ljust(40, "0")


def parse_amount(amount: Any) -> tuple[str, str | None, Decimal]:
    """Return (currency, issuer, value) from an XRPL JSON Amount."""
    if isinstance(amount, str):  # XRP in drops
        return "XRP", None, Decimal(amount) / DROPS_PER_XRP
    return (
        currency_code(amount["currency"]),
        amount.get("issuer"),
        Decimal(str(amount["value"])),
    )


def evaluate(tx: dict, policy: dict, ctx: dict) -> list[str]:
    """Check a decoded, agent-signed transaction against the operator policy.

    ctx keys:
      treasury        classic address of the business treasury
      agent_address   address of the agent's signing key
      asset_currency  expected currency ("XRP" or on-ledger code)
      asset_issuer    expected issuer (None for XRP)
      spent_today     Decimal, amount already executed today (UTC)
      invoice_hash    expected InvoiceID (SHA-256 of the invoice being paid)
      invoice_already_paid  True if this invoice was already paid or is in flight
    Returns a list of human-readable violations (empty = approved).
    """
    v: list[str] = []

    if policy.get("paused"):
        v.append("Agent payments are paused by the operator.")

    if tx.get("TransactionType") != "Payment":
        v.append(f"Only Payment transactions are allowed (got {tx.get('TransactionType')}).")

    if tx.get("Account") != ctx["treasury"]:
        v.append("Transaction does not spend from the business treasury account.")

    signers = tx.get("Signers") or []
    if len(signers) != 1 or signers[0].get("Signer", {}).get("Account") != ctx["agent_address"]:
        v.append("Transaction must carry exactly one signature, from the registered agent key.")

    for field in FORBIDDEN_FIELDS:
        if field in tx:
            v.append(f"Field {field} is not allowed for agent payments.")

    flags = int(tx.get("Flags", 0) or 0)
    if flags & TF_PARTIAL_PAYMENT:
        v.append("Partial payments are not allowed.")

    max_fee = int(policy.get("max_fee_drops", DEFAULT_MAX_FEE_DROPS))
    try:
        fee = int(tx.get("Fee"))
    except (TypeError, ValueError):
        v.append("Fee is missing or malformed.")
    else:
        if fee < 0 or fee > max_fee:
            v.append(f"Fee {fee} drops exceeds the maximum of {max_fee} drops.")

    if "LastLedgerSequence" not in tx:
        v.append("LastLedgerSequence is required so the transaction cannot linger.")

    expected_invoice = ctx.get("invoice_hash")
    if not expected_invoice or str(tx.get("InvoiceID", "")).upper() != expected_invoice:
        v.append("InvoiceID must be the SHA-256 of the invoice being paid.")
    if ctx.get("invoice_already_paid"):
        v.append("This invoice was already paid (or a payment for it is in flight).")

    if policy.get("require_memo", True) and not tx.get("Memos"):
        v.append("A memo with the invoice reference is required (audit trail).")

    destination = tx.get("Destination")
    allowlist = policy.get("allowlist", {})
    if destination not in allowlist:
        v.append(f"Destination {destination} is not an approved supplier.")

    try:
        currency, issuer, value = parse_amount(tx.get("Amount"))
    except (KeyError, TypeError, InvalidOperation):
        v.append("Amount is missing or malformed.")
        return v

    if currency != ctx["asset_currency"] or (currency != "XRP" and issuer != ctx["asset_issuer"]):
        v.append("Payment asset is not the approved treasury asset.")

    if value <= 0:
        v.append("Amount must be positive.")

    per_tx_max = Decimal(str(policy.get("per_tx_max", "0")))
    if value > per_tx_max:
        v.append(f"Amount {value} exceeds the per-payment limit of {per_tx_max}.")

    daily_max = Decimal(str(policy.get("daily_max", "0")))
    spent = Decimal(str(ctx.get("spent_today", 0)))
    if spent + value > daily_max:
        v.append(
            f"Daily limit exceeded: {spent} already spent today + {value} > {daily_max}."
        )

    return v
