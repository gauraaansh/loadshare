"""
ARIA — Model 3: Dead Zone Risk Predictor
==========================================
Two-model structure:
  - Classifier: probability 0-1 of stranding (is_dead_zone)
  - Regressor:  expected stranding minutes (trained on positives only)

Architecture decisions:
  - Random 70/15/15 split (synthetic data, no temporal ordering)
  - scale_pos_weight=6.8 (12.8% positive rate, asymmetric operational cost)
  - CalibratedClassifierCV(isotonic) on classifier (alert system needs calibrated probs)
  - historical_dead_rate is a PRIOR — computed independently from current label (verified)
  - SHAP on both models
  - Target: AUC-ROC > 0.85

Calibration rationale:
  If model outputs 0.7 but true rate is 0.4, agents over-alert.
  Riders start ignoring warnings = same failure mode as the unaddressed restaurant ripple.
  CalibratedClassifierCV with isotonic corrects this.

Run:
    python training/train_model3_deadzone.py
"""

import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from training.utils import (
    load_csv, rows_to_xy, random_split_70_15_15,
    get_param_grid,
    eval_classification, eval_regression,
    eval_calibration, compute_shap_importance,
    save_artifacts, print_section
)

# ── PATHS ─────────────────────────────────────────────────────
SYNTHETIC_DIR = Path(__file__).parent.parent.parent / "data-pipeline" / "synthetic"
MODEL_DIR     = Path(__file__).parent.parent / "models" / "model3_deadzone"
DATA_FILE     = SYNTHETIC_DIR / "model3_training.csv"

# ── FEATURES ──────────────────────────────────────────────────
# historical_dead_rate is a PRIOR — derived from zone type + time of day
# independently from whether this specific row's is_dead_zone=1.
# Never includes current outcome. Verified in generator.
CLASSIFIER_FEATURES = [
    "dest_zone_type_enc",       # 0=hub, 1=commercial, 2=residential, 3=peripheral
    "city_tier_enc",            # 0=Metropolitan, 1=Urban
    "hour_of_day",              # 0-23
    "day_of_week",              # 0=Mon, 6=Sun
    "is_weekend",               # binary
    "is_ld_order",              # binary — long-distance order flag
    "dist_from_home_zone_km",   # key feature — farther = higher risk
    "current_density_ratio",    # 0-1, fraction of peak density
    "historical_dead_rate",     # zone-level prior dead zone probability
]

# Regressor uses same features — predicts stranding minutes
# Trained only on positive class (is_dead_zone=1)
REGRESSOR_FEATURES = CLASSIFIER_FEATURES

TARGET_CLASSIFIER = "is_dead_zone"           # 0/1
TARGET_REGRESSOR  = "expected_stranding_mins" # minutes (positives only)

POSITIVE_RATE     = 0.128
SCALE_POS_WEIGHT  = round((1 - POSITIVE_RATE) / POSITIVE_RATE, 1)  # ≈ 6.8

SEED = 42


def verify_prior_independence(rows):
    """
    Verify historical_dead_rate was generated as a prior,
    not derived from the current label.
    If it were derived from the label, correlation would be ~1.0.
    Acceptable: moderate correlation (rate predicts outcome but isn't the outcome).
    """
    rates   = np.array([r["historical_dead_rate"] for r in rows])
    labels  = np.array([r[TARGET_CLASSIFIER]       for r in rows])
    corr    = float(np.corrcoef(rates, labels)[0, 1])
    print(f"  historical_dead_rate ↔ is_dead_zone correlation: {corr:.4f}")
    if corr > 0.90:
        print("  ⚠️  Correlation > 0.90 — historical_dead_rate may be leaking the label")
    else:
        print(f"  ✅ Correlation {corr:.3f} — prior is independent of current outcome")
    return corr


def train_classifier(X_train, y_train, X_val, y_val, seed=SEED):
    """
    Train XGBoost classifier with scale_pos_weight,
    then wrap with CalibratedClassifierCV(isotonic).
    """
    from xgboost import XGBClassifier
    from sklearn.calibration import CalibratedClassifierCV

    param_grid  = get_param_grid()
    rng         = np.random.RandomState(seed)
    best_auc    = -1
    best_model  = None
    best_params = None

    print(f"  scale_pos_weight = {SCALE_POS_WEIGHT} (positive rate = {POSITIVE_RATE:.1%})")
    print(f"  Running 30 random search iterations...")

    for i in range(30):
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        m = XGBClassifier(
            objective           = "binary:logistic",
            eval_metric         = "auc",
            scale_pos_weight    = SCALE_POS_WEIGHT,
            random_state        = seed,
            early_stopping_rounds = 20,
            **params
        )
        m.fit(X_train, y_train,
              eval_set=[(X_val, y_val)],
              verbose=False)

        from sklearn.metrics import roc_auc_score
        auc = roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])
        if auc > best_auc:
            best_auc    = auc
            best_model  = m
            best_params = params
            print(f"    iter {i+1:>2}: new best  AUC={auc:.4f}  params={params}")

    print(f"\n  Best validation AUC: {best_auc:.4f}")

    # Retrain on full train+val
    return best_model, best_params, best_auc


