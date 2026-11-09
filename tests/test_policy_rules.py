import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from policy_rules import TF_PARTIAL_PAYMENT, currency_code, evaluate, invoice_hash  # noqa: E402

TREASURY, AGENT, SUP, ISSUER = "rTreasury", "rAgent", "rSupplier", "rIssuer"
POLICY = {"paused": False, "per_tx_max": "2000", "daily_max": "5000",
          "require_memo": True, "allowlist": {SUP: "Supplier"}}
CTX = {"treasury": TREASURY, "agent_address": AGENT, "asset_currency": "USD",
       "asset_issuer": ISSUER, "spent_today": Decimal(0),
       "invoice_hash": invoice_hash("INV-1001"), "invoice_already_paid": False}


def tx(**over):
    base = {
        "TransactionType": "Payment", "Account": TREASURY, "Destination": SUP,
        "Amount": {"currency": "USD", "issuer": ISSUER, "value": "1250"},
        "Flags": 0, "Fee": "30", "InvoiceID": invoice_hash("INV-1001"), "LastLedgerSequence": 123, "Memos": [{"Memo": {"MemoData": "AB"}}],
        "Signers": [{"Signer": {"Account": AGENT, "SigningPubKey": "ED..", "TxnSignature": ".."}}],
    }
    base.update(over)
    return base


def test_valid_payment_is_approved():
    assert evaluate(tx(), POLICY, CTX) == []


def test_per_tx_limit():
    v = evaluate(tx(Amount={"currency": "USD", "issuer": ISSUER, "value": "4800"}), POLICY, CTX)
    assert any("per-payment limit" in x for x in v)


def test_daily_limit():
    v = evaluate(tx(), POLICY, CTX | {"spent_today": Decimal("4000")})
    assert any("Daily limit" in x for x in v)


def test_unknown_destination():
    assert any("not an approved supplier" in x for x in evaluate(tx(Destination="rFraud"), POLICY, CTX))


def test_paused():
    assert any("paused" in x for x in evaluate(tx(), POLICY | {"paused": True}, CTX))


def test_wrong_asset_issuer():
    v = evaluate(tx(Amount={"currency": "USD", "issuer": "rFake", "value": "10"}), POLICY, CTX)
    assert any("asset" in x for x in v)


def test_partial_payment_and_sendmax_refused():
    v = evaluate(tx(Flags=TF_PARTIAL_PAYMENT, SendMax="1000000"), POLICY, CTX)
    assert any("Partial" in x for x in v) and any("SendMax" in x for x in v)


def test_must_be_signed_by_agent_only():
    two = tx(Signers=[{"Signer": {"Account": AGENT}}, {"Signer": {"Account": "rOther"}}])
    assert any("exactly one signature" in x for x in evaluate(two, POLICY, CTX))
    other = tx(Signers=[{"Signer": {"Account": "rOther"}}])
    assert any("exactly one signature" in x for x in evaluate(other, POLICY, CTX))


def test_other_tx_types_refused():
    v = evaluate(tx(TransactionType="SignerListSet"), POLICY, CTX)
    assert any("Only Payment" in x for x in v)


def test_memo_required():
    t = tx()
    del t["Memos"]
    assert any("memo" in x for x in evaluate(t, POLICY, CTX))


def test_xrp_amount_in_drops():
    ctx = CTX | {"asset_currency": "XRP", "asset_issuer": None}
    assert evaluate(tx(Amount="1000000000"), POLICY, ctx) == []        # 1000 XRP
    assert evaluate(tx(Amount="3000000000"), POLICY, ctx) != []        # 3000 XRP


def test_currency_code():
    assert currency_code("USD") == "USD"
    assert currency_code("RLUSD") == "524C555344000000000000000000000000000000"


def test_fee_cap():
    assert any("Fee" in x for x in evaluate(tx(Fee="5000000"), POLICY, CTX))
    assert any("Fee" in x for x in evaluate(tx(Fee=None), POLICY, CTX))
    assert evaluate(tx(Fee="5000"), POLICY | {"max_fee_drops": 10000}, CTX) == []


def test_invoice_binding():
    assert any("InvoiceID" in x for x in evaluate(tx(InvoiceID=invoice_hash("INV-9")), POLICY, CTX))
    t = tx()
    del t["InvoiceID"]
    assert any("InvoiceID" in x for x in evaluate(t, POLICY, CTX))


def test_invoice_paid_only_once():
    v = evaluate(tx(), POLICY, CTX | {"invoice_already_paid": True})
    assert any("already paid" in x for x in v)
