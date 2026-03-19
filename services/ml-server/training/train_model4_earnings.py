"""
ARIA — Model 4: Earnings Trajectory Forecaster
================================================
Two-model structure:
  - Regressor:   projected_final_eph (continuous)
  - Classifier:  below_threshold (binary alert flag)

Architecture decisions:
  - Random 70/15/15 split (synthetic data — no real temporal ordering)
  - eph_target EXCLUDED from classifier features (would let model shortcut
    through the label definition instead of learning trajectory patterns)
  - eph_target INCLUDED in regressor features (predicts absolute EPH value)
  - Momentum features added: eph_slope, eph_acceleration
  - Interaction feature time_remaining × current_eph SKIPPED (trees learn this)
  - No calibration on classifier (binary flag — threshold matters, not probability magnitude)
  - SHAP on both models
  - Target: Regressor RMSE reasonable, Classifier F1 > 0.80

Momentum rationale (from Loadshare article):
  A rider at Rs.83 with declining lags (91→88→86→83) heading 2hrs more
  will finish ~Rs.75. Same rider with flat lags and 30min left finishes ~Rs.83.
  eph_slope and eph_acceleration make this pattern explicit.

Run:
    python training/train_model4_earnings.py
"""

import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from training.utils import (
    load_csv, rows_to_xy, random_split_70_15_15,
    get_param_grid,
    eval_regression, eval_classification,
    compute_shap_importance,
    save_artifacts, print_section
)

# ── PATHS ─────────────────────────────────────────────────────
SYNTHETIC_DIR = Path(__file__).parent.parent.parent / "data-pipeline" / "synthetic"
MODEL_DIR     = Path(__file__).parent.parent / "models" / "model4_earnings"
DATA_FILE     = SYNTHETIC_DIR / "model4_training.csv"

# ── FEATURES ──────────────────────────────────────────────────
# Momentum features (derived, added at load time):
#   eph_slope        = current_eph - eph_lag1_30min     (direction of change)
#   eph_acceleration = eph_lag1_30min - eph_lag2_60min  (rate of change of rate)

# REGRESSOR features — includes eph_target
# Predicts the actual EPH value, so knowing the target is informative context
REGRESSOR_FEATURES = [
    "persona_enc",              # 0=supplementary, 1=dedicated
    "hour_of_day",              # affects zone density / order availability
    "orders_completed",         # throughput so far
    "earnings_so_far",          # absolute earnings accumulated
    "current_eph",              # current earnings rate
    "idle_time_mins",           # wasted time at restaurants + between orders
    "dead_runs_count",          # number of dead zone trips
    "zone_density",             # current zone order availability
    "obs_point_mins",           # how far into shift (observation time)
    "time_remaining_mins",      # time left in shift
    "total_shift_mins",         # total planned shift length
    "eph_lag1_30min",           # EPH 30min ago
    "eph_lag2_60min",           # EPH 60min ago
    "eph_lag3_90min",           # EPH 90min ago
    "eph_slope",                # current_eph - eph_lag1 (momentum)
    "eph_acceleration",         # eph_lag1 - eph_lag2 (rate of change)
    "eph_target",               # Rs.95 supplementary / Rs.110 dedicated
]

# CLASSIFIER features — eph_target EXCLUDED
# If eph_target is included and below_threshold = projected < eph_target,
# the classifier shortcuts: "high eph_target → predict below_threshold=1"
# That's not learning trajectory — it's reading the label definition.
CLASSIFIER_FEATURES = [
    "persona_enc",
    "hour_of_day",
    "orders_completed",
    "earnings_so_far",
    "current_eph",
    "idle_time_mins",
    "dead_runs_count",
    "zone_density",
    "obs_point_mins",
    "time_remaining_mins",
    "total_shift_mins",
    "eph_lag1_30min",
    "eph_lag2_60min",
    "eph_lag3_90min",
    "eph_slope",                # momentum
    "eph_acceleration",         # acceleration
    # eph_target intentionally excluded
]

TARGET_REGRESSOR   = "projected_final_eph"
TARGET_CLASSIFIER  = "below_threshold"

SEED = 42


