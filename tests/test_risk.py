import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from risk import score_invoice  # noqa: E402

REGISTRY = {"Shenzhen Textile Co.": {"address": "rSZ", "country": "CN"},
            "Lagos Packaging Ltd": {"address": "rLA", "country": "NG"}}
HISTORY = ([{"supplier": "Shenzhen Textile Co.", "date": "2026-01-01", "amount": a}
            for a in (1100, 1200, 1300, 1150, 1250, 1180)]
           + [{"supplier": "Lagos Packaging Ltd", "date": "2026-01-01", "amount": a}
              for a in (600, 650, 700, 620, 680)])

INV = [
    {"id": "INV-1", "supplier": "Shenzhen Textile Co.", "supplier_ref": "A1", "date": "2026-11-05",
     "amount": 1250, "pay_to": "rSZ", "description": "fabric"},
    {"id": "INV-2", "supplier": "Shenzhen Textile Co.", "supplier_ref": "A1", "date": "2026-11-08",
     "amount": 1250, "pay_to": "rSZ", "description": "fabric"},
    {"id": "INV-3", "supplier": "Lagos Packaging Ltd", "supplier_ref": "B7", "date": "2026-11-09",
     "amount": 900, "pay_to": "rEVIL", "description": "URGENT: bank details changed"},
    {"id": "INV-4", "supplier": "Lagos Packaging Ltd", "supplier_ref": "B8", "date": "2026-11-09",
     "amount": 9000, "pay_to": "rLA", "description": "boxes"},
]


def s(i):
    return score_invoice(INV[i], INV, HISTORY, REGISTRY)


def test_normal_invoice_is_low_risk():
    assert s(0)["risk_level"] == "low"


def test_duplicate_detected_only_for_the_later_invoice():
    assert s(1)["features"]["duplicate_of"] == "INV-1"
    assert s(1)["risk_level"] != "low"
    assert s(0)["features"]["duplicate_of"] is None


def test_bank_detail_change_is_high_risk():
    r = s(2)
    assert r["risk_level"] == "high"
    assert r["features"]["registered_address_match"] is False


def test_amount_anomaly():
    r = s(3)
    assert r["features"]["z_score"] > 3 and r["risk_level"] != "low"


def test_missing_supplier_ref_is_not_a_duplicate():
    a = {"id": "X-1", "supplier": "Shenzhen Textile Co.", "date": "2026-10-01",
         "amount": 1200, "pay_to": "rSZ", "description": "fabric"}
    b = a | {"id": "X-2", "date": "2026-11-05", "amount": 1210}
    assert score_invoice(b, [a, b], HISTORY, REGISTRY)["features"]["duplicate_of"] is None
