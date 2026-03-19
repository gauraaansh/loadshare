"""
ARIA — Shared Training Utilities
==================================
Used by all four model training scripts.
Handles: splits, SHAP, evaluation, saving artifacts.
"""

import json
import os
import csv
import joblib
import numpy as np
from datetime import datetime
from pathlib import Path

# ── PATHS ────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
MODELS_DIR = ROOT / "models"
DATA_DIR   = ROOT.parent / "data-pipeline"


# ══════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════

def load_csv(path):
    """Load CSV to list of dicts, auto-convert numerics."""
    rows = list(csv.DictReader(open(path)))
    # Auto-convert to float where possible
    converted = []
    for row in rows:
        new_row = {}
        for k, v in row.items():
            try:
                new_row[k] = float(v)
            except (ValueError, TypeError):
                new_row[k] = v
        converted.append(new_row)
    return converted


def rows_to_xy(rows, feature_cols, target_col):
    """Convert list of dicts to numpy X, y arrays."""
    X = np.array([[row[col] for col in feature_cols] for row in rows], dtype=np.float32)
    y = np.array([row[target_col] for row in rows], dtype=np.float32)
    return X, y


# ══════════════════════════════════════════════════════════════
# SPLITS
# ══════════════════════════════════════════════════════════════

def random_split_70_15_15(rows, seed=42):
    """
    70/15/15 random split.
    Use for: Models 1, 3, 4 (synthetic data, no real temporal ordering).
    """
    rng = np.random.RandomState(seed)
    idx = rng.permutation(len(rows))
    n   = len(rows)
    t   = int(n * 0.70)
    v   = int(n * 0.85)

    train = [rows[i] for i in idx[:t]]
    val   = [rows[i] for i in idx[t:v]]
    test  = [rows[i] for i in idx[v:]]

    print(f"  Split → train={len(train):,}  val={len(val):,}  test={len(test):,}")
    return train, val, test


def time_split_70_15_15(rows, date_col):
    """
    Chronological 70/15/15 split.
    Use for: Model 2 (real data with Order_Date).
    Sorts by date_col ascending, then cuts at 70/85%.
    """
    # Sort chronologically
    sorted_rows = sorted(rows, key=lambda r: str(r.get(date_col, '')))
    n = len(sorted_rows)
    t = int(n * 0.70)
    v = int(n * 0.85)

    train = sorted_rows[:t]
    val   = sorted_rows[t:v]
    test  = sorted_rows[v:]

    print(f"  Time split → train={len(train):,}  val={len(val):,}  test={len(test):,}")
    return train, val, test


def stratified_split_70_15_15(rows, label_col, seed=42):
    """
    Stratified 70/15/15 — preserves class balance in each split.
    Use for: Model 1 (80/20 class imbalance needs preservation).
    """
    from collections import defaultdict
    rng = np.random.RandomState(seed)

    # Group by label
    by_label = defaultdict(list)
    for row in rows:
        by_label[row[label_col]].append(row)

    train, val, test = [], [], []
    for label, group in by_label.items():
        idx = rng.permutation(len(group))
        n   = len(group)
        t   = int(n * 0.70)
        v   = int(n * 0.85)
        train += [group[i] for i in idx[:t]]
        val   += [group[i] for i in idx[t:v]]
        test  += [group[i] for i in idx[v:]]

    # Shuffle each split (don't want all class-0 then all class-1)
    for split in [train, val, test]:
        rng.shuffle(split)

    print(f"  Stratified split → train={len(train):,}  val={len(val):,}  test={len(test):,}")
    return train, val, test


# ══════════════════════════════════════════════════════════════
# HYPERPARAMETER SEARCH
# ══════════════════════════════════════════════════════════════

def get_param_grid():
    """Shared XGBoost hyperparameter search space."""
    return {
        'max_depth':        [3, 4, 5, 6],
        'learning_rate':    [0.05, 0.1, 0.2],
        'n_estimators':     [100, 200, 300],
        'subsample':        [0.8, 1.0],
        'colsample_bytree': [0.8, 1.0],
        'min_child_weight': [1, 3, 5],
    }


def random_search(model_class, param_grid, X_train, y_train, X_val, y_val,
                  n_iter=30, seed=42, early_stopping_rounds=20,
                  eval_metric='rmse', model_kwargs=None):
    """
    RandomizedSearch over param_grid.
    Uses validation set for early stopping (not test set).
    Returns best model and best params.
    """
    rng = np.random.RandomState(seed)
    best_score = float('inf')
    best_model = None
    best_params = None

    # Build list of random param combinations
    param_keys = list(param_grid.keys())
    combinations = []
    for _ in range(n_iter):
        combo = {k: rng.choice(param_grid[k]) for k in param_keys}
        combinations.append(combo)

    print(f"  Running {n_iter} random search iterations...")

    for i, params in enumerate(combinations):
        kwargs = dict(
            random_state=seed,
            early_stopping_rounds=early_stopping_rounds,
            eval_metric=eval_metric,
            **(model_kwargs or {})
        )
        kwargs.update(params)

        model = model_class(**kwargs)
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            verbose=False,
        )

        # Score on validation set
        if hasattr(model, 'predict_proba'):
            from sklearn.metrics import roc_auc_score
            score = 1 - roc_auc_score(y_val, model.predict_proba(X_val)[:, 1])
        else:
            preds = model.predict(X_val)
            score = float(np.sqrt(np.mean((preds - y_val) ** 2)))

        if score < best_score:
            best_score = score
            best_model = model
            best_params = params
            print(f"    iter {i+1:>2}: new best  score={score:.4f}  params={params}")

    print(f"  Best validation score: {best_score:.4f}")
    print(f"  Best params: {best_params}")
    return best_model, best_params


