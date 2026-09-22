"""Stable data constants shared by the offline agent tests."""

from __future__ import annotations

from typing import Any, Final

# Mirrors plan/yields.py:414: two judged failures still qualify as a near miss.
NEAR_MISS: Final[dict[str, Any]] = {
    "alpha_id": "AH_TEST_0001",
    "expression": "rank(ts_delta(close, 5))",
    "region": "USA",
    "delay": 1,
    "universe": "TOP3000",
    "neutralization": "MARKET",
    "decay": 4,
    "truncation": 0.08,
    "sharpe": 1.72,
    "fitness": 1.10,
    "turnover": 0.81,
    "checks": [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.58, "value": 1.72},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.10},
        {"name": "HIGH_TURNOVER", "result": "FAIL", "limit": 0.70, "value": 0.81},
        {"name": "CONCENTRATED_WEIGHT", "result": "FAIL", "limit": None, "value": None},
        {"name": "MATCHES_COMPETITION", "result": "WARNING", "limit": None, "value": None},
    ],
}

# Mirrors labs/repair.py's negative-Sharpe flip path.
NEGATIVE_SHARPE: Final[dict[str, Any]] = {
    "alpha_id": "AH_TEST_0002",
    "expression": "rank(ts_delta(close, 5))",
    "region": "USA",
    "delay": 1,
    "universe": "TOP3000",
    "neutralization": "MARKET",
    "decay": 4,
    "truncation": 0.08,
    "sharpe": -1.92,
    "fitness": 0.42,
    "turnover": 0.35,
    "checks": [
        {"name": "LOW_SHARPE", "result": "FAIL", "limit": 1.58, "value": -1.92},
    ],
}

# Mirrors a locally stored alpha whose judged submission checks all pass.
CLEAN_PASS: Final[dict[str, Any]] = {
    "alpha_id": "AH_TEST_0003",
    "expression": "rank(ts_mean(close, 20))",
    "region": "USA",
    "delay": 1,
    "universe": "TOP3000",
    "neutralization": "MARKET",
    "decay": 6,
    "truncation": 0.05,
    "sharpe": 2.10,
    "fitness": 1.45,
    "turnover": 0.42,
    "checks": [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.58, "value": 2.10},
        {"name": "LOW_FITNESS", "result": "PASS", "limit": 1.0, "value": 1.45},
        {"name": "HIGH_TURNOVER", "result": "PASS", "limit": 0.70, "value": 0.42},
        {"name": "CONCENTRATED_WEIGHT", "result": "PASS", "limit": None, "value": None},
    ],
}

_WIRE_SETTINGS: Final[dict[str, Any]] = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "delay": 1,
    "universe": "TOP3000",
    "neutralization": "MARKET",
    "decay": 4,
    "truncation": 0.08,
}
_WIRE_STATS: Final[dict[str, Any]] = {
    "sharpe": 1.72,
    "fitness": 1.10,
    "turnover": 0.42,
    "checks": [
        {"name": "LOW_SHARPE", "result": "PASS", "limit": 1.58, "value": 1.72},
    ],
}
_WIRE_REGULAR: Final[dict[str, Any]] = {
    "id": "AH_PAGE_REGULAR",
    "type": "REGULAR",
    "settings": _WIRE_SETTINGS,
    "regular": {"code": "rank(close)", "operatorCount": 1},
    "dateCreated": "2026-09-19T08:00:00Z",
    "status": "UNSUBMITTED",
    "is": _WIRE_STATS,
}
_WIRE_RA_PARENT: Final[dict[str, Any]] = {
    **_WIRE_REGULAR,
    "id": "AH_PAGE_RA_PARENT",
    "type": "RA_PARENT",
    "regular": {"code": "rank(ts_mean(close, 20))", "operatorCount": 2},
    "dateCreated": "2026-09-19T07:59:00Z",
}
_WIRE_RA_CHILD: Final[dict[str, Any]] = {
    **_WIRE_REGULAR,
    "id": "AH_PAGE_RA_CHILD",
    "type": "RA_CHILD",
    "regular": {"code": "rank(ts_mean(close, 40))", "operatorCount": 2},
    "dateCreated": "2026-09-19T07:58:00Z",
}
_WIRE_UNKNOWN: Final[dict[str, Any]] = {
    **_WIRE_REGULAR,
    "id": "AH_PAGE_UNKNOWN",
    "type": "SOMETHING_THE_PLATFORM_ADDED_LATER",
    "dateCreated": "2026-09-19T07:57:00Z",
}

# Mirrors the current platform page containing REGULAR and recursive-alpha variants.
PAGE_REGULAR_RA_PARENT_RA_CHILD: Final[dict[str, Any]] = {
    "count": 3,
    "results": [_WIRE_REGULAR, _WIRE_RA_PARENT, _WIRE_RA_CHILD],
}

# Mirrors a forward-compatible page where one new enum value must not discard valid rows.
PAGE_WITH_UNKNOWN_TYPE: Final[dict[str, Any]] = {
    "count": 3,
    "results": [_WIRE_REGULAR, _WIRE_RA_PARENT, _WIRE_UNKNOWN],
}

# Mirrors a full BRAIN page that exercises backfill.py's MAX_OFFSET empty-page guard.
PAGE_ALL_UNPARSEABLE: Final[dict[str, Any]] = {
    "count": 100,
    "results": [
        {
            **_WIRE_UNKNOWN,
            "id": f"AH_PAGE_UNKNOWN_{index:03d}",
            "dateCreated": f"2026-09-18T23:{index % 60:02d}:00Z",
        }
        for index in range(100)
    ],
}
