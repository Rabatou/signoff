"""Invoice risk scoring: the agent's data-science layer.

Transparent, explainable features (no black box), so a CFO can read why an
invoice was held:
  * supplier unknown / pay-to address differs from the registered one (BEC fraud)
  * amount anomaly vs the supplier's history (z-score)
  * duplicate invoice (same supplier reference, or same amount within 30 days)
  * suspicious wording ("urgent", "bank details changed", ...)
"""
from __future__ import annotations

from datetime import date
from statistics import mean, stdev

URGENCY_WORDS = ("urgent", "bank details", "new account", "changed", "immediately")


def _days_between(a: str, b: str) -> int:
    return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)


def score_invoice(inv: dict, invoices: list[dict], history: list[dict], registry: dict) -> dict:
    score = 0
    reasons: list[str] = []
    features: dict = {}

    supplier = registry.get(inv["supplier"])
    if supplier is None:
        score += 60
        reasons.append("Supplier is not in the vendor registry.")
    elif inv["pay_to"] != supplier["address"]:
        score += 70
        reasons.append(
            "Pay-to address differs from the supplier's registered address "
            "(classic business-email-compromise pattern)."
        )
    features["registered_address_match"] = bool(supplier) and inv["pay_to"] == supplier["address"]

    past = [h["amount"] for h in history if h["supplier"] == inv["supplier"]]
    if len(past) >= 3:
        mu, sd = mean(past), stdev(past)
        z = (inv["amount"] - mu) / sd if sd > 0 else 0.0
        features.update(history_mean=round(mu, 2), history_std=round(sd, 2), z_score=round(z, 2))
        if abs(z) > 3:
            score += 40
            reasons.append(f"Amount is {z:+.1f} standard deviations from this supplier's usual invoices.")
        elif abs(z) > 2:
            score += 20
            reasons.append(f"Amount is unusual for this supplier (z = {z:+.1f}).")
    else:
        score += 15
        reasons.append("Not enough payment history with this supplier.")

    earlier = [
        o for o in invoices
        if o["id"] != inv["id"] and o["supplier"] == inv["supplier"]
        and (o["date"], o["id"]) < (inv["date"], inv["id"])
    ]
    ref = inv.get("supplier_ref")
    dup = next((o for o in earlier if ref and o.get("supplier_ref") == ref), None)
    if dup is None:
        dup = next((o for o in earlier if o["amount"] == inv["amount"]
                    and _days_between(o["date"], inv["date"]) <= 30), None)
    if dup is not None:
        score += 50
        reasons.append(f"Possible duplicate of {dup['id']} (same supplier reference or amount).")
    features["duplicate_of"] = dup["id"] if dup else None

    text = inv.get("description", "").lower()
    hits = [w for w in URGENCY_WORDS if w in text]
    if hits:
        score += 15
        reasons.append(f"Pressure / change-of-details wording: {', '.join(hits)}.")

    score = min(score, 100)
    level = "low" if score < 30 else "medium" if score < 60 else "high"
    return {"invoice_id": inv["id"], "risk_score": score, "risk_level": level,
            "reasons": reasons or ["No risk signal found."], "features": features}
