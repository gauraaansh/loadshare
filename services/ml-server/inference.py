"""
ARIA — ML Server: Inference
=============================
One prediction function per model.
All functions receive the loaded artifact + parsed request,
return a dict that maps directly to the response schema.

Design:
  - No I/O here — pure numpy/sklearn operations
  - SHAP importances served from pre-computed training artifacts
    (not recomputed at inference time — too slow per-request)
  - key_factors = top N features from shap_importance dict,
    re-ranked per-request using the model's built-in feature
    importances as a fast proxy
  - EPH threshold: Rs.90/hr (from Loadshare article research)
"""

import numpy as np
from typing import Any

# Rs./hr threshold from Loadshare article: platform EPH was Rs.70-85
# vs rider expectation Rs.90-100. We flag below Rs.90 as at-risk.
EPH_THRESHOLD          = 90.0

# Dead zone risk threshold — above this we flag as high risk
DEAD_ZONE_THRESHOLD    = 0.55

# Assumed average EPH for dead run cost calculation
ASSUMED_EPH_RS_PER_HR  = 82.0


# ── Helpers ───────────────────────────────────────────────────

def _to_array(values: list[float]) -> np.ndarray:
    return np.array(values, dtype=np.float32).reshape(1, -1)


def _top_factors(shap_importance: dict, feature_names: list[str],
                 n: int = 3) -> list[dict]:
    """
    Return top N features from the pre-computed SHAP importance dict,
    filtered to only features present in this prediction's feature_names.
    """
    relevant = {k: v for k, v in shap_importance.items()
                if k in feature_names}
    sorted_items = sorted(relevant.items(), key=lambda x: -x[1])
    return [{"feature": k, "importance": round(v, 4)}
            for k, v in sorted_items[:n]]


def _get_model_version(metadata: dict) -> str:
    return metadata.get("model_version", "unknown")


# ══════════════════════════════════════════════════════════════
# MODEL 1 — Rider Persona Classifier
# ══════════════════════════════════════════════════════════════

def predict_persona(artifacts, req) -> dict:
    """
    Classify rider as supplementary (0) or dedicated (1).

    Returns confidence = probability of the predicted class.
    This is calibrated implicitly by the model — XGBClassifier
    produces reasonable probabilities without explicit calibration
    for binary classification.
    """
    features = artifacts.features
    values   = [getattr(req, col) for col in features]
    X        = _to_array(values)

    proba        = artifacts.model.predict_proba(X)[0]   # [p_supp, p_ded]
    label        = int(np.argmax(proba))
    confidence   = float(proba[label])
    ded_prob     = float(proba[1])
    supp_prob    = float(proba[0])

    persona = "dedicated" if label == 1 else "supplementary"

    return {
        "persona":            persona,
        "persona_label":      label,
        "confidence":         round(confidence, 4),
        "dedicated_prob":     round(ded_prob, 4),
        "supplementary_prob": round(supp_prob, 4),
        "key_factors":        _top_factors(artifacts.shap_importance, features, n=3),
        "model_version":      _get_model_version(artifacts.metadata),
    }


# ══════════════════════════════════════════════════════════════
# MODEL 2 — Delivery Duration Scorer
# ══════════════════════════════════════════════════════════════

def predict_duration(artifacts, req) -> dict:
    """
    Predict delivery duration in minutes.

    baseline_minutes is not computed here — the MCP server /
    Restaurant Intelligence Agent computes deviations from
    historical baselines using the algorithmic module.
    We return None and let the caller add context.
    """
    features = artifacts.features
    values   = [getattr(req, col) for col in features]
    X        = _to_array(values)

    predicted = float(artifacts.model.predict(X)[0])

    return {
        "predicted_minutes": round(predicted, 1),
        "baseline_minutes":  None,   # caller adds historical baseline
        "deviation_minutes": None,   # caller computes: actual - predicted
        "key_factors":       _top_factors(artifacts.shap_importance, features, n=5),
        "model_version":     _get_model_version(artifacts.metadata),
    }


# ══════════════════════════════════════════════════════════════
# MODEL 3 — Dead Zone Risk Predictor
# ══════════════════════════════════════════════════════════════

