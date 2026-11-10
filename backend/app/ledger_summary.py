"""Read-only allocations of historical credits; never change balances or cash."""
from decimal import Decimal, ROUND_HALF_UP
import re

from .config import POINTS_PER_YUAN


def number(value):
    return Decimal(str(value or 0))


def ledger_components(row):
    """Split only gifts demonstrably included in this credited amount.

    Old admin recharges stored face value as RMB even for gifts. Explicit
    gift-only notes classify the whole entry. Mixed notes must reconcile to
    credited points; a promised gift outside this entry is never counted twice.
    """
    item = dict(row)
    if item["entry_type"] != "recharge":
        return [item]
    reason = re.sub(r"\s+", "", str(item.get("reason") or ""))
    points = number(item["points_amount"])
    rate = number(POINTS_PER_YUAN)
    face = points / rate
    if points <= 0:
        return [{**item, "entry_type": "adjustment", "rmb_amount": None}]
    base = (re.search(r"(?:充值|实付|付款)[¥￥]?(\d+(?:\.\d+)?)元?", reason)
            or re.search(r"(\d+(?:\.\d+)?)元?(?:套餐|[，,；;]?赠|[，,；;]?加赠)", reason))
    if re.match(r"^(?:赠送|加赠|赠|补偿积分)", reason) and not base:
        return [{**item, "entry_type": "grant", "rmb_amount": None, "allocation": "gift_note"}]
    if not re.search(r"赠|[+＋]", reason) or re.search(r"(?:未|不|无|取消)赠", reason):
        return [item]
    gift = re.search(r"(?:加赠送|赠送|加赠|赠|[+＋])[¥￥]?(\d+(?:\.\d+)?)(元|积分|分)?", reason)
    if base and gift:
        principal = number(base[1])
        bonus = number(gift[1]) / (rate if gift[2] in {"积分", "分"} else 1)
        tolerance = Decimal("0.051") / rate
        if abs(face - principal) <= tolerance:
            # The note describes a separate/promised bonus, absent from this
            # row's points. Do not subtract it from real principal or invent it.
            return [item]
        if abs(face - bonus) <= tolerance and principal > bonus:
            return [{**item, "entry_type": "grant", "rmb_amount": None, "allocation": "gift_note"}]
        if bonus > 0 and principal > 0 and abs(face - principal - bonus) <= tolerance:
            gift_points = (bonus * rate).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            return [
                {**item, "points_amount": float(points - gift_points), "rmb_amount": float(principal), "allocation": "principal"},
                {**item, "points_amount": float(gift_points), "rmb_amount": None, "entry_type": "grant", "allocation": "included_gift"},
            ]
    # Ambiguous descriptions must not silently inflate reported cash revenue.
    return [{**item, "entry_type": "other", "rmb_amount": None, "allocation": "needs_review"}]


def ledger_totals(items):
    result = {kind: {"amount_rmb": Decimal(0), "points": Decimal(0), "count": 0, "unrecorded_cash_count": 0}
              for kind in ("recharge", "refund", "consume", "grant", "adjustment", "other")}
    review = 0
    for item in items:
        total = result[item["entry_type"]]
        points = number(item["points_amount"])
        total["count"] += 1
        total["points"] += points
        if item["entry_type"] == "recharge":
            if item["rmb_amount"] is None:
                total["unrecorded_cash_count"] += 1
            else:
                total["amount_rmb"] += number(item["rmb_amount"])
        else:
            # Refunds are returned credits, not cash refunds. Label as face
            # value in the UI. Adjustment stays signed, never sum absolutes.
            total["amount_rmb"] += points / number(POINTS_PER_YUAN)
        review += item.get("allocation") == "needs_review"
    for total in result.values():
        for key in ("amount_rmb", "points"):
            total[key] = float(total[key].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {"by_type": result, "review_count": review}
