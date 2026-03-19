"""
ARIA — Model 2: Delivery Duration Risk Scorer
===============================================
Predicts total delivery time in minutes.
Used as baseline for restaurant ripple detection:
  deviation = actual_time - model_prediction
  Consistent positive deviation per restaurant = ripple signal

Architecture decisions:
  - XGBoost regressor
  - TIME-BASED 70/15/15 split (real data has temporal structure)
  - Coordinates dropped (city_name_enc + City_enc sufficient)
  - Restaurant feature skipped (no restaurant IDs in dataset)
  - SHAP for deviation explanation
  - Target: RMSE 5-7min, R² 0.75-0.82

Run:
    python training/train_model2_duration.py
"""

import sys
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from training.utils import (
    load_csv, rows_to_xy, time_split_70_15_15,
    get_param_grid,
    eval_regression, compute_shap_importance,
    save_artifacts, print_section
)

# ── PATHS ─────────────────────────────────────────────────────
DATA_DIR  = Path(__file__).parent.parent.parent / "data-pipeline"
MODEL_DIR = Path(__file__).parent.parent / "models" / "model2_duration"

# Try multiple possible locations for train_final.csv
DATA_CANDIDATES = [
    DATA_DIR / "train_final.csv",
    Path(__file__).parent.parent.parent / "data-pipeline" / "kaggle_data" / "train_final.csv",
    Path("train_final.csv"),
]

# ── FEATURES ──────────────────────────────────────────────────
# Raw coordinates DROPPED — city identity already in city_name_enc + City_enc
# Using raw lat/lng would make model memorise specific pickup points
FEATURE_COLS = [
    # Order features (strongest signal)
    "distance_km",             # haversine distance — strongest predictor
    "Road_traffic_density_enc",# 0=Low, 1=Med, 2=High, 3=Jam
    "order_hour",              # hour of day — peak vs off-peak
    "is_lunch_peak",           # binary flag: 12-14h
    "is_dinner_peak",          # binary flag: 18-21h
    "is_weekend",              # binary flag
    "day_of_week",             # 0=Mon, 6=Sun
    "month",                   # seasonal patterns
    # Geography (city tier, not raw coords)
    "City_enc",                # 0=Urban, 1=Metropolitan, 2=Semi-Urban
    "city_name_enc",           # 0-21 city identifier
    # Order characteristics
    "Weatherconditions_enc",   # 0-5 weather conditions
    "Type_of_vehicle_enc",     # 0=bicycle, 1=e-scooter, 2=scooter, 3=motorcycle
    "Type_of_order_enc",       # 0=Snack, 1=Drinks, 2=Meal, 3=Buffet
    "Festival_enc",            # 0=No, 1=Yes
    "multiple_deliveries",     # 0-3 simultaneous orders
    # Rider attributes (weaker but real signal from dataset)
    "Delivery_person_Age",
    "Delivery_person_Ratings",
    "Vehicle_condition",       # 0-3 condition score
]
TARGET_COL = "Time_taken(min)"

# Date column for time-based split
DATE_COL = "Order_Date"

SEED = 42


def find_data_file():
    """Find train_final.csv — could be in several places."""
    for candidate in DATA_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"train_final.csv not found. Checked:\n" +
        "\n".join(f"  {c}" for c in DATA_CANDIDATES) +
        "\n\nRun: python notebooks/02_preprocess_clean.py first"
    )


def validate_features(rows):
    """Check all feature columns exist and have no missing values."""
    missing_cols = [c for c in FEATURE_COLS if c not in rows[0]]
    if missing_cols:
        raise ValueError(f"Missing feature columns: {missing_cols}")

    for col in FEATURE_COLS + [TARGET_COL]:
        n_missing = sum(1 for r in rows if str(r.get(col, '')).strip() in ('', 'nan', 'NaN'))
        if n_missing > 0:
            print(f"  ⚠️  {col}: {n_missing} missing values — will be imputed with median")

    print(f"  ✅ All {len(FEATURE_COLS)} feature columns present")


