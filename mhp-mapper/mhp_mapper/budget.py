"""Hard spending caps, enforced in code.

Every paid call goes through `guard(service, ...)` first. Spending is recorded in a ledger
file (data/spend_ledger.json) that is shared by all parallel workers (file lock) and keyed
by calendar month. When a call would push a service or the monthly total over its cap,
`BudgetExceeded` is raised and the run stops. Nothing is "pay and see".

Caps come from .env (see .env.example); the defaults below are the PILOT caps.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def caps() -> dict:
    return {
        # Monthly dollar caps per service, plus an overall monthly cap.
        "total_usd": _f("MHP_CAP_TOTAL_USD", 100),
        "claude_usd": _f("MHP_CAP_CLAUDE_USD", 50),
        "google_places_usd": _f("MHP_CAP_GOOGLE_USD", 0),
        "parcel_api_usd": _f("MHP_CAP_PARCEL_USD", 0),
        # Call-count caps (protect free tiers / trials even when the $ price is unknown).
        "google_places_calls": _f("MHP_CAP_GOOGLE_CALLS", 4500),  # Google's free tier is 5,000/mo
        "parcel_api_calls": _f("MHP_CAP_PARCEL_CALLS", 1000),     # ReportAll trial size
    }


# Unit prices. Google Text Search Pro: $32 / 1,000 after 5,000 free calls a month.
# Claude Opus 5.5: $4 / $20 per million input / output tokens.
GOOGLE_FREE_CALLS = 5000
GOOGLE_USD_PER_CALL = 0.032
CLAUDE_USD_PER_MTOK = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
                       "claude-haiku-5-5": (0.10, 0.50)}
CLAUDE_EST_USD_PER_CALL = 0.08  # pre-call reservation; the real cost is recorded afterwards


def parcel_usd_per_call() -> float:
    return _f("MHP_PARCEL_USD_PER_CALL", 0.0)  # set once ReportAll / Regrid quote a price


def _ledger_path() -> Path:
    from .config import get_settings

    return get_settings().data_dir / "spend_ledger.json"


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@contextmanager
def _locked_ledger():
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        data = json.loads(raw) if raw.strip() else {}
        month = data.setdefault(_month(), {})
        yield month
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(data, indent=2))
        fcntl.flock(fh, fcntl.LOCK_UN)


def _check(month: dict, service: str, usd: float, calls: int, c: dict) -> None:
    svc = month.get(service, {"usd": 0.0, "calls": 0})
    total = sum(v["usd"] for v in month.values())
    if f"{service}_usd" in c and svc["usd"] + usd > c[f"{service}_usd"] + 1e-9:
        raise BudgetExceeded(f"{service}: ${svc['usd'] + usd:.2f} would exceed cap ${c[f'{service}_usd']:.2f}/month")
    if f"{service}_calls" in c and svc["calls"] + calls > c[f"{service}_calls"]:
        raise BudgetExceeded(f"{service}: {svc['calls'] + calls} calls would exceed cap {int(c[f'{service}_calls'])}/month")
    if total + usd > c["total_usd"] + 1e-9:
        raise BudgetExceeded(f"total: ${total + usd:.2f} would exceed overall cap ${c['total_usd']:.2f}/month")


def _add(month: dict, service: str, usd: float, calls: int) -> None:
    svc = month.setdefault(service, {"usd": 0.0, "calls": 0})
    svc["usd"] = round(svc["usd"] + usd, 4)
    svc["calls"] += calls


def guard(service: str, calls: int = 1) -> None:
    """Reserve one paid call. Raises BudgetExceeded instead of overspending."""
    c = caps()
    with _locked_ledger() as month:
        used = month.get(service, {"calls": 0})["calls"]
        if service == "google_places":
            free_left = max(0, GOOGLE_FREE_CALLS - used)
            usd = max(0, calls - free_left) * GOOGLE_USD_PER_CALL
        elif service == "parcel_api":
            usd = calls * parcel_usd_per_call()
        elif service == "claude":
            usd = CLAUDE_EST_USD_PER_CALL * calls
        else:
            usd = 0.0
        _check(month, service, usd, calls, c)
        _add(month, service, usd, calls)


def settle_claude(model: str, input_tokens: int, output_tokens: int) -> float:
    """Replace the pre-call reservation with the actual cost of a Claude call."""
    pin, pout = CLAUDE_USD_PER_MTOK.get(model, (4.0, 20.0))
    actual = input_tokens / 1e6 * pin + output_tokens / 1e6 * pout
    with _locked_ledger() as month:
        _add(month, "claude", actual - CLAUDE_EST_USD_PER_CALL, 0)
    return actual


def report() -> dict:
    with _locked_ledger() as month:
        snapshot = json.loads(json.dumps(month))
    c = caps()
    return {"month": _month(), "spent": snapshot,
            "total_usd": round(sum(v["usd"] for v in snapshot.values()), 2), "caps": c}
