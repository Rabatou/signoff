"""SignOff accounts-payable agent.

Reads pending supplier invoices, scores their risk, and pays the safe ones
from the business treasury on XRPL. It holds ONE key of the treasury's
multisig (weight 1 of quorum 2): every payment must also be co-signed by the
independent policy service, which enforces the CFO's limits.

  python agent.py               # auto: Anthropic, OpenAI-compatible, or rules
  python agent.py --provider rules
  python agent.py --reset       # restore the demo invoices
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import uuid

import httpx
from xrpl.transaction import autofill, sign
from xrpl.models.transactions import Payment
from xrpl.wallet import Wallet

from common import (
    DATA,
    SOURCE_TAG,
    agent_memo,
    asset_label,
    get_client,
    load_json,
    make_amount,
    need,
    save_json,
)
from policy_rules import invoice_hash
from risk import score_invoice

SYSTEM = """You are SignOff, the accounts-payable agent of a small import business in Dakar.
Your job: process today's supplier invoices and pay the legitimate ones from the treasury.

Rules you follow:
- Always call score_invoice before deciding on an invoice.
- Pay only invoices with risk_level "low". Flag medium and high risk invoices with a clear reason.
- Never split an invoice, change an amount, or change a destination.
- Payments are co-signed by an independent policy service that enforces the CFO's limits.
  If it rejects a payment, do not try to work around it: report the reasons for the CFO.
- Finish with a short summary for the CFO: what was paid (with explorer links), what was held and why."""

TASK = "Process all invoices waiting for payment today."

TOOLS = [
    {"name": "list_pending_invoices",
     "description": "List invoices waiting for a decision (pending or previously blocked by policy).",
     "parameters": {"type": "object", "properties": {}, "required": []}},
    {"name": "score_invoice",
     "description": "Fraud / anomaly risk score of an invoice, with explanations.",
     "parameters": {"type": "object", "properties": {"invoice_id": {"type": "string"}},
                    "required": ["invoice_id"]}},
    {"name": "pay_invoice",
     "description": "Sign the payment for an invoice and send it to the policy co-signer, "
                    "which checks the CFO's limits and submits it to XRPL.",
     "parameters": {"type": "object",
                    "properties": {"invoice_id": {"type": "string"},
                                   "justification": {"type": "string"}},
                    "required": ["invoice_id", "justification"]}},
    {"name": "flag_invoice",
     "description": "Hold an invoice for human review.",
     "parameters": {"type": "object",
                    "properties": {"invoice_id": {"type": "string"},
                                   "reason": {"type": "string"}},
                    "required": ["invoice_id", "reason"]}},
]
TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


class Tools:
    def __init__(self) -> None:
        self.client = get_client()
        self.agent = Wallet.from_seed(need("AGENT_SEED"))
        self.treasury = need("TREASURY_ADDRESS")
        self.service = need("POLICY_SERVICE_URL")
        self.session_id = uuid.uuid4().hex[:12]  # ties every on-chain memo to this run

    # -- data helpers
    def _invoices(self) -> list[dict]:
        return load_json("invoices.json", [])

    def _save(self, invoices: list[dict]) -> None:
        save_json("invoices.json", invoices)

    def _get(self, invoice_id: str) -> dict | None:
        return next((i for i in self._invoices() if i["id"] == invoice_id), None)

    def _set_status(self, invoice_id: str, **fields) -> None:
        invoices = self._invoices()
        for inv in invoices:
            if inv["id"] == invoice_id:
                inv.update(fields)
        self._save(invoices)

    # -- tools
    def list_pending_invoices(self) -> list[dict]:
        return [
            {k: i[k] for k in ("id", "supplier", "date", "amount", "pay_to", "description", "status")}
            | {"currency": asset_label()}
            for i in self._invoices() if i["status"] in ("pending", "blocked_by_policy")
        ]

    def score_invoice(self, invoice_id: str) -> dict:
        inv = self._get(invoice_id)
        if inv is None:
            return {"error": f"Unknown invoice {invoice_id}"}
        return score_invoice(inv, self._invoices(), load_json("history.json", []),
                             load_json("suppliers.json", {}))

    def pay_invoice(self, invoice_id: str, justification: str) -> dict:
        inv = self._get(invoice_id)
        if inv is None:
            return {"error": f"Unknown invoice {invoice_id}"}
        if inv["status"] not in ("pending", "blocked_by_policy", "approved_by_cfo"):
            return {"error": f"Invoice {invoice_id} is already {inv['status']}."}

        tx = Payment(
            account=self.treasury,
            destination=inv["pay_to"],
            amount=make_amount(inv["amount"]),
            invoice_id=invoice_hash(inv["id"]),  # one invoice = one payment, on-chain
            memos=[agent_memo("pay_invoice", inv["id"], self.session_id,
                              supplier_ref=inv.get("supplier_ref"))],
            source_tag=SOURCE_TAG,
        )
        # Fee is computed for 2 signers: agent + policy co-signer.
        filled = autofill(tx, self.client, signers_count=2)
        signed = sign(filled, self.agent, multisign=True)

        r = httpx.post(f"{self.service}/cosign", timeout=60, json={
            "tx_blob": signed.blob(), "invoice_id": invoice_id, "justification": justification})
        try:
            body = r.json()
        except ValueError:
            return {"result": "ERROR", "http_status": r.status_code, "detail": r.text[:300]}
        if r.status_code == 200 and body.get("ledger_result") == "tesSUCCESS":
            self._set_status(invoice_id, status="paid", tx_hash=body["tx_hash"])
            return {"result": "PAID", "tx_hash": body["tx_hash"], "explorer": body["explorer"]}
        if r.status_code == 403 and isinstance(body.get("detail"), dict):
            reasons = body["detail"].get("violations", [])
            self._set_status(invoice_id, status="blocked_by_policy", policy_reasons=reasons)
            return {"result": "REJECTED_BY_POLICY", "violations": reasons}
        return {"result": "ERROR", "http_status": r.status_code, "detail": body}

    def flag_invoice(self, invoice_id: str, reason: str) -> dict:
        if self._get(invoice_id) is None:
            return {"error": f"Unknown invoice {invoice_id}"}
        self._set_status(invoice_id, status="flagged", flag_reason=reason)
        return {"result": "FLAGGED", "invoice_id": invoice_id}

    def dispatch(self, name: str, args: dict):
        print(f"  -> {name}({json.dumps(args, ensure_ascii=False)})")
        if name not in TOOL_NAMES:  # never let the model call internal helpers
            out = {"error": f"Unknown tool {name}"}
            print(f"  <- {json.dumps(out)}")
            return out
        try:
            out = getattr(self, name)(**args)
        except Exception as exc:  # report errors to the model instead of crashing
            out = {"error": f"{type(exc).__name__}: {exc}"}
        print(f"  <- {json.dumps(out, ensure_ascii=False)[:400]}")
        return out


# ------------------------------------------------------------------ runners
def run_rules(tools: Tools) -> None:
    """Deterministic fallback agent (no LLM needed)."""
    for inv in tools.list_pending_invoices():
        print(f"\n[{inv['id']}] {inv['supplier']} — {inv['amount']} {inv['currency']}")
        risk = tools.dispatch("score_invoice", {"invoice_id": inv["id"]})
        if risk.get("risk_level") == "low":
            tools.dispatch("pay_invoice", {"invoice_id": inv["id"],
                                           "justification": f"Low risk ({risk['risk_score']}/100)."})
        else:
            tools.dispatch("flag_invoice", {"invoice_id": inv["id"],
                                            "reason": " ".join(risk.get("reasons", []))})


def run_anthropic(tools: Tools, model: str, max_steps: int) -> None:
    import anthropic

    client = anthropic.Anthropic()
    specs = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
             for t in TOOLS]
    messages = [{"role": "user", "content": TASK}]
    for _ in range(max_steps):
        resp = client.messages.create(model=model, max_tokens=2048, system=SYSTEM,
                                      tools=specs, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                print(f"\n{block.text}")
        if resp.stop_reason != "tool_use":
            return
        results = [
            {"type": "tool_result", "tool_use_id": b.id,
             "content": json.dumps(tools.dispatch(b.name, b.input))}
            for b in resp.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})


def run_openai(tools: Tools, model: str, max_steps: int) -> None:
    """Works with OpenAI and any OpenAI-compatible endpoint (set OPENAI_BASE_URL),
    e.g. Groq, Gemini's OpenAI endpoint, or a local Ollama server."""
    from openai import OpenAI

    client = OpenAI()
    specs = [{"type": "function", "function": t} for t in TOOLS]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": TASK}]
    for _ in range(max_steps):
        resp = client.chat.completions.create(model=model, messages=messages, tools=specs)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if msg.content:
            print(f"\n{msg.content}")
        if not msg.tool_calls:
            return
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            out = tools.dispatch(call.function.name, args)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(out)})