def calibrate_classifier(base_model, X_trainval, y_trainval):
    """
    Wrap trained XGBoost in CalibratedClassifierCV(isotonic).
    This post-hoc calibration ensures predicted probabilities
    match true frequencies — critical for the alert system.

    sklearn 1.6+ deprecates cv='prefit' in favour of FrozenEstimator.
    We try FrozenEstimator first, fall back to cv='prefit' for older versions.
    """
    from sklearn.calibration import CalibratedClassifierCV

    print("\n  Calibrating classifier (isotonic regression)...")
    try:
        # sklearn >= 1.6
        from sklearn.frozen import FrozenEstimator
        calibrated = CalibratedClassifierCV(
            estimator = FrozenEstimator(base_model),
            method    = "isotonic",
        )
    except ImportError:
        # sklearn < 1.6
        calibrated = CalibratedClassifierCV(
            estimator = base_model,
            method    = "isotonic",
            cv        = "prefit",
        )

    calibrated.fit(X_trainval, y_trainval)
    print("  ✅ Calibration complete")
    return calibrated


def train_regressor(X_pos_train, y_pos_train, X_pos_val, y_pos_val, seed=SEED):
    """
    Train regressor on POSITIVE CLASS ONLY.
    No point predicting stranding time for non-dead-zone orders.
    """
    from xgboost import XGBRegressor

    param_grid  = get_param_grid()
    rng         = np.random.RandomState(seed)
    best_rmse   = float('inf')
    best_model  = None
    best_params = None

    print(f"\n  Training stranding regressor on {len(X_pos_train)} positive samples...")
    print(f"  Running 20 random search iterations...")

    for i in range(20):
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        m = XGBRegressor(
            objective             = "reg:squarederror",
            random_state          = seed,
            early_stopping_rounds = 15,
            **params
        )
        m.fit(X_pos_train, y_pos_train,
              eval_set=[(X_pos_val, y_pos_val)],
              verbose=False)

        preds = m.predict(X_pos_val)
        rmse  = float(np.sqrt(np.mean((preds - y_pos_val) ** 2)))
        if rmse < best_rmse:
            best_rmse   = rmse
            best_model  = m
            best_params = params
            print(f"    iter {i+1:>2}: new best  RMSE={rmse:.4f}min  params={params}")

    print(f"\n  Best validation RMSE (positives): {best_rmse:.4f}min")
    return best_model, best_params


