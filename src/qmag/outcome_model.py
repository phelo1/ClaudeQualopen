"""A small, auditable outcome model with real fitted weights.

This is supervised return prediction, not neural or reinforcement learning.
Its deployment is handled by the same prospective experiment controller as
parameter changes. Only information available at entry is a feature.
"""
from __future__ import annotations

import hashlib
import json
import math
import numpy as np
import pandas as pd

FEATURES = ("rvol", "adr_pct", "depth", "gap_pct", "stop_pct", "theme_pct", "edge_score", "signal_score")
SOURCE_WEIGHT = {"broker_verified": 1.0, "paper_fill": 0.5, "shadow": 0.15, "estimated": 0.0}


def vector(features: dict) -> list[float]:
    values = []
    for key in FEATURES:
        value = features.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = float("nan")
        values.append(value if math.isfinite(value) else float("nan"))
    return values


def predict(model: dict | None, features: dict) -> float | None:
    if not model or model.get("features") != list(FEATURES):
        return None
    try:
        x = np.asarray(vector(features))
        mean, scale = np.asarray(model["mean"]), np.asarray(model["scale"])
        x = np.where(np.isfinite(x), x, mean)
        z = np.clip((x - mean) / scale, -5, 5)
        y = float(model["intercept"] + z @ np.asarray(model["weights"]))
        return float(np.clip(y, -3, 5)) if math.isfinite(y) else None
    except (ValueError, TypeError, KeyError):
        return None


def train(closed: list[dict], shadows: list[dict], minimum: int = 80) -> dict:
    from .shadow_quality import geometry_error
    records = []
    for row in [*closed, *[dict(s, evidence="shadow") for s in shadows if s.get("status") == "closed" and geometry_error(s) is None]]:
        evidence = row.get("evidence", "estimated")
        weight = SOURCE_WEIGHT.get(evidence, 0)
        try:
            entered = pd.Timestamp(row.get("entry_date") or row.get("date"))
            resolved = pd.Timestamp(row.get("closed_on") or row.get("resolved_on"))
            reward = float(row["r_multiple"])
            if pd.isna(entered) or pd.isna(resolved) or resolved < entered or not math.isfinite(reward):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        if weight and row.get("features"):
            records.append((entered.date(), resolved.date(), row, weight))
    records.sort(key=lambda r: r[0])
    if len(records) < minimum:
        return {"status": "collecting", "records": len(records), "required": minimum}
    cutoff = records[int(len(records) * 0.75)][0]
    fitting = [r for r in records if r[1] < cutoff]
    # Shadows may help fit; promotion validation needs actual paper/live records.
    testing = [r for r in records if r[0] >= cutoff and r[2].get("evidence") in ("paper_fill", "broker_verified")]
    if len(fitting) < 40 or len(testing) < 20:
        return {"status": "collecting", "reason": "Need 40 purged training and 20 later non-shadow outcomes", "records": len(records)}
    x = np.asarray([vector(r[2]["features"]) for r in fitting])
    finite = np.isfinite(x)
    mean = np.divide(np.where(finite, x, 0).sum(axis=0), finite.sum(axis=0), out=np.zeros(len(FEATURES)), where=finite.sum(axis=0)>0)
    x = np.where(finite, x, mean)
    scale = np.maximum(x.std(axis=0), 1e-6)
    x = np.column_stack([np.ones(len(x)), np.clip((x-mean)/scale, -5, 5)])
    y = np.clip([r[2]["r_multiple"] for r in fitting], -5, 10)
    weights = np.asarray([r[3] for r in fitting])
    penalty = np.eye(x.shape[1]) * 10.0
    penalty[0, 0] = 0
    coefficients = np.linalg.solve(x.T @ (weights[:, None] * x) + penalty, x.T @ (weights * y))
    model = {"features": list(FEATURES), "mean": mean.tolist(), "scale": scale.tolist(),
             "intercept": float(coefficients[0]), "weights": coefficients[1:].tolist(),
             "trained_through": str(max(r[1] for r in fitting)), "validation_from": str(cutoff),
             "training_rows": len(fitting), "validation_rows": len(testing),
             "sources": {k: sum(r[2].get("evidence") == k for r in fitting) for k in SOURCE_WEIGHT}}
    predicted = np.asarray([predict(model, r[2]["features"]) for r in testing])
    actual = np.asarray([r[2]["r_multiple"] for r in testing])
    baseline = float(np.average(y, weights=weights))
    base_error = float(np.mean((actual-baseline)**2))
    error = float(np.mean((actual-predicted)**2))
    model.update(validation_mse=error, baseline_mse=base_error,
                 status="candidate" if base_error > 0 and error < base_error * 0.95 else "rejected")
    model["id"] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()[:16]
    return model
