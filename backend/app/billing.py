"""Volcengine Billing cost lookup for the admin overview."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from .config import VOLCENGINE_ACCESS_KEY, VOLCENGINE_SECRET_KEY
from .logging_setup import logger


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _sign(payload: bytes, query: str, date: str) -> str:
    host = "open.volcengineapi.com"
    content_type = "application/json"
    hashed = hashlib.sha256(payload).hexdigest()
    canonical_headers = f"content-type:{content_type}\nhost:{host}\nx-date:{date}\n"
    signed_headers = "content-type;host;x-date"
    canonical = f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{hashed}"
    day = date[:8]
    scope = f"{day}/cn-north-1/billing/request"
    string_to_sign = f"HMAC-SHA256\n{date}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    key = _hmac(_hmac(_hmac(_hmac(("VOLC" + VOLCENGINE_SECRET_KEY).encode(), day), "cn-north-1"), "billing"), "request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return f"HMAC-SHA256 Credential={VOLCENGINE_ACCESS_KEY}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"


def fetch_monthly_ark_cost(month: str) -> float | None:
    """Return actual payable CNY for Ark's current billing month.

    Billing data is delayed by the provider. Missing credentials or a provider
    error deliberately returns None so the admin page can keep using its local
    task-cost fallback.
    """
    if not VOLCENGINE_ACCESS_KEY or not VOLCENGINE_SECRET_KEY:
        return None
    query = "Action=ListAmortizedCostBillDetail&Version=2022-01-01"
    body = json.dumps({"AmortizedMonth": month, "Product": ["ark"], "Limit": 300, "Offset": 0, "NeedRecordNum": 1, "IgnoreZero": 1}, separators=(",", ":")).encode()
    date = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    request = Request(
        f"https://open.volcengineapi.com/?{query}",
        data=body,
        headers={"Content-Type": "application/json", "X-Date": date, "Authorization": _sign(body, query, date)},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("volcengine_billing_failed month=%s error=%s", month, str(exc)[:300])
        return None
    rows = ((payload.get("Result") or {}).get("List") or []) if isinstance(payload, dict) else []
    total = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("AmortizedPayableAmount", row.get("DailyAmortizedPayableAmount", 0))
        try:
            total += float(value or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)