def add_momentum_features(rows):
    """
    Add eph_slope and eph_acceleration.
    slope        = current_eph - eph_lag1_30min
    acceleration = eph_lag1_30min - eph_lag2_60min

    Struggling riders: slope < 0, acceleration < 0 (speeding up decline)
    Healthy riders:    slope ≈ 0, acceleration ≈ 0 (stable)
    """
    for row in rows:
        row["eph_slope"]        = float(row["current_eph"]) - float(row["eph_lag1_30min"])
        row["eph_acceleration"] = float(row["eph_lag1_30min"]) - float(row["eph_lag2_60min"])
    return rows


def verify_no_shortcut(rows):
    """
    Verify eph_target is not in CLASSIFIER_FEATURES.
    Also check that below_threshold correlates with trajectory, not just target level.
    """
    assert "eph_target" not in CLASSIFIER_FEATURES, \
        "eph_target must not be in classifier features — label shortcut risk"

    # Sanity: do momentum features have signal?
    below = [r for r in rows if r[TARGET_CLASSIFIER] == 1]
    above = [r for r in rows if r[TARGET_CLASSIFIER] == 0]

    def avg(rs, col): return sum(float(r[col]) for r in rs) / len(rs)

    print(f"  Momentum signal check:")
    print(f"    eph_slope:        below={avg(below,'eph_slope'):.2f}  above={avg(above,'eph_slope'):.2f}")
    print(f"    eph_acceleration: below={avg(below,'eph_acceleration'):.2f}  above={avg(above,'eph_acceleration'):.2f}")
    print(f"    current_eph:      below={avg(below,'current_eph'):.1f}  above={avg(above,'current_eph'):.1f}")
    print(f"  ✅ eph_target excluded from classifier features")


def train_regressor(X_train, y_train, X_val, y_val, seed=SEED):
    from xgboost import XGBRegressor

    param_grid  = get_param_grid()
    rng         = np.random.RandomState(seed)
    best_rmse   = float('inf')
    best_model  = None
    best_params = None

    print(f"  Running 30 random search iterations...")
    for i in range(30):
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        m = XGBRegressor(
            objective             = "reg:squarederror",
            random_state          = seed,
            early_stopping_rounds = 20,
            **params
        )
        m.fit(X_train, y_train,
              eval_set=[(X_val, y_val)],
              verbose=False)

        preds = m.predict(X_val)
        rmse  = float(np.sqrt(np.mean((preds - y_val) ** 2)))
        if rmse < best_rmse:
            best_rmse   = rmse
            best_model  = m
            best_params = params
            print(f"    iter {i+1:>2}: new best  RMSE={rmse:.4f}  params={params}")

    print(f"\n  Best validation RMSE: {best_rmse:.4f}")
    return best_model, best_params


def train_classifier(X_train, y_train, X_val, y_val, seed=SEED):
    from xgboost import XGBClassifier
    from sklearn.metrics import roc_auc_score

    param_grid  = get_param_grid()
    rng         = np.random.RandomState(seed)
    best_auc    = -1
    best_model  = None
    best_params = None

    below_rate = y_train.mean()
    print(f"  below_threshold rate in train: {below_rate:.1%}")
    print(f"  Running 30 random search iterations...")

    for i in range(30):
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        m = XGBClassifier(
            objective             = "binary:logistic",
            eval_metric           = "logloss",
            random_state          = seed,
            early_stopping_rounds = 20,
            **params
        )
        m.fit(X_train, y_train,
              eval_set=[(X_val, y_val)],
              verbose=False)

        auc = roc_auc_score(y_val, m.predict_proba(X_val)[:, 1])
        if auc > best_auc:
            best_auc    = auc
            best_model  = m
            best_params = params
            print(f"    iter {i+1:>2}: new best  AUC={auc:.4f}  params={params}")

    print(f"\n  Best validation AUC: {best_auc:.4f}")
    return best_model, best_params


