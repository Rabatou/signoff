"""The co-signer must check the agent's signature cryptographically, not just the Account field."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xrpl.core.binarycodec import decode  # noqa: E402
from xrpl.models.amounts import IssuedCurrencyAmount  # noqa: E402
from xrpl.models.transactions import Payment  # noqa: E402
from xrpl.transaction import sign  # noqa: E402
from xrpl.wallet import Wallet  # noqa: E402

from common import verify_multisig_signer  # noqa: E402

AGENT = Wallet.from_seed("sEdTM1uX8pu2do5XvTnutH6HsouMaM2")  # throwaway test key
TREASURY = Wallet.from_seed("sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r").address


def signed_by(wallet: Wallet) -> dict:
    tx = Payment(account=TREASURY, destination=AGENT.address, fee="30", sequence=1,
                 last_ledger_sequence=10, source_tag=1,
                 amount=IssuedCurrencyAmount(currency="USD", issuer=TREASURY, value="1"))
    return decode(sign(tx, wallet, multisign=True).blob())


def test_genuine_agent_signature_is_accepted():
    assert verify_multisig_signer(signed_by(AGENT), AGENT.address)


def test_signature_from_another_key_claiming_the_agent_account_is_refused():
    forged = signed_by(Wallet.create())
    forged["Signers"][0]["Signer"]["Account"] = AGENT.address
    assert not verify_multisig_signer(forged, AGENT.address)


def test_tampered_fields_invalidate_the_signature():
    tx = signed_by(AGENT)
    tx["Amount"]["value"] = "999999"
    assert not verify_multisig_signer(tx, AGENT.address)


def test_two_signers_or_none_are_refused():
    tx = signed_by(AGENT)
    assert not verify_multisig_signer(tx | {"Signers": []}, AGENT.address)
    assert not verify_multisig_signer(tx | {"Signers": tx["Signers"] * 2}, AGENT.address)
