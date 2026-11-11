"""Small bounded timing summaries; call outside the acquisition path."""
import statistics


def percentiles(values) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {"count": len(ordered), "p50_ms": statistics.median(ordered),
            "p95_ms": ordered[min(len(ordered)-1, int(0.95*len(ordered)))], "max_ms": ordered[-1]}