def main():
    print_section("Model 4 — Earnings Trajectory Forecaster")

    # ── LOAD DATA ─────────────────────────────────────────────
    print("\n── Loading data ──")
    rows = load_csv(DATA_FILE)
    print(f"  Loaded {len(rows):,} rows from {DATA_FILE.name}")

    below_count = sum(1 for r in rows if r[TARGET_CLASSIFIER] == 1)
    print(f"  below_threshold: {below_count} ({below_count/len(rows):.1%})")

    # ── ADD MOMENTUM FEATURES ─────────────────────────────────
    print("\n── Adding momentum features ──")
    rows = add_momentum_features(rows)

    slopes = [r["eph_slope"] for r in rows]
    accs   = [r["eph_acceleration"] for r in rows]
    print(f"  eph_slope:        mean={sum(slopes)/len(slopes):.2f}  "
          f"min={min(slopes):.2f}  max={max(slopes):.2f}")
    print(f"  eph_acceleration: mean={sum(accs)/len(accs):.2f}  "
          f"min={min(accs):.2f}  max={max(accs):.2f}")

    # ── VERIFY NO SHORTCUT ────────────────────────────────────
    print("\n── Verifying classifier setup ──")
    verify_no_shortcut(rows)

    # ── RANDOM SPLIT ─────────────────────────────────────────
    print("\n── Random 70/15/15 split (synthetic data) ──")
    train_rows, val_rows, test_rows = random_split_70_15_15(rows, seed=SEED)

    # ── REGRESSOR ─────────────────────────────────────────────
    print("\n── Training EPH regressor ──")
    X_train_r, y_train_r = rows_to_xy(train_rows, REGRESSOR_FEATURES, TARGET_REGRESSOR)
    X_val_r,   y_val_r   = rows_to_xy(val_rows,   REGRESSOR_FEATURES, TARGET_REGRESSOR)
    X_test_r,  y_test_r  = rows_to_xy(test_rows,  REGRESSOR_FEATURES, TARGET_REGRESSOR)

    base_reg, reg_params = train_regressor(X_train_r, y_train_r, X_val_r, y_val_r)

    # Retrain on train+val
    from xgboost import XGBRegressor, XGBClassifier
    X_tv_r = np.vstack([X_train_r, X_val_r])
    y_tv_r = np.concatenate([y_train_r, y_val_r])

    best_n_reg = (base_reg.best_iteration + 1
                  if hasattr(base_reg, 'best_iteration') and base_reg.best_iteration
                  else reg_params['n_estimators'])

    final_reg = XGBRegressor(
        objective    = "reg:squarederror",
        random_state = SEED,
        n_estimators = best_n_reg,
        **{k: v for k, v in reg_params.items() if k != 'n_estimators'},
    )
    final_reg.fit(X_tv_r, y_tv_r, verbose=False)

    # ── EVALUATE REGRESSOR ────────────────────────────────────
    print("\n── Regressor evaluation on held-out test set ──")
    reg_metrics = eval_regression(final_reg, X_test_r, y_test_r, label="Test")

    # Distribution sanity: do predictions cluster in right ranges?
    preds_eph = final_reg.predict(X_test_r)
    test_below = [r[TARGET_CLASSIFIER] == 1 for r in test_rows]
    preds_below = preds_eph[test_below]
    preds_above = preds_eph[[not b for b in test_below]]
    print(f"\n  Predicted EPH by actual class:")
    print(f"    Struggling (below_threshold=1): mean={preds_below.mean():.1f}  "
          f"range=[{preds_below.min():.1f}, {preds_below.max():.1f}]")
    print(f"    Healthy    (below_threshold=0): mean={preds_above.mean():.1f}  "
          f"range=[{preds_above.min():.1f}, {preds_above.max():.1f}]")
    print(f"  Article check: struggling should cluster Rs.70-85, healthy Rs.90+")

    # ── CLASSIFIER ────────────────────────────────────────────
    print("\n── Training below_threshold classifier ──")
    X_train_c, y_train_c = rows_to_xy(train_rows, CLASSIFIER_FEATURES, TARGET_CLASSIFIER)
    X_val_c,   y_val_c   = rows_to_xy(val_rows,   CLASSIFIER_FEATURES, TARGET_CLASSIFIER)
    X_test_c,  y_test_c  = rows_to_xy(test_rows,  CLASSIFIER_FEATURES, TARGET_CLASSIFIER)

    base_clf, clf_params = train_classifier(X_train_c, y_train_c, X_val_c, y_val_c)

    # Retrain on train+val
    X_tv_c = np.vstack([X_train_c, X_val_c])
    y_tv_c = np.concatenate([y_train_c, y_val_c])

    best_n_clf = (base_clf.best_iteration + 1
                  if hasattr(base_clf, 'best_iteration') and base_clf.best_iteration
                  else clf_params['n_estimators'])

    final_clf = XGBClassifier(
        objective         = "binary:logistic",
        random_state      = SEED,
        use_label_encoder = False,
        n_estimators      = best_n_clf,
        **{k: v for k, v in clf_params.items() if k != 'n_estimators'},
    )
    final_clf.fit(X_tv_c, y_tv_c, verbose=False)

    # ── EVALUATE CLASSIFIER ───────────────────────────────────
    print("\n── Classifier evaluation on held-out test set ──")
    clf_metrics = eval_classification(final_clf, X_test_c, y_test_c, label="Test")

    if clf_metrics['f1'] >= 0.80:
        print(f"  ✅ F1={clf_metrics['f1']:.4f} — above target 0.80")
    else:
        print(f"  ⚠️  F1={clf_metrics['f1']:.4f} — below target 0.80")

    # ── SHAP ─────────────────────────────────────────────────
    print("\n── Computing SHAP importance ──")
    shap_reg = compute_shap_importance(final_reg, X_test_r, REGRESSOR_FEATURES)
    shap_clf = compute_shap_importance(final_clf, X_test_c, CLASSIFIER_FEATURES)

    print("  Regressor top 5:")
    for feat, val in list(shap_reg.items())[:5]:
        print(f"    {feat:<30} {val:.4f}")
    print("  Classifier top 5:")
    for feat, val in list(shap_clf.items())[:5]:
        print(f"    {feat:<30} {val:.4f}")

    # Verify eph_slope appears in top features (momentum should matter)
    top_reg_feats = list(shap_reg.keys())[:5]
    top_clf_feats = list(shap_clf.keys())[:5]
    if "eph_slope" in top_reg_feats or "eph_slope" in top_clf_feats:
        print("  ✅ eph_slope in top 5 — momentum signal is being used")
    else:
        print("  ⚠️  eph_slope not in top 5 — check if momentum features are adding value")

    # ── SAVE ARTIFACTS ────────────────────────────────────────
    print("\n── Saving artifacts ──")
    save_artifacts(MODEL_DIR, {
        'regressor':           final_reg,
        'classifier':          final_clf,
        'regressor_features':  REGRESSOR_FEATURES,
        'classifier_features': CLASSIFIER_FEATURES,
        'metadata': {
            'model_name':              'earnings_trajectory_forecaster',
            'regressor_type':          'XGBRegressor',
            'classifier_type':         'XGBClassifier (no calibration — binary flag)',
            'n_regressor_features':    len(REGRESSOR_FEATURES),
            'n_classifier_features':   len(CLASSIFIER_FEATURES),
            'training_rows':           len(X_tv_r),
            'test_rows':               len(X_test_r),
            'below_threshold_rate':    float(below_count / len(rows)),
            'reg_best_params':         reg_params,
            'clf_best_params':         clf_params,
            'regressor_metrics':       reg_metrics,
            'classifier_metrics':      clf_metrics,
            'eph_ranges': {
                'supplementary_target': 95.0,
                'dedicated_target':     110.0,
                'crisis_min':           70.0,
                'crisis_max':           85.0,
            },
            'notes': (
                'Trained on synthetic data grounded in Loadshare article. '
                'eph_target excluded from classifier features to prevent shortcut learning. '
                'Momentum features (eph_slope, eph_acceleration) added explicitly. '
                'Two separate models: regressor for EPH projection, '
                'classifier for binary alert flag. '
                'Article ground truth: struggling riders Rs.70-85, target Rs.90-110.'
            ),
        },
        'shap_importance': {
            'regressor':  shap_reg,
            'classifier': shap_clf,
        },
    })

    print(f"\n{'='*60}")
    print(f"  Model 4 training complete")
    print(f"  Regressor:  RMSE={reg_metrics['rmse']:.4f}  R²={reg_metrics['r2']:.4f}")
    print(f"  Classifier: F1={clf_metrics['f1']:.4f}  AUC={clf_metrics['auc']:.4f}")
    print(f"  Artifacts: {MODEL_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