# ══════════════════════════════════════════════════════════════
# SHAP EXPLAINABILITY
# ══════════════════════════════════════════════════════════════


def _patch_xgb_booster(model):
    """
    XGBoost >= 2.0 stores base_score as '[9.294E1]' (bracket notation).
    SHAP's TreeExplainer calls float() on this directly and crashes.

    Fix: extract the booster, patch base_score in the JSON config to a
    plain float string, reload the config, return the patched booster.
    Also handles CalibratedClassifierCV wrappers.
    """
    import re, json as _json

    # Unwrap CalibratedClassifierCV — get the inner XGB estimator
    if hasattr(model, 'calibrated_classifiers_'):
        inner = getattr(model, 'estimator', None)
        if inner is None:
            inner = model.calibrated_classifiers_[0].estimator
        model = inner

    # Must have get_booster to be an XGB sklearn wrapper
    if not hasattr(model, 'get_booster'):
        return model   # already a raw booster or unknown — pass through

    booster = model.get_booster()

    try:
        cfg      = booster.save_config()
        cfg_dict = _json.loads(cfg)
        lmp      = cfg_dict.get('learner', {}).get('learner_model_param', {})

        if 'base_score' in lmp:
            raw     = str(lmp['base_score'])
            cleaned = re.sub(r'[\[\]]', '', raw).strip()   # '[9.294E1]' → '9.294E1'
            try:
                float(cleaned)           # validate it parses cleanly
                lmp['base_score'] = cleaned
                booster.load_config(_json.dumps(cfg_dict))
            except ValueError:
                pass                     # leave as-is if still unparseable
    except Exception:
        pass    # config patching is best-effort

    return booster


def compute_shap_importance(model, X, feature_names, n_samples=500):
    """
    Compute SHAP values for a sample of rows.
    Returns mean |SHAP| per feature (global importance).
    Used for key_factors_json in DB.

    Compatible with XGBoost 1.x and 2.x.
    """
    try:
        import shap

        if len(X) > n_samples:
            idx      = np.random.choice(len(X), n_samples, replace=False)
            X_sample = X[idx]
        else:
            X_sample = X

        # Patch base_score before SHAP reads it
        patched      = _patch_xgb_booster(model)
        explainer    = shap.TreeExplainer(patched)
        shap_values  = explainer.shap_values(X_sample)

        # Binary classifiers may return list [neg_class, pos_class]
        if isinstance(shap_values, list):
            shap_values = shap_values[1]

        # Some XGB versions return shape (n, features, 1)
        if hasattr(shap_values, 'ndim') and shap_values.ndim == 3:
            shap_values = shap_values[:, :, 0]

        mean_abs   = np.abs(shap_values).mean(axis=0)
        importance = dict(zip(feature_names, mean_abs.tolist()))
        importance = dict(sorted(importance.items(), key=lambda x: -x[1]))
        return importance

    except ImportError:
        print("  SHAP not installed — using built-in feature importance")
        return get_builtin_importance(model, feature_names)
    except Exception as e:
        print(f"  SHAP failed ({e}) — falling back to built-in importance")
        return get_builtin_importance(model, feature_names)


def get_builtin_importance(model, feature_names):
    """Fallback: built-in XGBoost feature importance."""
    scores = model.feature_importances_
    importance = dict(zip(feature_names, scores.tolist()))
    return dict(sorted(importance.items(), key=lambda x: -x[1]))


def top_n_features(importance_dict, n=5):
    """Return top N features as list of {feature, importance} dicts."""
    items = list(importance_dict.items())[:n]
    return [{"feature": k, "importance": round(v, 4)} for k, v in items]


# ══════════════════════════════════════════════════════════════
# EVALUATION
# ══════════════════════════════════════════════════════════════

def eval_regression(model, X_test, y_test, label="Test"):
    preds = model.predict(X_test)
    rmse  = float(np.sqrt(np.mean((preds - y_test) ** 2)))
    mae   = float(np.mean(np.abs(preds - y_test)))
    ss_res = np.sum((y_test - preds) ** 2)
    ss_tot = np.sum((y_test - np.mean(y_test)) ** 2)
    r2    = float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0

    print(f"\n  [{label}] RMSE={rmse:.3f}  MAE={mae:.3f}  R²={r2:.4f}")
    return {"rmse": rmse, "mae": mae, "r2": r2}