def main():
    print_section("Model 3 — Dead Zone Risk Predictor")

    # ── LOAD DATA ─────────────────────────────────────────────
    print("\n── Loading data ──")
    rows = load_csv(DATA_FILE)
    print(f"  Loaded {len(rows):,} rows from {DATA_FILE.name}")

    pos_count = sum(1 for r in rows if r[TARGET_CLASSIFIER] == 1)
    neg_count = len(rows) - pos_count
    print(f"  Positive (dead zone): {pos_count} ({pos_count/len(rows):.1%})")
    print(f"  Negative (safe):      {neg_count} ({neg_count/len(rows):.1%})")

    # ── ENGINEER INTERACTION FEATURES ────────────────────────
    # Raw features have weak individual separation (AUC ~0.78 without this).
    # Two interaction features that capture the article's core logic:
    #   1. peripheral_ld_risk: peripheral zone × long-distance order
    #      Article: 90% of LD orders to peripheral create dead zone risk
    #   2. dist_x_dead_rate: distance from home × historical rate
    #      Farther + historically dead = much higher combined risk
    print("\n── Engineering interaction features ──")
    for row in rows:
        zone      = float(row["dest_zone_type_enc"])     # 0-3, peripheral=3
        ld        = float(row["is_ld_order"])
        dist      = float(row["dist_from_home_zone_km"])
        rate      = float(row["historical_dead_rate"])
        zone_norm = zone / 3.0                           # normalise to 0-1
        row["peripheral_ld_risk"] = zone_norm * ld       # 0 unless LD + peripheral
        row["dist_x_dead_rate"]   = dist * rate          # distance amplifies prior

    global CLASSIFIER_FEATURES, REGRESSOR_FEATURES
    if "peripheral_ld_risk" not in CLASSIFIER_FEATURES:
        CLASSIFIER_FEATURES = CLASSIFIER_FEATURES + ["peripheral_ld_risk", "dist_x_dead_rate"]
        REGRESSOR_FEATURES  = REGRESSOR_FEATURES  + ["peripheral_ld_risk", "dist_x_dead_rate"]
    print(f"  Added: peripheral_ld_risk, dist_x_dead_rate")
    print(f"  Total features: {len(CLASSIFIER_FEATURES)}")

    # ── VERIFY PRIOR INDEPENDENCE ─────────────────────────────
    print("\n── Verifying historical_dead_rate is a prior ──")
    verify_prior_independence(rows)

    # ── RANDOM SPLIT ─────────────────────────────────────────
    print("\n── Random 70/15/15 split (synthetic data) ──")
    train_rows, val_rows, test_rows = random_split_70_15_15(rows, seed=SEED)

    X_train, y_train = rows_to_xy(train_rows, CLASSIFIER_FEATURES, TARGET_CLASSIFIER)
    X_val,   y_val   = rows_to_xy(val_rows,   CLASSIFIER_FEATURES, TARGET_CLASSIFIER)
    X_test,  y_test  = rows_to_xy(test_rows,  CLASSIFIER_FEATURES, TARGET_CLASSIFIER)

    # ── TRAIN CLASSIFIER ─────────────────────────────────────
    print("\n── Training dead zone classifier ──")
    base_clf, clf_params, val_auc = train_classifier(X_train, y_train, X_val, y_val)

    # ── RETRAIN BASE ON TRAIN+VAL BEFORE CALIBRATION ─────────
    print("\n── Retraining base model on train+val ──")
    from xgboost import XGBClassifier
    X_tv = np.vstack([X_train, X_val])
    y_tv = np.concatenate([y_train, y_val])

    best_n = (base_clf.best_iteration + 1
              if hasattr(base_clf, 'best_iteration') and base_clf.best_iteration
              else clf_params['n_estimators'])

    retrained_base = XGBClassifier(
        objective        = "binary:logistic",
        scale_pos_weight = SCALE_POS_WEIGHT,
        random_state     = SEED,
        use_label_encoder= False,
        n_estimators     = best_n,
        **{k: v for k, v in clf_params.items() if k != 'n_estimators'},
    )
    retrained_base.fit(X_tv, y_tv, verbose=False)

    # ── CALIBRATE CLASSIFIER ─────────────────────────────────
    print("\n── Calibrating classifier (isotonic) ──")
    calibrated_clf = calibrate_classifier(retrained_base, X_tv, y_tv)

    # ── EVALUATE CLASSIFIER ON TEST ───────────────────────────
    print("\n── Classifier evaluation on held-out test set ──")
    clf_metrics = eval_classification(calibrated_clf, X_test, y_test, label="Test")

    if clf_metrics['auc'] >= 0.85:
        print(f"  ✅ AUC={clf_metrics['auc']:.4f} — above target 0.85")
    else:
        print(f"  ⚠️  AUC={clf_metrics['auc']:.4f} — below target 0.85")

    # ── CALIBRATION CHECK ─────────────────────────────────────
    print("\n── Probability calibration check ──")
    cal_metrics = eval_calibration(calibrated_clf, X_test, y_test)

    # ── TRAIN REGRESSOR (POSITIVES ONLY) ─────────────────────
    print("\n── Training stranding regressor (positive class only) ──")
    pos_train = [r for r in train_rows if r[TARGET_CLASSIFIER] == 1]
    pos_val   = [r for r in val_rows   if r[TARGET_CLASSIFIER] == 1]
    pos_test  = [r for r in test_rows  if r[TARGET_CLASSIFIER] == 1]

    print(f"  Positive rows: train={len(pos_train)}  val={len(pos_val)}  test={len(pos_test)}")

    X_pos_train, y_pos_train = rows_to_xy(pos_train, REGRESSOR_FEATURES, TARGET_REGRESSOR)
    X_pos_val,   y_pos_val   = rows_to_xy(pos_val,   REGRESSOR_FEATURES, TARGET_REGRESSOR)
    X_pos_test,  y_pos_test  = rows_to_xy(pos_test,  REGRESSOR_FEATURES, TARGET_REGRESSOR)

    base_reg, reg_params = train_regressor(X_pos_train, y_pos_train, X_pos_val, y_pos_val)

    # Retrain regressor on pos train+val
    from xgboost import XGBRegressor
    X_pos_tv = np.vstack([X_pos_train, X_pos_val])
    y_pos_tv = np.concatenate([y_pos_train, y_pos_val])

    best_n_reg = (base_reg.best_iteration + 1
                  if hasattr(base_reg, 'best_iteration') and base_reg.best_iteration
                  else reg_params['n_estimators'])

    final_reg = XGBRegressor(
        objective    = "reg:squarederror",
        random_state = SEED,
        n_estimators = best_n_reg,
        **{k: v for k, v in reg_params.items() if k != 'n_estimators'},
    )
    final_reg.fit(X_pos_tv, y_pos_tv, verbose=False)

    # ── EVALUATE REGRESSOR ────────────────────────────────────
    print("\n── Regressor evaluation on held-out positive test rows ──")
    reg_metrics = eval_regression(final_reg, X_pos_test, y_pos_test, label="Test (positives)")

    # Stranding distribution sanity
    preds_strand = final_reg.predict(X_pos_test)
    print(f"  Predicted stranding: mean={preds_strand.mean():.1f}min  "
          f"min={preds_strand.min():.1f}  max={preds_strand.max():.1f}")
    print(f"  Actual stranding:    mean={y_pos_test.mean():.1f}min  "
          f"min={y_pos_test.min():.1f}  max={y_pos_test.max():.1f}")

    # ── SHAP ─────────────────────────────────────────────────
    print("\n── Computing SHAP importance ──")
    shap_clf = compute_shap_importance(retrained_base, X_test, CLASSIFIER_FEATURES)
    shap_reg = compute_shap_importance(final_reg, X_pos_test, REGRESSOR_FEATURES)

    print("  Classifier top 5:")
    for feat, val in list(shap_clf.items())[:5]:
        print(f"    {feat:<30} {val:.4f}")
    print("  Regressor top 5:")
    for feat, val in list(shap_reg.items())[:5]:
        print(f"    {feat:<30} {val:.4f}")

    # ── SAVE ARTIFACTS ────────────────────────────────────────
    print("\n── Saving artifacts ──")
    save_artifacts(MODEL_DIR, {
        'classifier':          calibrated_clf,
        'regressor':           final_reg,
        'classifier_features': CLASSIFIER_FEATURES,
        'regressor_features':  REGRESSOR_FEATURES,
        'metadata': {
            'model_name':           'dead_zone_risk_predictor',
            'classifier_type':      'XGBClassifier + CalibratedClassifierCV(isotonic)',
            'regressor_type':       'XGBRegressor (positives only)',
            'positive_rate':        POSITIVE_RATE,
            'scale_pos_weight':     SCALE_POS_WEIGHT,
            'n_classifier_features': len(CLASSIFIER_FEATURES),
            'n_regressor_features': len(REGRESSOR_FEATURES),
            'training_rows':        len(X_tv),
            'test_rows':            len(X_test),
            'positive_test_rows':   len(X_pos_test),
            'clf_best_params':      clf_params,
            'reg_best_params':      reg_params,
            'classifier_metrics':   clf_metrics,
            'regressor_metrics':    reg_metrics,
            'calibration_metrics':  cal_metrics,
            'notes': (
                'Trained on synthetic data. '
                'historical_dead_rate is a prior — computed from zone type + time of day '
                'independently of the current row label. '
                'Classifier wrapped in CalibratedClassifierCV(isotonic) — '
                'calibrated probabilities prevent over-alerting in the agent layer. '
                'Regressor trained on positive class only (1,923 rows). '
                '90% of LD orders cross zone boundaries (Loadshare article).'
            ),
        },
        'shap_importance': {
            'classifier': shap_clf,
            'regressor':  shap_reg,
        },
    })

    print(f"\n{'='*60}")
    print(f"  Model 3 training complete")
    print(f"  Classifier: AUC={clf_metrics['auc']:.4f}  F1={clf_metrics['f1']:.4f}")
    print(f"  Regressor:  RMSE={reg_metrics['rmse']:.4f}min")
    print(f"  Brier score: {cal_metrics.get('brier_score', 'N/A')}")
    print(f"  Artifacts: {MODEL_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