def reset_demo() -> None:
    """Demo convenience, run by the presenter on the demo machine (not an agent tool).

    The co-signer refuses to pay an invoice twice, so replaying the demo needs a
    fresh journal too. The old journal is archived, never deleted.
    """
    from datetime import datetime

    shutil.copy(DATA / "invoices.template.json", DATA / "invoices.json")
    journal = DATA / "journal.db"
    if journal.exists():
        archive = DATA / f"journal-{datetime.now():%Y%m%d-%H%M%S}.db"
        journal.rename(archive)
        print(f"Journal archived to {archive.name}.")
    policy = load_json("policy.json", {})
    suppliers = load_json("suppliers.json", {})
    policy.update(paused=False, per_tx_max="2000", daily_max="8000", updated_by="demo reset",
                  allowlist={info["address"]: name for name, info in suppliers.items()})
    policy.pop("updated_at", None)
    save_json("policy.json", policy)
    print("Demo reset: invoices pending, limits 2000 / 8000, empty journal.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default=os.getenv("LLM_PROVIDER", "auto"),
                    choices=["auto", "anthropic", "openai", "rules"])
    ap.add_argument("--model", default=os.getenv("LLM_MODEL"))
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--pay", metavar="INVOICE_ID",
                    help="pay one invoice the CFO approved in the console, print the result as JSON")
    ap.add_argument("--reset", action="store_true",
                    help="demo only: restore invoices, demo limits and an empty journal, then exit")
    args = ap.parse_args()

    if args.reset:
        reset_demo()
        return

    if args.pay:
        # Still signed by the agent key only: the co-signer applies every limit.
        out = Tools().pay_invoice(args.pay, "Approved by the CFO in the console.")
        print(json.dumps(out))
        return

    provider = args.provider
    if provider == "auto":
        provider = ("anthropic" if os.getenv("ANTHROPIC_API_KEY")
                    else "openai" if os.getenv("OPENAI_API_KEY") else "rules")
    print(f"SignOff agent — provider: {provider}")

    tools = Tools()
    if provider == "rules":
        run_rules(tools)
    elif provider == "anthropic":
        run_anthropic(tools, args.model or "claude-sonnet-5-5", args.max_steps)
    else:
        run_openai(tools, args.model or "gpt-4.1-mini", args.max_steps)


if __name__ == "__main__":
    main()
