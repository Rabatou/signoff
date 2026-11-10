"""SignOff CFO console: the business owner supervises the AP agent here.

Run: streamlit run dashboard.py
Talks to the policy co-signer with the operator token, which the agent never has.
The agent runs in its own process (agent.py) with its own key.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import streamlit as st

from common import (ROOT, account_link, asset_label, get_client, load_json, need, network, save_json,
                    treasury_balance, tx_link)
from risk import score_invoice

SERVICE = need("POLICY_SERVICE_URL")
TOKEN = os.getenv("ADMIN_TOKEN", "")
TREASURY = need("TREASURY_ADDRESS")
# Indicative rate (XOF is pegged to the EUR, the USD rate floats). Set XOF_PER_USD in .env.
XOF_PER_USD = float(os.getenv("XOF_PER_USD", "600"))
# Typical all-in cost of an international bank transfer from WAEMU (fees + FX spread).
BANK_FEE_PCT = float(os.getenv("BANK_FEE_PCT", "3.5"))

st.set_page_config(page_title="SignOff", page_icon="✅", layout="wide")

# ------------------------------------------------------------------ texts
T = {
    "fr": {
        "tagline": "Votre agent paie vos fournisseurs. Vous fixez les règles. Le XRP Ledger les fait respecter.",
        "lang": "Langue", "network": "Réseau", "treasury": "Compte trésorerie",
        "how_title": "Comment ça marche",
        "how": "1. **L'agent** lit les factures et détecte la fraude.\n"
               "2. **Le co-signataire** vérifie vos règles avant chaque paiement.\n"
               "3. **Le XRP Ledger** exige les deux signatures : l'agent seul ne peut rien payer.",
        "demo": "Outils de démo", "reset": "Réinitialiser la démo", "reset_done": "Démo réinitialisée.",
        "unreachable": "Le co-signataire est injoignable ({url}). Lancez : `uvicorn policy_service:app --port 8000`",
        "k_balance": "Solde de la trésorerie", "k_paid": "Payé par l'agent", "k_caught": "Fraudes et anomalies arrêtées",
        "k_saved": "Frais bancaires évités (est.)", "unavailable": "indisponible",
        "k_saved_help": "Estimation : un virement international depuis l'UEMOA coûte environ {pct} % (frais + change) "
                        "et prend 2 à 5 jours. Sur le XRP Ledger : une fraction de centime, en quelques secondes.",
        "k_payments": "{n} paiement(s)",
        "k_caught_help": "Factures suspectes signalées par l'agent ou rejetées par vous.",
        "run_title": "Factures du jour", "run_btn": "▶ Lancer l'agent",
        "run_help": "L'agent vérifie chaque facture en attente, paie celles sans risque et vous signale le reste.",
        "running": "L'agent traite les factures…", "run_done": "Terminé", "run_log": "Journal technique de l'agent",
        "run_summary": "{paid} payée(s) · {flagged} signalée(s) · {blocked} bloquée(s) par vos règles",
        "nothing": "Aucune facture en attente.",
        "todo_title": "À valider par vous", "todo_empty": "Rien à valider. Tout est en ordre.",
        "risk": "Risque", "approve": "Approuver et payer", "reject": "Rejeter",
        "confirm_high": "Je confirme : ce paiement présente un risque élevé.",
        "paid_ok": "{id} payée. Transaction confirmée sur le XRP Ledger.",
        "pay_refused": "{id} refusée par le co-signataire : {why}",
        "pay_error": "{id} : erreur ({why})", "rejected_ok": "{id} rejetée. Elle ne sera pas payée.",
        "raise_pay": "Relever la limite à {amt} et payer",
        "raise_hint": "Cette facture vient d'un fournisseur autorisé, mais dépasse vos règles. "
                      "Vous seul(e) pouvez relever la limite.",
        "addr_known": "Adresse habituelle du fournisseur", "addr_invoice": "Adresse indiquée sur cette facture",
        "call_advice": "📞 **Avant toute décision, appelez {supplier} sur son numéro habituel**, pas celui "
                       "écrit sur la facture. Un changement de coordonnées annoncé par e-mail est le signe "
                       "classique d'une fraude.",
        "no_pay_addr": "Paiement impossible : cette adresse n'est pas un fournisseur autorisé. "
                       "Le co-signataire refusera de signer, même si l'agent essaie.",
        "dup_of": "Ressemble à **{id}** du {date} ({status}). Vérifiez qu'il ne s'agit pas d'un double envoi.",
        "k_blocked": "+{n} bloquée(s) par vos règles", "k_balance_help": "Solde exact : {amt}",
        "progress": {"list_pending_invoices": "📥 Lecture des factures en attente",
                     "score_invoice": "🔎 Analyse du risque de {id}", "pay_invoice": "💸 Paiement de {id}…",
                     "flag_invoice": "🚩 {id} signalée pour vous"},
        "p_paid": "✅ {id} payée, confirmée sur le XRP Ledger", "p_blocked": "⛔ {id} bloquée par vos règles",
        "c_receipt": "Reçu", "send_wa": "Envoyer sur WhatsApp",
        "wa_text": "Paiement effectué via SignOff : facture {id} ({supplier}), {amt}. "
                   "Preuve sur le XRP Ledger : {link}",
        "tab_inv": "Toutes les factures", "tab_rules": "Règles de dépense", "tab_journal": "Journal d'audit",
        "tab_sec": "Sécurité",
        "c_invoice": "Facture", "c_supplier": "Fournisseur", "c_amount": "Montant",
        "c_status": "Statut", "c_note": "Détail", "c_time": "Heure (UTC)", "c_decision": "Décision",
        "c_why": "Motif", "c_tx": "Transaction", "view_tx": "voir",
        "rules_intro": "Ces limites sont appliquées par le co-signataire, hors de portée de l'agent.",
        "pause": "Suspendre tous les paiements de l'agent",
        "per_tx": "Maximum par paiement ({cur})", "daily": "Maximum par jour ({cur})",
        "memo": "Exiger la référence de facture sur chaque paiement",
        "suppliers": "Fournisseurs autorisés", "save": "Enregistrer les règles",
        "saved": "Règles enregistrées. Elles s'appliquent dès le prochain paiement.",
        "refused": "Modification refusée ({code}) : {text}", "last_change": "Dernière modification : {by}, {at} UTC",
        "journal_empty": "Aucun paiement tenté pour l'instant. Lancez l'agent.",
        "sec": "**Trois verrous, indépendants de l'IA :**\n\n"
               "- 🔐 **Multisignature XRPL** : la trésorerie exige 2 signatures. L'agent n'en a qu'une. "
               "Seul, il est refusé par le ledger (`tefBAD_QUORUM`).\n"
               "- 🛡️ **Co-signataire indépendant** : un service séparé, avec sa propre clé, vérifie vos limites, "
               "les fournisseurs autorisés, les doublons et les frais avant de signer.\n"
               "- 👤 **Vous seul(e)** pouvez changer les règles, avec votre jeton opérateur. L'agent ne l'a pas.\n\n"
               "Chaque paiement porte la référence de la facture, sur le ledger public : l'audit est vérifiable par tous.",
        "status": {"pending": "⏳ À traiter", "paid": "✅ Payée", "flagged": "🚩 Signalée",
                   "blocked_by_policy": "⛔ Bloquée par vos règles", "approved_by_cfo": "👍 Approuvée",
                   "rejected": "✖️ Rejetée"},
        "decision": {"APPROVED": "✅ Approuvé", "REJECTED": "⛔ Refusé"},
        "jstatus": {"EXECUTED": "Confirmé sur le ledger", "SUBMITTING": "En cours", "UNCERTAIN": "En vérification",
                    "FAILED": "Échec", "REJECTED": "Non envoyé"},
        "level": {"low": "faible", "medium": "moyen", "high": "élevé"},
        "why": [
            (r"per-payment limit of (\d+(?:\.\d+)?)", "dépasse votre maximum par paiement ({0})"),
            (r"Daily limit exceeded", "dépasserait votre maximum journalier"),
            (r"not an approved supplier", "le destinataire n'est pas un fournisseur autorisé"),
            (r"paused", "les paiements sont suspendus"),
            (r"already paid", "facture déjà payée"),
            (r"Fee .* exceeds", "frais de transaction anormaux"),
            (r"InvoiceID", "référence de facture invalide"),
            (r"simulation failed \((\w+)\)", "la simulation sur le ledger a échoué ({0})"),
            (r"memo", "référence de facture manquante"),
            (r"Pay-to address differs", "l'adresse de paiement diffère de celle du fournisseur (fraude au changement de RIB)"),
            (r"not in the vendor registry", "fournisseur inconnu"),
            (r"standard deviations|unusual for this supplier", "montant inhabituel pour ce fournisseur"),
            (r"Not enough payment history", "historique de paiement insuffisant"),
            (r"Possible duplicate of ([\w-]+)", "doublon probable de {0}"),
            (r"Pressure / change-of-details wording", "formulation de pression (« urgent », « nouveau compte »…)"),
            (r"No risk signal", "aucun signal de risque"),
        ],
    },
    "en": {
        "tagline": "Your agent pays your suppliers. You set the rules. The XRP Ledger enforces them.",
        "lang": "Language", "network": "Network", "treasury": "Treasury account",
        "how_title": "How it works",
        "how": "1. **The agent** reads invoices and detects fraud.\n"
               "2. **The co-signer** checks your rules before every payment.\n"
               "3. **The XRP Ledger** requires both signatures: the agent alone can't pay anything.",
        "demo": "Demo tools", "reset": "Reset the demo", "reset_done": "Demo reset.",
        "unreachable": "The co-signer is unreachable ({url}). Run: `uvicorn policy_service:app --port 8000`",
        "k_balance": "Treasury balance", "k_paid": "Paid by the agent", "k_caught": "Fraud and anomalies stopped",
        "k_saved": "Fees saved (est.)", "unavailable": "unavailable",
        "k_saved_help": "Estimate: an international bank transfer from WAEMU costs about {pct}% (fees + FX) "
                        "and takes 2 to 5 days. On the XRP Ledger: a fraction of a cent, in seconds.",
        "k_payments": "{n} payment(s)",
        "k_caught_help": "Suspicious invoices flagged by the agent or rejected by you.",
        "run_title": "Today's invoices", "run_btn": "▶ Run the agent",
        "run_help": "The agent checks every pending invoice, pays the safe ones and flags the rest for you.",
        "running": "The agent is processing invoices…", "run_done": "Done", "run_log": "Agent technical log",
        "run_summary": "{paid} paid · {flagged} flagged · {blocked} blocked by your rules",
        "nothing": "No pending invoice.",
        "todo_title": "Needs your decision", "todo_empty": "Nothing to review. All good.",
        "risk": "Risk", "approve": "Approve and pay", "reject": "Reject",
        "confirm_high": "I confirm: this payment is high risk.",
        "paid_ok": "{id} paid. Transaction confirmed on the XRP Ledger.",
        "pay_refused": "{id} refused by the co-signer: {why}",
        "pay_error": "{id}: error ({why})", "rejected_ok": "{id} rejected. It won't be paid.",
        "raise_pay": "Raise the limit to {amt} and pay",
        "raise_hint": "This invoice is from an approved supplier but exceeds your rules. "
                      "Only you can raise the limit.",
        "addr_known": "Supplier's usual address", "addr_invoice": "Address on this invoice",
        "call_advice": "📞 **Before deciding, call {supplier} on their usual number**, not the one "
                       "written on the invoice. A change of details announced by email is the classic "
                       "sign of fraud.",
        "no_pay_addr": "Payment impossible: this address is not an approved supplier. "
                       "The co-signer will refuse to sign, even if the agent tries.",
        "dup_of": "Looks like **{id}** from {date} ({status}). Check it isn't sent twice.",
        "k_blocked": "+{n} blocked by your rules", "k_balance_help": "Exact balance: {amt}",
        "progress": {"list_pending_invoices": "📥 Reading pending invoices",
                     "score_invoice": "🔎 Scoring the risk of {id}", "pay_invoice": "💸 Paying {id}…",
                     "flag_invoice": "🚩 {id} flagged for you"},
        "p_paid": "✅ {id} paid, confirmed on the XRP Ledger", "p_blocked": "⛔ {id} blocked by your rules",
        "c_receipt": "Receipt", "send_wa": "Send on WhatsApp",
        "wa_text": "Payment made via SignOff: invoice {id} ({supplier}), {amt}. "
                   "Proof on the XRP Ledger: {link}",
        "tab_inv": "All invoices", "tab_rules": "Spending rules", "tab_journal": "Audit journal",
        "tab_sec": "Security",
        "c_invoice": "Invoice", "c_supplier": "Supplier", "c_amount": "Amount",
        "c_status": "Status", "c_note": "Details", "c_time": "Time (UTC)", "c_decision": "Decision",
        "c_why": "Reason", "c_tx": "Transaction", "view_tx": "view",
        "rules_intro": "These limits are enforced by the co-signer, out of the agent's reach.",
        "pause": "Pause all agent payments",
        "per_tx": "Max per payment ({cur})", "daily": "Max per day ({cur})",
        "memo": "Require the invoice reference on every payment",
        "suppliers": "Approved suppliers", "save": "Save rules",
        "saved": "Rules saved. They apply from the next payment.",
        "refused": "Update refused ({code}): {text}", "last_change": "Last change: {by}, {at} UTC",
        "journal_empty": "No payment attempt yet. Run the agent.",
        "sec": "**Three locks, independent of the AI:**\n\n"
               "- 🔐 **XRPL multisig**: the treasury needs 2 signatures. The agent has only one. "
               "Alone, the ledger refuses it (`tefBAD_QUORUM`).\n"
               "- 🛡️ **Independent co-signer**: a separate service with its own key checks your limits, "
               "approved suppliers, duplicates and fees before signing.\n"
               "- 👤 **Only you** can change the rules, with your operator token. The agent doesn't have it.\n\n"
               "Every payment carries the invoice reference on the public ledger: anyone can audit it.",
        "status": {"pending": "⏳ To process", "paid": "✅ Paid", "flagged": "🚩 Flagged",
                   "blocked_by_policy": "⛔ Blocked by your rules", "approved_by_cfo": "👍 Approved",
                   "rejected": "✖️ Rejected"},
        "decision": {"APPROVED": "✅ Approved", "REJECTED": "⛔ Refused"},
        "jstatus": {"EXECUTED": "Confirmed on the ledger", "SUBMITTING": "In progress", "UNCERTAIN": "Being checked",
                    "FAILED": "Failed", "REJECTED": "Not sent"},
        "level": {"low": "low", "medium": "medium", "high": "high"},
        "why": [
            (r"per-payment limit of (\d+(?:\.\d+)?)", "above your per-payment maximum ({0})"),
            (r"Daily limit exceeded", "would exceed your daily maximum"),
            (r"not an approved supplier", "the recipient is not an approved supplier"),
            (r"paused", "payments are paused"),
            (r"already paid", "invoice already paid"),
            (r"Fee .* exceeds", "abnormal transaction fee"),
            (r"InvoiceID", "invalid invoice reference"),
            (r"simulation failed \((\w+)\)", "the ledger simulation failed ({0})"),
            (r"memo", "missing invoice reference"),
            (r"Pay-to address differs", "pay-to address differs from the supplier's (bank-details-change fraud)"),
            (r"not in the vendor registry", "unknown supplier"),
            (r"standard deviations|unusual for this supplier", "unusual amount for this supplier"),
            (r"Not enough payment history", "not enough payment history"),
            (r"Possible duplicate of ([\w-]+)", "likely duplicate of {0}"),
            (r"Pressure / change-of-details wording", "pressure wording (“urgent”, “new account”…)"),
            (r"No risk signal", "no risk signal"),
        ],
    },
}

with st.sidebar:
    lang = st.segmented_control("🌐", ["fr", "en"], default="fr", format_func=lambda x: {"fr": "Français", "en": "English"}[x],
                                key="lang", label_visibility="collapsed") or "fr"
L = T[lang]


# ------------------------------------------------------------------ helpers
def money(value) -> str:
    s = f"{float(value):,.2f}"
    if lang == "fr":
        s = s.replace(",", " ").replace(".", ",")
    return f"{s} {asset_label()}"


def xof(value, compact: bool = False) -> str:
    n = float(value) * XOF_PER_USD
    if compact and n >= 1_000_000:  # KPI tiles: "17,8 M FCFA" instead of a truncated full number
        s = f"{n / 1_000_000:.1f} M"
        return (s.replace(".", ",") + " FCFA") if lang == "fr" else s + " XOF"
    s = f"{round(n):,}"
    return (s.replace(",", " ") if lang == "fr" else s) + (" FCFA" if lang == "fr" else " XOF")


def humanize(reason: str) -> str:
    for pattern, text in L["why"]:
        m = re.search(pattern, reason, flags=re.IGNORECASE)
        if m:
            return text.format(*(money(g) if re.fullmatch(r"\d+(\.\d+)?", g) else g for g in m.groups()))
    return reason


def humanize_all(reasons: list[str]) -> str:
    return " · ".join(dict.fromkeys(humanize(r) for r in reasons))


AGENT_CMD = [sys.executable, "-u", str(ROOT / "agent.py")]


def run_agent(*args: str) -> subprocess.CompletedProcess:
    # The agent runs as its own process, with its own key: the console never signs payments.
    return subprocess.run([*AGENT_CMD, *args], cwd=ROOT, capture_output=True, text=True, timeout=600)


CALL = re.compile(r"^\s*-> (\w+)\((.*)\)$")
RESULT = re.compile(r"^\s*<- (.*)$")


def stream_agent(box) -> tuple[int, str]:
    """Run the agent and show each step live, in plain language."""
    proc = subprocess.Popen(AGENT_CMD, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    log, current = [], None
    for line in proc.stdout:
        log.append(line)
        if m := CALL.match(line):
            name = m.group(1)
            try:
                current = json.loads(m.group(2) or "{}").get("invoice_id")
            except ValueError:
                current = None
            step = L["progress"].get(name)
            if step:
                box.write(step.format(id=current))
                box.update(label=step.format(id=current))
        elif (m := RESULT.match(line)) and current:
            if '"PAID"' in m.group(1):
                box.write(L["p_paid"].format(id=current))
            elif '"REJECTED_BY_POLICY"' in m.group(1):
                box.write(L["p_blocked"].format(id=current))
    proc.wait()
    return proc.returncode, "".join(log)


def set_invoice(invoice_id: str, **fields) -> None:
    invoices = load_json("invoices.json", [])
    for inv in invoices:
        if inv["id"] == invoice_id:
            inv.update(fields)
    save_json("invoices.json", invoices)


def flash(kind: str, text: str) -> None:
    st.session_state.setdefault("flash", []).append((kind, text))


USD_LIKE = asset_label().upper() in ("USD", "RLUSD")


def amount(value) -> str:
    """Local currency first in French (the owner thinks in FCFA), USD first in English."""
    if not USD_LIKE:
        return money(value)
    return f"{xof(value)} ({money(value)})" if lang == "fr" else f"{money(value)} (≈ {xof(value)})"


def short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


RISK_COLOR = {"low": ("green", "#0E7C66"), "medium": ("orange", "#D98E04"), "high": ("red", "#C62828")}


def risk_bar(score: int, level: str) -> None:
    badge_color, hex_color = RISK_COLOR[level]
    st.badge(f"{L['risk']} {L['level'][level]} · {score}/100", color=badge_color)
    st.markdown(
        f'<div role="progressbar" aria-valuenow="{score}" aria-valuemin="0" aria-valuemax="100" '
        f'style="background:rgba(128,128,128,.18);border-radius:6px;height:10px;margin:-4px 0 8px">'
        f'<div style="width:{score}%;background:{hex_color};height:10px;border-radius:6px"></div></div>',
        unsafe_allow_html=True)


def spent_today(journal: list[dict]) -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    return sum(float(j["amount"]) for j in journal
               if j["status"] in ("EXECUTED", "SUBMITTING", "UNCERTAIN") and j["amount"] and j["ts"].startswith(today))


def put_policy(policy: dict, **changes) -> httpx.Response:
    body = {"paused": policy["paused"], "per_tx_max": str(policy["per_tx_max"]),
            "daily_max": str(policy["daily_max"]), "allowlist": policy["allowlist"],
            "require_memo": policy.get("require_memo", True),
            "max_fee_drops": int(policy.get("max_fee_drops", 1000))}
    body.update({k: str(v) if isinstance(v, float) else v for k, v in changes.items()})
    return httpx.put(f"{SERVICE}/policy", timeout=10, headers={"x-admin-token": TOKEN}, json=body)


def approve_and_pay(inv: dict) -> None:
    """CFO decision: the agent process signs, the co-signer still checks every rule."""
    set_invoice(inv["id"], status="approved_by_cfo")
    proc = run_agent("--pay", inv["id"])
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        out = {"result": "ERROR", "detail": (proc.stderr or proc.stdout)[-300:]}
    if out.get("result") == "PAID":
        flash("success", L["paid_ok"].format(id=inv["id"]))
    elif out.get("result") == "REJECTED_BY_POLICY":
        set_invoice(inv["id"], status="blocked_by_policy", policy_reasons=out["violations"])
        flash("error", L["pay_refused"].format(id=inv["id"], why=humanize_all(out["violations"])))
    else:
        set_invoice(inv["id"], status=inv["status"])
        flash("error", L["pay_error"].format(id=inv["id"], why=out.get("detail") or out.get("error")))


def receipt_link(inv: dict) -> str | None:
    if inv.get("status") != "paid" or not inv.get("tx_hash"):
        return None
    text = L["wa_text"].format(id=inv["id"], supplier=inv["supplier"], amt=amount(inv["amount"]),
                               link=tx_link(inv["tx_hash"]))
    return "https://wa.me/?text=" + quote(text)


# ------------------------------------------------------------------ data
try:
    policy = httpx.get(f"{SERVICE}/policy", timeout=10).json()
    journal = httpx.get(f"{SERVICE}/journal", timeout=10).json()
except httpx.HTTPError:
    st.error(L["unreachable"].format(url=SERVICE))
    st.stop()

invoices = load_json("invoices.json", [])
history = load_json("history.json", [])
registry = load_json("suppliers.json", {})

# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("## ✅ SignOff")
    st.caption(L["tagline"])
    st.markdown(f"**{L['network']}** : XRPL {network().capitalize()}")
    st.markdown(f"**{L['treasury']}** : [`{TREASURY[:6]}…{TREASURY[-4:]}`]({account_link(TREASURY)})")
    with st.expander(L["how_title"], expanded=True):
        st.markdown(L["how"])
    with st.expander(L["demo"]):
        if st.button(L["reset"], width="stretch"):
            run_agent("--reset")
            flash("success", L["reset_done"])
            st.rerun()

# ------------------------------------------------------------------ header + KPIs
st.title("SignOff")
st.caption(L["tagline"])

for kind, text in st.session_state.pop("flash", []):
    getattr(st, kind)(text)

executed = [j for j in journal if j["status"] == "EXECUTED" and j["amount"]]
paid_total = sum(float(j["amount"]) for j in executed)
caught = sum(1 for i in invoices if i["status"] in ("flagged", "rejected"))
blocked = sum(1 for i in invoices if i["status"] == "blocked_by_policy")
def local(value) -> str:
    return xof(value, compact=True) if USD_LIKE else money(value)

k1, k2, k3, k4 = st.columns(4)
try:
    balance = treasury_balance(get_client()).split()[0]
    k1.metric(L["k_balance"], local(balance), help=L["k_balance_help"].format(amt=money(balance)))
except Exception:
    k1.metric(L["k_balance"], L["unavailable"])
k2.metric(L["k_paid"], local(paid_total), L["k_payments"].format(n=len(executed)), delta_color="off",
          help=money(paid_total))
k3.metric(L["k_caught"], caught, L["k_blocked"].format(n=blocked) if blocked else None, delta_color="off",
          help=L["k_caught_help"])
k4.metric(L["k_saved"], local(paid_total * BANK_FEE_PCT / 100), help=L["k_saved_help"].format(pct=BANK_FEE_PCT))

# ------------------------------------------------------------------ run the agent
st.subheader(L["run_title"])
pending = [i for i in invoices if i["status"] in ("pending", "blocked_by_policy")]
c1, c2 = st.columns([1, 3], vertical_alignment="center")
run = c1.button(L["run_btn"], type="primary", width="stretch", disabled=not pending)
c2.caption(L["run_help"] if pending else L["nothing"])
if run:
    before = {i["id"]: i["status"] for i in invoices}
    with st.status(L["running"], expanded=True) as box:
        code, log = stream_agent(box)
        after = {i["id"]: i["status"] for i in load_json("invoices.json", [])}
        changed = [s for k, s in after.items() if before.get(k) != s or s == "blocked_by_policy"]
        box.update(label=L["run_done"], state="complete" if code == 0 else "error")
    if code != 0:
        flash("error", log[-1500:])
    flash("success" if code == 0 else "error", L["run_summary"].format(
        paid=changed.count("paid"), flagged=changed.count("flagged"), blocked=changed.count("blocked_by_policy")))
    st.rerun()

# ------------------------------------------------------------------ needs your decision
st.subheader(L["todo_title"])
todo = [i for i in invoices if i["status"] in ("flagged", "blocked_by_policy")]
if not todo:
    st.success(L["todo_empty"])
by_id = {i["id"]: i for i in invoices}
for inv in todo:
    risk = score_invoice(inv, invoices, history, registry)
    level, feats = risk["risk_level"], risk["features"]
    known = registry.get(inv["supplier"])
    payable_address = inv["pay_to"] in policy["allowlist"]
    with st.container(border=True):
        top, badge = st.columns([4, 1], vertical_alignment="center")
        top.markdown(f"**{inv['id']} · {inv['supplier']}** · {inv.get('date', '')}")
        top.markdown(f"#### {amount(inv['amount'])}")
        top.caption(f"“{inv.get('description', '')}”")
        with badge:
            st.badge(L["status"][inv["status"]], color="red" if inv["status"] == "flagged" else "orange")

        approve_label, raise_to = L["approve"], {}
        if inv["status"] == "blocked_by_policy":
            st.markdown(f"⛔ {humanize_all(inv.get('policy_reasons', []))}")
            per_tx, daily = float(policy["per_tx_max"]), float(policy["daily_max"])
            if inv["amount"] > per_tx:
                raise_to["per_tx_max"] = float(inv["amount"])
            needed_daily = spent_today(journal) + inv["amount"]
            if needed_daily > daily:
                raise_to["daily_max"] = float(needed_daily)
            if raise_to:
                approve_label = L["raise_pay"].format(amt=money(raise_to.get("per_tx_max", raise_to.get("daily_max"))))
                st.caption(L["raise_hint"])
        else:
            risk_bar(risk["risk_score"], level)
            for r in risk["reasons"]:
                st.markdown(f"- {humanize(r)}")
            if known and inv["pay_to"] != known["address"]:
                a1, a2 = st.columns(2)
                a1.markdown(f"✅ {L['addr_known']}  \n`{short(known['address'])}`")
                a2.markdown(f"❌ {L['addr_invoice']}  \n`{short(inv['pay_to'])}`")
                st.warning(L["call_advice"].format(supplier=inv["supplier"]))
            if (dup := by_id.get(feats.get("duplicate_of") or "")):
                st.info(L["dup_of"].format(id=dup["id"], date=dup.get("date", ""),
                                           status=L["status"].get(dup["status"], dup["status"])))

        confirmed = True
        if not payable_address:
            st.error(L["no_pay_addr"])
        elif level == "high" and inv["status"] == "flagged":
            confirmed = st.checkbox(L["confirm_high"], key=f"confirm-{inv['id']}")

        # The safe choice is the prominent one: reject first for suspicious invoices.
        suspicious = inv["status"] == "flagged" and level != "low"
        b1, b2, _ = st.columns([1.4, 1, 1.6])
        reject_btn, approve_btn = (b1, b2) if suspicious else (b2, b1)
        if reject_btn.button(L["reject"], key=f"no-{inv['id']}", width="stretch",
                             type="primary" if suspicious else "secondary"):
            set_invoice(inv["id"], status="rejected", decided_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            flash("info", L["rejected_ok"].format(id=inv["id"]))
            st.rerun()
        if payable_address and approve_btn.button(approve_label, key=f"ok-{inv['id']}", disabled=not confirmed,
                                                  width="stretch", type="secondary" if suspicious else "primary"):
            if raise_to:
                r = put_policy(policy, **raise_to)
                if r.status_code != 200:
                    flash("error", L["refused"].format(code=r.status_code, text=r.text))
                    st.rerun()
            approve_and_pay(inv)
            st.rerun()

# ------------------------------------------------------------------ tabs
tab_inv, tab_rules, tab_journal, tab_sec = st.tabs([L["tab_inv"], L["tab_rules"], L["tab_journal"], L["tab_sec"]])

with tab_inv:
    st.dataframe([{
        L["c_invoice"]: i["id"], L["c_supplier"]: i["supplier"], L["c_amount"]: amount(i["amount"]),
        L["c_status"]: L["status"].get(i["status"], i["status"]),
        L["c_tx"]: tx_link(i["tx_hash"]) if i.get("tx_hash") else None,
        L["c_receipt"]: receipt_link(i),
        L["c_note"]: "" if i["status"] == "paid"
        else humanize_all(i.get("policy_reasons", [])) if i.get("policy_reasons")
        else humanize_all(re.split(r"(?<=\.)\s+", i["flag_reason"])) if i.get("flag_reason") else "",
    } for i in invoices], width="stretch", hide_index=True, column_config={
        L["c_tx"]: st.column_config.LinkColumn(L["c_tx"], display_text=L["view_tx"]),
        L["c_receipt"]: st.column_config.LinkColumn(L["c_receipt"], display_text="📲 " + L["send_wa"])})

with tab_rules:
    st.caption(L["rules_intro"])
    with st.form("policy"):
        paused = st.toggle(L["pause"], value=policy["paused"])
        r1, r2 = st.columns(2)
        per_tx = r1.number_input(L["per_tx"].format(cur=asset_label()), min_value=0.0,
                                 value=float(policy["per_tx_max"]), step=100.0)
        if USD_LIKE:
            r1.caption(f"≈ {xof(policy['per_tx_max'])}")
        daily = r2.number_input(L["daily"].format(cur=asset_label()), min_value=0.0,
                                value=float(policy["daily_max"]), step=500.0)
        if USD_LIKE:
            r2.caption(f"≈ {xof(policy['daily_max'])}")
        require_memo = st.checkbox(L["memo"], value=policy.get("require_memo", True))
        st.markdown(f"**{L['suppliers']}**")
        allowlist = {}
        for name, info in registry.items():
            label = f"{name} ({info['country']}) · `{info['address'][:6]}…{info['address'][-4:]}`"
            if st.checkbox(label, value=info["address"] in policy["allowlist"], key=info["address"]):
                allowlist[info["address"]] = name
        saved = st.form_submit_button(L["save"], type="primary")
    if saved:
        r = put_policy(policy, paused=paused, per_tx_max=per_tx, daily_max=daily,
                       allowlist=allowlist, require_memo=require_memo)
        if r.status_code == 200:
            flash("success", L["saved"])
            st.rerun()
        else:
            st.error(L["refused"].format(code=r.status_code, text=r.text))
    if policy.get("updated_at"):
        st.caption(L["last_change"].format(by=policy.get("updated_by"), at=policy["updated_at"]))

with tab_journal:
    if not journal:
        st.info(L["journal_empty"])
    else:
        st.dataframe([{
            L["c_time"]: j["ts"].replace("T", " ").replace("+00:00", ""), L["c_invoice"]: j["invoice_id"],
            L["c_amount"]: money(j["amount"]) if j["amount"] else "",
            L["c_decision"]: L["decision"].get(j["decision"], j["decision"]),
            L["c_status"]: L["jstatus"].get(j["status"], j["status"]),
            L["c_why"]: humanize_all(j["reasons"]) if j["reasons"] else (j["justification"] or ""),
            L["c_tx"]: j["explorer"],
        } for j in journal], width="stretch", hide_index=True, column_config={
            L["c_tx"]: st.column_config.LinkColumn(L["c_tx"], display_text=L["view_tx"])})

with tab_sec:
    st.markdown(L["sec"])
