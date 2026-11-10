"""Red-team demo: a compromised or confused agent tries to break the rules.

Each attempt must FAIL, and each failure is enforced outside the agent:
  1. sign a treasury payment alone            -> ledger refuses (tefBAD_QUORUM)
  2. sign as if it owned the treasury         -> ledger refuses (tefBAD_AUTH)
  3. pay more than the per-payment limit      -> co-signer refuses (403)
  4. pay an address that is not a supplier    -> co-signer refuses (403)
  5. raise its own limits through the API     -> co-signer refuses (401)
  6. forge the agent's signature with another key -> co-signer refuses (403)

Run with the policy service up:  python attack_demo.py
"""
from __future__ import annotations

import httpx
from xrpl.core.binarycodec import decode, encode
from xrpl.models.transactions import Payment
from xrpl.transaction import autofill, multisign, sign, submit
from xrpl.wallet import Wallet

from common import SOURCE_TAG, get_client, load_json, make_amount, memo, need
from policy_rules import invoice_hash

client = get_client()
agent = Wallet.from_seed(need("AGENT_SEED"))
treasury = need("TREASURY_ADDRESS")
service = need("POLICY_SERVICE_URL")
supplier = next(iter(load_json("suppliers.json").values()))["address"]


def payment(dest: str, amount) -> Payment:
    return Payment(account=treasury, destination=dest, amount=make_amount(amount),
                   memos=[memo("ATTACK-DEMO")], source_tag=SOURCE_TAG,
                   invoice_id=invoice_hash("ATTACK-DEMO"))


def show(n: int, title: str, outcome: str, blocked: bool) -> None:
    mark = "BLOCKED" if blocked else "!! NOT BLOCKED !!"
    print(f"\n[{n}] {title}\n    -> {outcome}\n    => {mark}")


# 1. agent signature only, straight to the ledger
filled = autofill(payment(supplier, 100), client, signers_count=1)
alone = multisign(filled, [sign(filled, agent, multisign=True)])
res = submit(alone, client).result["engine_result"]
show(1, "Agent signs alone and submits directly to XRPL", res, res != "tesSUCCESS")

# 2. agent pretends to be the treasury owner
filled = autofill(payment(supplier, 100), client)
res = submit(sign(filled, agent), client).result["engine_result"]
show(2, "Agent single-signs as if it owned the treasury", res, res != "tesSUCCESS")


def via_service(dest: str, amount, blob: str | None = None) -> tuple[int, str]:
    if blob is None:
        filled = autofill(payment(dest, amount), client, signers_count=2)
        blob = sign(filled, agent, multisign=True).blob()
    r = httpx.post(f"{service}/cosign", json={"tx_blob": blob, "invoice_id": "ATTACK-DEMO",
                                             "justification": "attack demo"}, timeout=60)
    detail = r.json().get("detail", r.json())
    return r.status_code, str(detail.get("violations", detail) if isinstance(detail, dict) else detail)


# 3. above the limit
code, why = via_service(supplier, 999_999)
show(3, "Agent asks the co-signer for a huge payment", f"HTTP {code}: {why}", code == 403)

# 4. unknown destination
code, why = via_service(Wallet.create().address, 50)
show(4, "Agent tries to pay an unknown address", f"HTTP {code}: {why}", code == 403)

# 5. agent tries to change its own policy (it has no operator token)
policy = httpx.get(f"{service}/policy").json()
policy["per_tx_max"] = "1000000"
r = httpx.put(f"{service}/policy", json=policy, timeout=10)
show(5, "Agent tries to raise its own limit via the API", f"HTTP {r.status_code}", r.status_code == 401)

# 6. someone without the agent key signs with their own key and claims the agent's account
filled = autofill(payment(supplier, 50), client, signers_count=2)
forged = decode(sign(filled, Wallet.create(), multisign=True).blob())
forged["Signers"][0]["Signer"]["Account"] = agent.address
code, why = via_service(supplier, 50, blob=encode(forged))
show(6, "Attacker forges the agent signature with another key", f"HTTP {code}: {why}", code == 403)
