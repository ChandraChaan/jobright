"""Learning from results: tracks that get interviews rank higher next time.

Uses only your own outcomes (the statuses you set on the dashboard: interview,
offer, rejected). The effect is bounded and needs enough data first, so one
lucky or unlucky week does not reshape the search.
"""

from __future__ import annotations

from jobright import store

MIN_TRACK = 8        # applications in a track before it is judged
MIN_TOTAL = 15       # applications overall before anything is judged
BOUND = 0.15         # a track's priority moves at most ±15%


def _rate(t: dict[str, int]) -> float:
    return (t["interview"] + t["offer"]) / t["applied"] if t["applied"] else 0.0


def factors(outcomes: dict[str, dict[str, int]] | None = None) -> dict[str, float]:
    """Priority multiplier per track: 1.0 until there is enough evidence."""
    outcomes = store.track_outcomes() if outcomes is None else outcomes
    total = sum(t["applied"] for t in outcomes.values())
    if total < MIN_TOTAL:
        return {}
    overall = sum(t["interview"] + t["offer"] for t in outcomes.values()) / total
    out = {}
    for name, t in outcomes.items():
        if t["applied"] >= MIN_TRACK:
            f = (_rate(t) + 0.05) / (overall + 0.05)
            out[name] = round(max(1 - BOUND, min(1 + BOUND, f)), 3)
    return out


def summary() -> list[dict]:
    """Rows for the dashboard: results by track, best response rate first."""
    outcomes = store.track_outcomes()
    f = factors(outcomes)
    rows = [{"track": k, **v, "rate": round(100 * _rate(v)), "factor": f.get(k, 1.0)} for k, v in outcomes.items()]
    return sorted(rows, key=lambda r: (-r["rate"], -r["applied"]))