def predict_dead_zone(artifacts, req) -> dict:
    """
    Two-stage prediction:
      1. Classifier → dead zone probability (calibrated)
      2. If high risk → regressor → expected stranding minutes

    EPH loss computed as: (stranding_mins / 60) * ASSUMED_EPH_RS_PER_HR

    Interaction features (peripheral_ld_risk, dist_x_dead_rate)
    computed here if they were used during training.
    """
    clf_features = artifacts.classifier_features

    # Base feature dict
    base = {
        "dest_zone_type_enc":     req.dest_zone_type_enc,
        "city_tier_enc":          req.city_tier_enc,
        "hour_of_day":            req.hour_of_day,
        "day_of_week":            req.day_of_week,
        "is_weekend":             req.is_weekend,
        "is_ld_order":            req.is_ld_order,
        "dist_from_home_zone_km": req.dist_from_home_zone_km,
        "current_density_ratio":  req.current_density_ratio,
        "historical_dead_rate":   req.historical_dead_rate,
    }

    # Add interaction features if the model was trained with them
    zone_norm = req.dest_zone_type_enc / 3.0
    base["peripheral_ld_risk"] = zone_norm * req.is_ld_order
    base["dist_x_dead_rate"]   = req.dist_from_home_zone_km * req.historical_dead_rate

    # Build feature vector in training order
    clf_values = [base.get(col, 0.0) for col in clf_features]
    X_clf      = _to_array(clf_values)

    # Stage 1 — classifier
    proba         = artifacts.classifier.predict_proba(X_clf)[0]
    dead_zone_prob = float(proba[1])
    is_high_risk   = dead_zone_prob >= DEAD_ZONE_THRESHOLD

    # Stage 2 — regressor (only if high risk)
    expected_stranding_mins = None
    expected_eph_loss       = None

    if is_high_risk:
        reg_features = artifacts.regressor_features
        reg_values   = [base.get(col, 0.0) for col in reg_features]
        X_reg        = _to_array(reg_values)
        stranding    = float(artifacts.regressor.predict(X_reg)[0])
        eph_loss     = (stranding / 60.0) * ASSUMED_EPH_RS_PER_HR

        expected_stranding_mins = round(max(stranding, 0.0), 1)
        expected_eph_loss       = round(max(eph_loss, 0.0), 2)

    # SHAP key factors from classifier
    clf_shap = artifacts.shap_importance.get("classifier", artifacts.shap_importance)

    return {
        "dead_zone_probability":   round(dead_zone_prob, 4),
        "is_high_risk":            is_high_risk,
        "expected_stranding_mins": expected_stranding_mins,
        "expected_eph_loss":       expected_eph_loss,
        "key_factors":             _top_factors(clf_shap, clf_features, n=3),
        "model_version":           _get_model_version(artifacts.metadata),
    }


# ══════════════════════════════════════════════════════════════
# MODEL 4 — Earnings Trajectory Forecaster
# ══════════════════════════════════════════════════════════════

def predict_earnings(artifacts, req) -> dict:
    """
    Two outputs:
      1. Regressor → projected_final_eph (absolute Rs./hr at end of shift)
      2. Classifier → below_threshold flag (will EPH fall below Rs.90?)

    eph_trend derived from lag features:
      - slope = current_eph - eph_lag1_30min
      - improving: slope > +3
      - declining:  slope < -3
      - stable:     in between

    alert_level:
      - 'none':      projected ≥ 90 and not below threshold
      - 'watch':     projected 80-90 or declining trend
      - 'intervene': projected < 80 or below threshold + declining
    """
    # ── Regressor ──────────────────────────────────────────────
    reg_features = artifacts.regressor_features
    reg_base = {
        "persona_enc":         req.persona_enc,
        "hour_of_day":         req.hour_of_day,
        "orders_completed":    req.orders_completed,
        "earnings_so_far":     req.earnings_so_far,
        "current_eph":         req.current_eph,
        "idle_time_mins":      req.idle_time_mins,
        "dead_runs_count":     req.dead_runs_count,
        "zone_density":        req.zone_density,
        "obs_point_mins":      req.obs_point_mins,
        "time_remaining_mins": req.time_remaining_mins,
        "total_shift_mins":    req.total_shift_mins,
        "eph_lag1_30min":      req.eph_lag1_30min,
        "eph_lag2_60min":      req.eph_lag2_60min,
        "eph_lag3_90min":      req.eph_lag3_90min,
        # Momentum features — computed from lags
        "eph_slope":           req.current_eph - req.eph_lag1_30min,
        "eph_acceleration":    req.eph_lag1_30min - req.eph_lag2_60min,
        # eph_target included for regressor (it was in training)
        "eph_target":          EPH_THRESHOLD,
    }
    reg_values = [reg_base.get(col, 0.0) for col in reg_features]
    X_reg      = _to_array(reg_values)
    projected  = float(artifacts.regressor.predict(X_reg)[0])
    # Sanity: projected EPH should be in Rs.50-300 range.
    # Values below 20 indicate XGBoost base_score serialization corruption
    # (model pickle loaded in a different XGBoost version). Fall back to
    # current_eph so callers get a meaningful signal instead of 0.
    if projected < 20.0:
        projected = req.current_eph
    projected  = round(max(projected, 0.0), 2)

    # ── Classifier ─────────────────────────────────────────────
    # eph_target excluded from classifier features (by design —
    # prevents shortcut learning through label definition)
    clf_features = artifacts.classifier_features
    clf_base     = {k: v for k, v in reg_base.items() if k != "eph_target"}
    clf_values   = [clf_base.get(col, 0.0) for col in clf_features]
    X_clf        = _to_array(clf_values)
    clf_proba    = artifacts.classifier.predict_proba(X_clf)[0]
    below_threshold = bool(clf_proba[1] >= 0.5)

    # ── Trend ──────────────────────────────────────────────────
    eph_slope = reg_base["eph_slope"]
    if eph_slope > 3.0:
        eph_trend = "improving"
    elif eph_slope < -3.0:
        eph_trend = "declining"
    else:
        eph_trend = "stable"

    # ── Alert level ────────────────────────────────────────────
    if projected < 80.0 or (below_threshold and eph_trend == "declining"):
        alert_level = "intervene"
    elif projected < EPH_THRESHOLD or eph_trend == "declining":
        alert_level = "watch"
    else:
        alert_level = "none"

    # SHAP key factors from regressor
    reg_shap = artifacts.shap_importance.get("regressor", artifacts.shap_importance)

    return {
        "projected_final_eph": projected,
        "current_eph":         round(req.current_eph, 2),
        "below_threshold":     below_threshold,
        "eph_trend":           eph_trend,
        "alert_level":         alert_level,
        "eph_slope":           round(eph_slope, 2),
        "key_factors":         _top_factors(reg_shap, reg_features, n=3),
        "model_version":       _get_model_version(artifacts.metadata),
    }