def add_date_column_if_missing(rows):
    """
    train_final.csv dropped Order_Date during preprocessing.
    We reconstruct a sort key from day/month columns for time-based split.
    """
    for row in rows:
        if DATE_COL not in row:
            # Reconstruct YYYY-MM-DD from day, month (year is always 2022 in dataset)
            try:
                m = int(float(row.get('month', 1)))
                d = int(float(row.get('day', 1)))
                row[DATE_COL] = f"2022-{m:02d}-{d:02d}"
            except:
                row[DATE_COL] = "2022-01-01"
    return rows


def impute_missing(rows, feature_cols):
    """Median imputation for any remaining missing values."""
    medians = {}
    for col in feature_cols:
        vals = [float(r[col]) for r in rows
                if str(r.get(col,'')).strip() not in ('','nan','NaN')]
        if vals:
            medians[col] = sorted(vals)[len(vals)//2]
        else:
            medians[col] = 0.0

    for row in rows:
        for col in feature_cols:
            if str(row.get(col,'')).strip() in ('','nan','NaN'):
                row[col] = medians[col]
    return rows, medians


def main():
    print_section("Model 2 — Delivery Duration Risk Scorer")

    # ── LOAD DATA ─────────────────────────────────────────────
    print("\n── Loading data ──")
    data_file = find_data_file()
    rows = load_csv(data_file)
    print(f"  Loaded {len(rows):,} rows from {data_file.name}")

    validate_features(rows)
    rows = add_date_column_if_missing(rows)
    rows, medians = impute_missing(rows, FEATURE_COLS)

    # Target stats
    targets = [float(r[TARGET_COL]) for r in rows]
    print(f"  Target: mean={sum(targets)/len(targets):.1f}min  "
          f"range=[{min(targets):.0f}, {max(targets):.0f}]")

    # ── TIME-BASED SPLIT ──────────────────────────────────────
    print("\n── Time-based 70/15/15 split (chronological) ──")
    print("  Sorting by Order_Date — train=oldest, test=most recent")
    train_rows, val_rows, test_rows = time_split_70_15_15(rows, DATE_COL)

    # Quick sanity: check target distribution is similar across splits
    for name, split in [("train", train_rows), ("val", val_rows), ("test", test_rows)]:
        t = [float(r[TARGET_COL]) for r in split]
        print(f"  {name}: mean={sum(t)/len(t):.1f}min  "
              f"range=[{min(t):.0f},{max(t):.0f}]")

    X_train, y_train = rows_to_xy(train_rows, FEATURE_COLS, TARGET_COL)
    X_val,   y_val   = rows_to_xy(val_rows,   FEATURE_COLS, TARGET_COL)
    X_test,  y_test  = rows_to_xy(test_rows,  FEATURE_COLS, TARGET_COL)

    # ── HYPERPARAMETER SEARCH ─────────────────────────────────
    print("\n── Randomised hyperparameter search (30 iterations) ──")
    from xgboost import XGBRegressor

    param_grid  = get_param_grid()
    rng         = np.random.RandomState(SEED)
    best_rmse   = float('inf')
    best_model  = None
    best_params = None

    print(f"  Running 30 random search iterations...")
    for i in range(30):
        params = {k: rng.choice(v) for k, v in param_grid.items()}
        m = XGBRegressor(
            objective       = 'reg:squarederror',
            random_state    = SEED,
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

    # ── RETRAIN ON TRAIN+VAL ──────────────────────────────────
    print("\n── Retraining on train+val with best params ──")
    X_trainval = np.vstack([X_train, X_val])
    y_trainval = np.concatenate([y_train, y_val])

    best_n_estimators = (
        best_model.best_iteration + 1
        if hasattr(best_model, 'best_iteration') and best_model.best_iteration
        else best_params['n_estimators']
    )

    final_model = XGBRegressor(
        objective    = 'reg:squarederror',
        random_state = SEED,
        n_estimators = best_n_estimators,
        **{k: v for k, v in best_params.items() if k != 'n_estimators'},
    )
    final_model.fit(X_trainval, y_trainval, verbose=False)

    # ── EVALUATE ON TEST SET ──────────────────────────────────
    print("\n── Final evaluation on held-out test set ──")
    metrics = eval_regression(final_model, X_test, y_test, label="Test")

    # Target check
    if metrics['rmse'] <= 7.0 and metrics['r2'] >= 0.75:
        print(f"  ✅ RMSE={metrics['rmse']:.2f}  R²={metrics['r2']:.3f} — within target range")
    else:
        print(f"  ⚠️  RMSE={metrics['rmse']:.2f}  R²={metrics['r2']:.3f} — outside target range")
        print(f"     Target: RMSE ≤ 7.0, R² ≥ 0.75")

    # Note: RMSE has a floor ~2-3 min due to integer target resolution
    print(f"  Note: Target has integer resolution — RMSE floor ~2-3min is expected")

    # Per-tier performance (check model works across city tiers)
    print("\n  Performance by city tier:")
    tier_groups = {}
    for row, pred in zip(test_rows, final_model.predict(X_test)):
        tier = int(float(row.get('City_enc', 0)))
        tier_name = {0: 'Urban', 1: 'Metropolitan', 2: 'Semi-Urban'}.get(tier, str(tier))
        if tier_name not in tier_groups:
            tier_groups[tier_name] = {'actual': [], 'pred': []}
        tier_groups[tier_name]['actual'].append(float(row[TARGET_COL]))
        tier_groups[tier_name]['pred'].append(float(pred))

    for tier_name, vals in sorted(tier_groups.items()):
        actual = np.array(vals['actual'])
        pred   = np.array(vals['pred'])
        rmse   = float(np.sqrt(np.mean((pred - actual) ** 2)))
        print(f"    {tier_name:<15} RMSE={rmse:.2f}min  n={len(actual)}")

    # ── SHAP IMPORTANCE ───────────────────────────────────────
    print("\n── Computing SHAP feature importance ──")
    shap_importance = compute_shap_importance(final_model, X_test, FEATURE_COLS)
    print("  Top 5 SHAP features:")
    for feat, val in list(shap_importance.items())[:5]:
        print(f"    {feat:<35} {val:.4f}")

    # ── SAVE ARTIFACTS ────────────────────────────────────────
    print("\n── Saving artifacts ──")

    # Load label encoders from preprocessing
    encoders = {}
    encoders_file = DATA_DIR / "label_encoders.json"
    if encoders_file.exists():
        import json
        with open(encoders_file) as f:
            encoders = json.load(f)

    save_artifacts(MODEL_DIR, {
        'model':    final_model,
        'features': FEATURE_COLS,
        'encoders': encoders,
        'metadata': {
            'model_name':     'delivery_duration_scorer',
            'model_type':     'XGBRegressor',
            'target':         TARGET_COL,
            'target_unit':    'minutes',
            'n_features':     len(FEATURE_COLS),
            'training_rows':  len(X_trainval),
            'test_rows':      len(X_test),
            'best_params':    best_params,
            'best_n_estimators': best_n_estimators,
            'metrics':        metrics,
            'feature_medians': {c: float(v) for c, v in medians.items()},
            'split_type':     'time_based_chronological',
            'notes': (
                'Trained on real food delivery dataset (41,953 rows, 22 Indian cities). '
                'Time-based split: train=oldest 70%, test=most recent 15%. '
                'Used as baseline for restaurant ripple detection: '
                'deviation = actual - predicted. '
                'Consistent positive deviation per restaurant signals the restaurant ripple.'
            ),
        },
        'shap_importance': shap_importance,
    })

    print(f"\n{'='*60}")
    print(f"  Model 2 training complete")
    print(f"  RMSE={metrics['rmse']:.4f}  MAE={metrics['mae']:.4f}  R²={metrics['r2']:.4f}")
    print(f"  Artifacts: {MODEL_DIR}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
