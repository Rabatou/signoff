"""SignOff CFO console — the human operator sets the agent's limits here.

Run: streamlit run dashboard.py
Talks to the policy service with the operator token. The agent has neither.
"""
from __future__ import annotations

import os

import httpx
import streamlit as st

from common import account_link, asset_label, get_client, load_json, need, treasury_balance

SERVICE = need("POLICY_SERVICE_URL")
TOKEN = os.getenv("ADMIN_TOKEN", "")
TREASURY = need("TREASURY_ADDRESS")

st.set_page_config(page_title="SignOff — CFO console", layout="wide")
st.title("SignOff — CFO console")
st.caption("The AI agent pays suppliers. You set the rules. The XRP Ledger enforces them.")

try:
    policy = httpx.get(f"{SERVICE}/policy", timeout=10).json()
    journal = httpx.get(f"{SERVICE}/journal", timeout=10).json()
except httpx.HTTPError as exc:
    st.error(f"Policy service unreachable at {SERVICE}: {exc}")
    st.stop()

c1, c2, c3 = st.columns(3)
try:
    c1.metric("Treasury balance", treasury_balance(get_client()))
except Exception as exc:  # network hiccup should not kill the console
    c1.metric("Treasury balance", "unavailable")
    c1.caption(str(exc)[:120])
executed = [j for j in journal if j["status"] == "EXECUTED"]
rejected = [j for j in journal if j["decision"] == "REJECTED"]
c2.metric("Payments executed", len(executed))
c3.metric("Payments blocked by policy", len(rejected))
st.markdown(f"Treasury account: [`{TREASURY}`]({account_link(TREASURY)})")

left, right = st.columns([1, 2])

with left:
    st.subheader("Spending policy")
    registry = load_json("suppliers.json", {})
    with st.form("policy"):
        paused = st.toggle("Pause all agent payments", value=policy["paused"])
        per_tx = st.number_input(f"Max per payment ({asset_label()})",
                                 min_value=0.0, value=float(policy["per_tx_max"]), step=100.0)
        daily = st.number_input(f"Max per day ({asset_label()})",
                                min_value=0.0, value=float(policy["daily_max"]), step=500.0)
        require_memo = st.checkbox("Require invoice memo on every payment",
                                   value=policy.get("require_memo", True))
        st.markdown("**Approved suppliers**")
        allowlist = {}
        for name, info in registry.items():
            if st.checkbox(f"{name} ({info['country']})", value=info["address"] in policy["allowlist"],
                           key=info["address"]):
                allowlist[info["address"]] = name
        saved = st.form_submit_button("Save policy", type="primary")

    if saved:
        r = httpx.put(f"{SERVICE}/policy", timeout=10, headers={"x-admin-token": TOKEN}, json={
            "paused": paused, "per_tx_max": str(per_tx), "daily_max": str(daily),
            "allowlist": allowlist, "require_memo": require_memo,
            "max_fee_drops": int(policy.get("max_fee_drops", 1000))})
        if r.status_code == 200:
            st.success("Policy updated. The co-signer applies it to the next payment.")
            st.rerun()
        else:
            st.error(f"Update refused ({r.status_code}): {r.text}")
    if policy.get("updated_at"):
        st.caption(f"Last change by {policy.get('updated_by')} at {policy['updated_at']} UTC")

with right:
    st.subheader("Agent activity (audit journal)")
    if not journal:
        st.info("No payment attempt yet. Run `python agent.py`.")
    else:
        rows = [{
            "time (UTC)": j["ts"], "invoice": j["invoice_id"], "amount": j["amount"],
            "decision": j["decision"], "status": j["status"],
            "why": "; ".join(j["reasons"]) if j["reasons"] else (j["justification"] or ""),
            "ledger": j["ledger_result"], "explorer": j["explorer"],
        } for j in journal]
        st.dataframe(rows, use_container_width=True, hide_index=True, column_config={
            "explorer": st.column_config.LinkColumn("explorer", display_text="view tx")})

    st.subheader("Invoices")
    invoices = load_json("invoices.json", [])
    st.dataframe([{
        "invoice": i["id"], "supplier": i["supplier"], "amount": i["amount"],
        "status": i["status"],
        "note": i.get("flag_reason") or "; ".join(i.get("policy_reasons", [])),
    } for i in invoices], use_container_width=True, hide_index=True)