def eval_classification(model, X_test, y_test, label="Test"):
    from sklearn.metrics import (
        f1_score, roc_auc_score, precision_score,
        recall_score, confusion_matrix
    )

    preds      = model.predict(X_test)
    proba      = model.predict_proba(X_test)[:, 1]

    f1         = float(f1_score(y_test, preds, average='weighted'))
    auc        = float(roc_auc_score(y_test, proba))
    precision  = float(precision_score(y_test, preds, average='weighted', zero_division=0))
    recall     = float(recall_score(y_test, preds, average='weighted', zero_division=0))
    cm         = confusion_matrix(y_test, preds).tolist()

    print(f"\n  [{label}] F1={f1:.4f}  AUC={auc:.4f}  Precision={precision:.4f}  Recall={recall:.4f}")
    print(f"  Confusion matrix: {cm}")
    return {"f1": f1, "auc": auc, "precision": precision, "recall": recall, "confusion_matrix": cm}


def eval_calibration(model, X_test, y_test):
    """
    Check probability calibration.
    Bins predictions into deciles, compares predicted vs actual rate.
    """
    try:
        from sklearn.calibration import calibration_curve
        proba = model.predict_proba(X_test)[:, 1]
        fraction_pos, mean_pred = calibration_curve(y_test, proba, n_bins=10)

        print("\n  Calibration check (predicted → actual):")
        for pred, actual in zip(mean_pred, fraction_pos):
            bar = "✅" if abs(pred - actual) < 0.10 else "⚠️ "
            print(f"    {bar} predicted={pred:.2f} → actual={actual:.2f}")

        from sklearn.metrics import brier_score_loss
        brier = float(brier_score_loss(y_test, proba))
        print(f"  Brier score: {brier:.4f}  (0=perfect, 0.25=random)")
        return {"brier_score": brier}
    except Exception as e:
        print(f"  Calibration check failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════
# SAVING ARTIFACTS
# ══════════════════════════════════════════════════════════════

class _NumpyEncoder(json.JSONEncoder):
    """
    JSON encoder that handles numpy types.
    XGBoost best_params and metrics contain numpy int64/float32/float64
    which standard json.dump rejects. This converts them to native Python types.
    """
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
        return super().default(obj)


def _json_dump(obj, f, indent=2):
    """json.dump with numpy type support."""
    json.dump(obj, f, indent=indent, cls=_NumpyEncoder)


def save_artifacts(model_dir, artifacts: dict):
    """
    Save all model artifacts.
    artifacts = {
        'model':        trained model (joblib),
        'features':     list of feature names (json),
        'encoders':     dict of label encoders (json, optional),
        'metadata':     dict with metrics, version, etc. (json),
        'shap_importance': dict (json),
    }
    """
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)

    saved = []

    if 'model' in artifacts:
        path = model_dir / "model.joblib"
        joblib.dump(artifacts['model'], path)
        saved.append(str(path))

    if 'classifier' in artifacts:
        path = model_dir / "classifier.joblib"
        joblib.dump(artifacts['classifier'], path)
        saved.append(str(path))

    if 'regressor' in artifacts:
        path = model_dir / "regressor.joblib"
        joblib.dump(artifacts['regressor'], path)
        saved.append(str(path))

    if 'features' in artifacts:
        path = model_dir / "features.json"
        with open(path, 'w') as f:
            _json_dump(artifacts['features'], f, indent=2)
        saved.append(str(path))

    if 'classifier_features' in artifacts:
        path = model_dir / "classifier_features.json"
        with open(path, 'w') as f:
            _json_dump(artifacts['classifier_features'], f, indent=2)
        saved.append(str(path))

    if 'regressor_features' in artifacts:
        path = model_dir / "regressor_features.json"
        with open(path, 'w') as f:
            _json_dump(artifacts['regressor_features'], f, indent=2)
        saved.append(str(path))

    if 'encoders' in artifacts:
        path = model_dir / "label_encoders.json"
        with open(path, 'w') as f:
            _json_dump(artifacts['encoders'], f, indent=2)
        saved.append(str(path))

    if 'metadata' in artifacts:
        meta = artifacts['metadata']
        meta['saved_at']      = datetime.now().isoformat()
        meta['model_version'] = f"v1.0_{datetime.now().strftime('%Y%m%d')}"
        path = model_dir / "metadata.json"
        with open(path, 'w') as f:
            _json_dump(meta, f, indent=2)
        saved.append(str(path))

    if 'shap_importance' in artifacts:
        path = model_dir / "shap_importance.json"
        with open(path, 'w') as f:
            _json_dump(artifacts['shap_importance'], f, indent=2)
        saved.append(str(path))

    print(f"\n  Saved {len(saved)} artifacts to {model_dir}:")
    for s in saved:
        print(f"    {Path(s).name}")


def print_section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
