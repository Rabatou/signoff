"""Bonus: a funded XRPL Payment Channel the agent cannot overdraw.

Use case: pay-as-you-go supplier (e.g. a logistics partner paid per delivery).
  1. The OPERATOR opens a channel from the treasury to the supplier, funded
     with a budget, and sets the AGENT's public key as the claim signer.
  2. The agent signs off-ledger claims (instant, free) as deliveries happen.
  3. The supplier redeems the latest claim on-ledger.
  4. A claim above the funded budget is refused by the ledger itself.
  5. The operator tops up the channel; the same claim now goes through.

The channel is XRP-only (payment channels hold XRP).
Run: python channel_demo.py
"""
from __future__ import annotations

from xrpl.core.binarycodec import encode_for_signing_claim
from xrpl.core.keypairs import sign as keypair_sign
from xrpl.models.requests import AccountChannels
from xrpl.models.transactions import PaymentChannelClaim, PaymentChannelCreate, PaymentChannelFund
from xrpl.transaction import submit_and_wait
from xrpl.utils import xrp_to_drops
from xrpl.wallet import Wallet

from common import SOURCE_TAG, get_client, need, operator_submit, tx_link

BUDGET_XRP = 10
TOP_UP_XRP = 5

client = get_client()
agent = Wallet.from_seed(need("AGENT_SEED"))
supplier = Wallet.from_seed(need("SUPPLIER2_SEED"))
treasury = need("TREASURY_ADDRESS")


def result(resp) -> str:
    return f"{resp.result['meta']['TransactionResult']}  {tx_link(resp.result['hash'])}"


def agent_claim(channel_id: str, total_xrp: float) -> tuple[str, str]:
    """The agent authorises a cumulative amount, off-ledger."""
    drops = xrp_to_drops(total_xrp)
    message = encode_for_signing_claim({"channel": channel_id, "amount": drops})
    return drops, keypair_sign(bytes.fromhex(message), agent.private_key)


def redeem(channel_id: str, drops: str, signature: str):
    tx = PaymentChannelClaim(account=supplier.address, channel=channel_id, balance=drops,
                             amount=drops, signature=signature, public_key=agent.public_key)
    return submit_and_wait(tx, client, supplier)


print(f"1. Operator opens a {BUDGET_XRP} XRP channel treasury -> supplier, signer = agent key")
create = PaymentChannelCreate(account=treasury, destination=supplier.address,
                              amount=xrp_to_drops(BUDGET_XRP), settle_delay=3600,
                              public_key=agent.public_key, source_tag=SOURCE_TAG)
print("   ", result(operator_submit(create, client)))

channels = client.request(AccountChannels(account=treasury,
                                          destination_account=supplier.address)).result["channels"]
channel = [c for c in channels if c.get("public_key_hex", "").upper() == agent.public_key.upper()][-1]
cid = channel["channel_id"]
print(f"    channel {cid}")

print("\n2. Agent streams claims for 3 deliveries (off-ledger, instant)")
for n, total in enumerate((2, 4, 6), 1):
    drops, sig = agent_claim(cid, total)
    print(f"    delivery {n}: claim for cumulative {total} XRP signed")

print("\n3. Supplier redeems the latest claim (6 XRP)")
print("   ", result(redeem(cid, drops, sig)))

print(f"\n4. Agent signs a claim for 12 XRP, above the {BUDGET_XRP} XRP budget")
drops12, sig12 = agent_claim(cid, 12)
try:
    print("   ", result(redeem(cid, drops12, sig12)), " <- refused by the ledger")
except Exception as exc:
    print(f"    refused: {exc}")

print(f"\n5. Operator tops up the channel by {TOP_UP_XRP} XRP, supplier retries")
fund = PaymentChannelFund(account=treasury, channel=cid, amount=xrp_to_drops(TOP_UP_XRP))
print("   ", result(operator_submit(fund, client)))
print("   ", result(redeem(cid, drops12, sig12)))
