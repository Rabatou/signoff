"""SignOff MCP server: plug ANY MCP-capable AI into the business's accounts payable.

Claude Desktop, Claude Code, Cursor or any MCP client becomes the AP agent.
It gets the same four tools as agent.py, plus read-only views of the CFO's
policy and the audit journal. It still holds only the agent key (weight 1 of
quorum 2): every payment must be co-signed by the independent policy service,
so no prompt, model or jailbreak can push it past the CFO's limits.

This process needs AGENT_SEED and POLICY_SERVICE_URL only. It never gets the
policy key, the operator key or the admin token, and has no tool to change limits.

Run (stdio):  python mcp_server.py
Claude Code:  claude mcp add signoff -- python /path/to/signoff/mcp_server.py
"""
from __future__ import annotations

import functools
from typing import Any

import anyio
import httpx

try:  # MCP Python SDK v2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK v1
    from mcp.server.fastmcp import FastMCP as _Server
from mcp.types import ToolAnnotations

from agent import SYSTEM, TASK, Tools
from common import need

mcp = _Server(
    name="signoff",
    instructions=SYSTEM,
)


@functools.cache
def tools() -> Tools:
    return Tools()


async def _run(fn, *args):
    # The XRPL sync client calls asyncio.run() internally, so it must not run
    # inside the MCP server's event loop: use a worker thread.
    return await anyio.to_thread.run_sync(fn, *args)


READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


@mcp.tool(annotations=READ)
async def list_pending_invoices() -> list[dict[str, Any]]:
    """List supplier invoices waiting for a decision (pending or previously blocked by policy)."""
    return await _run(lambda: tools().list_pending_invoices())


@mcp.tool(annotations=READ)
async def score_invoice(invoice_id: str) -> dict[str, Any]:
    """Explainable fraud / anomaly risk score of an invoice (BEC address change, amount
    z-score vs supplier history, duplicates, pressure wording). Always call it before paying."""
    return await _run(tools().score_invoice, invoice_id)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True,
                                      idempotent_hint=True, open_world_hint=True))
async def pay_invoice(invoice_id: str, justification: str) -> dict[str, Any]:
    """Pay an invoice on the XRP Ledger. Signs with the agent key and sends it to the
    independent policy co-signer, which enforces the CFO's limits before co-signing.
    If it is rejected, do not try to work around it: report the reasons to the CFO."""
    return await _run(tools().pay_invoice, invoice_id, justification)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False,
                                      idempotent_hint=True, open_world_hint=False))
async def flag_invoice(invoice_id: str, reason: str) -> dict[str, Any]:
    """Hold an invoice for human (CFO) review, with the reason."""
    return await _run(tools().flag_invoice, invoice_id, reason)


def _get(path: str):
    url = f"{need('POLICY_SERVICE_URL')}{path}"
    try:
        r = httpx.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        return {"error": f"Policy co-signer unreachable at {url}: {exc}"}


@mcp.tool(annotations=READ)
async def get_spending_policy() -> Any:
    """The CFO's current limits (per-payment cap, daily cap, approved suppliers, pause).
    Read-only: only the CFO can change them, from the console."""
    return await _run(_get, "/policy")


@mcp.tool(annotations=READ)
async def get_audit_journal(limit: int = 20) -> Any:
    """Latest co-signer decisions (approved / rejected, reasons, tx hash, explorer link)."""
    return await _run(_get, f"/journal?limit={max(1, min(limit, 100))}")


@mcp.prompt()
def process_invoices() -> str:
    """Process today's supplier invoices as the SignOff AP agent."""
    return f"{SYSTEM}\n\n{TASK}"


if __name__ == "__main__":
    mcp.run()
