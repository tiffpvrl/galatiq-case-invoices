"""Who should look at a held invoice.

A real AP team does not have one inbox for every exception: a foreign-currency
invoice belongs to Treasury, a suspected re-bill to the controller, a price
dispute to the buyer who negotiated it. This maps each reason code to an owner
and picks the most senior one when several apply. It is a suggestion shown in
the review queue and the report, not an access control.
"""

from .config import SETTINGS, Settings

# Ascending seniority: when several owners apply, the most senior wins.
ROLES = ["AP clerk", "Buyer", "Vendor management", "Treasury", "Controller", "VP", "CFO"]

CODE_OWNER = {
    # Possible fraud or control failures
    "PRESSURE_LANGUAGE": "Controller",
    "POSSIBLE_REBILL": "Controller",
    "INVOICE_NUMBER_COLLISION": "Controller",
    "IMMEDIATE_TERMS": "Controller",
    "RELATIVE_DUE_DATE": "Controller",
    "DUE_BEFORE_ISSUE": "Controller",
    "NEAR_THRESHOLD": "Controller",
    # Vendor master
    "UNKNOWN_VENDOR": "Vendor management",
    "VENDOR_RENAMED": "Vendor management",
    # Money movement
    "NON_SETTLEABLE_CURRENCY": "Treasury",
    "PAYMENT_FAILED": "Treasury",
    # Price disputes belong to whoever negotiated the price
    "PRICE_ABOVE_LIST": "Buyer",
    "PRICE_BELOW_LIST": "Buyer",
    # Approval tiers
    "OVER_CFO_THRESHOLD": "CFO",
    # Everything mechanical stays with AP
    "REVISION_OF_PRIOR": "AP clerk",
    "EXTRACTION_FAILED": "AP clerk",
    "INTERNAL_ERROR": "AP clerk",
    "TOTAL_MISMATCH": "AP clerk",
    "SUBTOTAL_MISMATCH": "AP clerk",
    "LINE_AMOUNT_MISMATCH": "AP clerk",
    "AMBIGUOUS_DATE": "AP clerk",
    "FUTURE_DATED": "AP clerk",
}


def _rank(role: str) -> int:
    return ROLES.index(role)


def suggest_owner(
    reasons: list[dict], total: float | None, settings: Settings = SETTINGS
) -> tuple[str, list[str]]:
    """The suggested owner and the reason codes (or amount tier) that decided it."""
    owner, because = "AP clerk", []
    for r in reasons:
        role = CODE_OWNER.get(r["code"])
        if role and _rank(role) > _rank(owner):
            owner, because = role, [r["code"]]
        elif role and role == owner and r["code"] not in because:
            because.append(r["code"])
    if total is not None:
        tier = ("CFO" if total > settings.policy.cfo_threshold
                else "VP" if total >= settings.policy.scrutiny_threshold else None)
        if tier and _rank(tier) > _rank(owner):
            owner, because = tier, [f"amount {total:,.0f}"]
    return owner, because
