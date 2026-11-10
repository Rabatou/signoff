"""Shared configuration and XRPL helpers."""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from policy_rules import currency_code

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ENV_FILE = ROOT / ".env"
load_dotenv(ENV_FILE)

NETWORKS = {
    "testnet": "https://s.altnet.rippletest.net:51234/",
    "devnet": "https://s.devnet.rippletest.net:51234/",
}
EXPLORERS = {
    "testnet": "https://testnet.xrpl.org/transactions/",
    "devnet": "https://devnet.xrpl.org/transactions/",
}
# Official XRPL agent SourceTag (xrpl.org/docs/agents/track-agent-behavior):
# it marks every SignOff transaction as agent-initiated on the public ledger.
SOURCE_TAG = 20260530
AGENT_ID = "signoff-ap-agent-v1"
RLUSD_TESTNET_ISSUER = "rQhWct2fv4Vc4KRjRgMrxa8xPN9Zx9iLKV"


def network() -> str:
    return os.getenv("XRPL_NETWORK", "testnet")


def rpc_url() -> str:
    return os.getenv("XRPL_RPC_URL") or NETWORKS[network()]


def get_client():
    from xrpl.clients import JsonRpcClient

    return JsonRpcClient(rpc_url())


def tx_link(tx_hash: str) -> str:
    return EXPLORERS.get(network(), EXPLORERS["testnet"]) + tx_hash


def account_link(address: str) -> str:
    return EXPLORERS.get(network(), EXPLORERS["testnet"]).replace("/transactions/", "/accounts/") + address


def need(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing {name} in .env — run `python setup.py` first.")
    return value


def asset_currency() -> str:
    return currency_code(os.getenv("ASSET_CURRENCY", "USD"))


def asset_issuer() -> str | None:
    return None if asset_currency() == "XRP" else need("ASSET_ISSUER")


def asset_label() -> str:
    return os.getenv("ASSET_CURRENCY", "USD")


def make_amount(value):
    """Build an XRPL Amount in the configured treasury asset."""
    if asset_currency() == "XRP":
        from xrpl.utils import xrp_to_drops

        return xrp_to_drops(Decimal(str(value)))
    from xrpl.models.amounts import IssuedCurrencyAmount

    return IssuedCurrencyAmount(
        currency=asset_currency(), issuer=asset_issuer(), value=str(value)
    )


def memo(text: str, kind: str = "invoice"):
    from xrpl.models.transactions import Memo
    from xrpl.utils import str_to_hex

    return Memo(memo_data=str_to_hex(text), memo_type=str_to_hex(kind))


def agent_memo(action: str, task_id: str, session_id: str, **extra):
    """Structured audit memo, in the format recommended by xrpl.org for agents.

    Anyone can decode it from the public ledger and match it to the journal.
    """
    from xrpl.models.transactions import Memo
    from xrpl.utils import str_to_hex

    payload = {"agent_id": AGENT_ID, "session_id": session_id,
               "action": action, "task_id": task_id, **extra}
    return Memo(memo_data=str_to_hex(json.dumps(payload, separators=(",", ":"))),
                memo_type=str_to_hex("signoff/agent"),
                memo_format=str_to_hex("application/json"))


def verify_multisig_signer(decoded: dict, expected_address: str) -> bool:
    """True if the decoded tx carries exactly one valid multisig signature from expected_address.

    The Signers array is self-declared by whoever built the blob, so the
    co-signer must check the cryptography, not just the Account field.
    """
    from xrpl.core.binarycodec import encode_for_multisigning
    from xrpl.core.keypairs import derive_classic_address, is_valid_message

    signers = decoded.get("Signers") or []
    if len(signers) != 1:
        return False
    signer = signers[0].get("Signer", {})
    try:
        if signer.get("Account") != expected_address:
            return False
        if derive_classic_address(signer["SigningPubKey"]) != expected_address:
            return False
        message = encode_for_multisigning({k: v for k, v in decoded.items() if k != "Signers"},
                                          expected_address)
        return is_valid_message(bytes.fromhex(message), bytes.fromhex(signer["TxnSignature"]),
                                signer["SigningPubKey"])
    except Exception:
        return False


def load_json(name: str, default=None):
    path = DATA / name
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(name: str, obj) -> None:
    DATA.mkdir(exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def operator_submit(tx, client):
    """Submit a treasury transaction signed by the operator key alone.

    The operator key has weight 2 in the treasury SignerList, so it meets the
    quorum by itself. The agent key (weight 1) never can.
    """
    from xrpl.transaction import autofill, multisign, sign, submit_and_wait
    from xrpl.wallet import Wallet

    operator = Wallet.from_seed(need("OPERATOR_SEED"))
    filled = autofill(tx, client, signers_count=1)
    signed = sign(filled, operator, multisign=True)
    return submit_and_wait(multisign(filled, [signed]), client)


def treasury_balance(client) -> str:
    """Human-readable balance of the treasury in the configured asset."""
    from xrpl.models.requests import AccountInfo, AccountLines
    from xrpl.utils import drops_to_xrp

    treasury = need("TREASURY_ADDRESS")
    info = client.request(AccountInfo(account=treasury, ledger_index="validated")).result
    xrp = drops_to_xrp(info["account_data"]["Balance"])
    if asset_currency() == "XRP":
        return f"{xrp} XRP"
    lines = client.request(AccountLines(account=treasury, ledger_index="validated")).result
    for line in lines.get("lines", []):
        if line["account"] == asset_issuer() and currency_code(line["currency"]) == asset_currency():
            return f"{line['balance']} {asset_label()} (+ {xrp} XRP for fees)"
    return f"0 {asset_label()} (+ {xrp} XRP for fees)"
