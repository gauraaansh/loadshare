"""
ARIA — Train All Models
========================
Runs all 4 training scripts in sequence.
Prints a final summary of all metrics.

Usage:
    # Train all models
    python training/train_all.py

    # Train specific model only
    python training/train_all.py --model 2

    # Skip a model
    python training/train_all.py --skip 1

Requirements:
    pip install xgboost scikit-learn shap pandas numpy joblib

Data requirements:
    services/data-pipeline/synthetic/model1_training.csv   (Model 1)
    services/data-pipeline/synthetic/model3_training.csv   (Model 3)
    services/data-pipeline/synthetic/model4_training.csv   (Model 4)
    services/data-pipeline/train_final.csv                 (Model 2)

    If train_final.csv is missing, run first:
        python services/data-pipeline/notebooks/02_preprocess_clean.py

Output:
    services/ml-server/models/model1_persona/
    services/ml-server/models/model2_duration/
    services/ml-server/models/model3_deadzone/
    services/ml-server/models/model4_earnings/
"""

import sys
import json
import time
import argparse
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def run_model(name, train_fn):
    print(f"\n{'#'*60}")
    print(f"  TRAINING: {name}")
    print(f"{'#'*60}")
    start = time.time()
    try:
        train_fn()
        elapsed = time.time() - start
        print(f"\n  ✅ {name} completed in {elapsed:.1f}s")
        return True, elapsed
    except Exception as e:
        elapsed = time.time() - start
        print(f"\n  ❌ {name} FAILED after {elapsed:.1f}s")
        print(f"  Error: {e}")
        traceback.print_exc()
        return False, elapsed


def load_metrics(model_dir):
    """Load saved metrics from metadata.json."""
    meta_file = Path(model_dir) / "metadata.json"
    if not meta_file.exists():
        return {}
    with open(meta_file) as f:
        meta = json.load(f)
    return meta.get('metrics') or meta.get('classifier_metrics') or {}


def main():
    parser = argparse.ArgumentParser(description="Train ARIA ML models")
    parser.add_argument('--model', type=int, choices=[1,2,3,4],
                        help="Train only this model number")
    parser.add_argument('--skip',  type=int, choices=[1,2,3,4],
                        help="Skip this model number")
    args = parser.parse_args()

    models_dir = Path(__file__).parent.parent / "models"

    # Lazy imports to avoid failing if a model's deps aren't installed
    def get_train_fns():
        from training.train_model1_persona  import main as m1
        from training.train_model2_duration import main as m2
        from training.train_model3_deadzone import main as m3
        from training.train_model4_earnings import main as m4
        return {
            1: ("Model 1 — Rider Persona Classifier",     m1, models_dir / "model1_persona"),
            2: ("Model 2 — Delivery Duration Scorer",     m2, models_dir / "model2_duration"),
            3: ("Model 3 — Dead Zone Risk Predictor",     m3, models_dir / "model3_deadzone"),
            4: ("Model 4 — Earnings Trajectory",          m4, models_dir / "model4_earnings"),
        }

    train_fns = get_train_fns()

    # Filter by --model / --skip
    to_run = list(train_fns.keys())
    if args.model:
        to_run = [args.model]
    if args.skip and args.skip in to_run:
        to_run.remove(args.skip)

    print(f"\n{'='*60}")
    print(f"  ARIA — Training Pipeline")
    print(f"  Models to train: {to_run}")
    print(f"{'='*60}")

    results = {}
    total_start = time.time()

    for model_num in to_run:
        name, train_fn, model_dir = train_fns[model_num]
        success, elapsed = run_model(name, train_fn)
        results[model_num] = {
            'name':    name,
            'success': success,
            'elapsed': elapsed,
            'dir':     str(model_dir),
        }

    total_elapsed = time.time() - total_start

    # ── FINAL SUMMARY ─────────────────────────────────────────
    print(f"\n\n{'='*60}")
    print(f"  TRAINING SUMMARY")
    print(f"{'='*60}")

    for model_num, result in results.items():
        status = "✅" if result['success'] else "❌"
        print(f"\n  {status} Model {model_num}: {result['name']}")
        print(f"     Time: {result['elapsed']:.1f}s")

        if result['success']:
            # Load and print key metrics
            _, _, model_dir = train_fns[model_num]
            meta_file = model_dir / "metadata.json"
            if meta_file.exists():
                with open(meta_file) as f:
                    meta = json.load(f)

                if model_num == 1:
                    m = meta.get('metrics', {})
                    print(f"     F1={m.get('f1','?'):.4f}  AUC={m.get('auc','?'):.4f}")
                elif model_num == 2:
                    m = meta.get('metrics', {})
                    print(f"     RMSE={m.get('rmse','?'):.3f}  R²={m.get('r2','?'):.4f}")
                elif model_num == 3:
                    cm = meta.get('classifier_metrics', {})
                    rm = meta.get('regressor_metrics', {})
                    cal = meta.get('calibration_metrics', {})
                    print(f"     Classifier: AUC={cm.get('auc','?'):.4f}  F1={cm.get('f1','?'):.4f}")
                    print(f"     Regressor:  RMSE={rm.get('rmse','?'):.3f}min")
                    print(f"     Brier:      {cal.get('brier_score','?')}")
                elif model_num == 4:
                    rm = meta.get('regressor_metrics', {})
                    cm = meta.get('classifier_metrics', {})
                    print(f"     Regressor:  RMSE={rm.get('rmse','?'):.3f}  R²={rm.get('r2','?'):.4f}")
                    print(f"     Classifier: F1={cm.get('f1','?'):.4f}  AUC={cm.get('auc','?'):.4f}")

                print(f"     Artifacts: {model_dir}")

    succeeded = sum(1 for r in results.values() if r['success'])
    print(f"\n  {succeeded}/{len(results)} models trained successfully")
    print(f"  Total time: {total_elapsed:.1f}s")

    # Metric targets reminder
    print(f"\n  Target metrics:")
    print(f"    Model 1: F1 0.90-0.95")
    print(f"    Model 2: RMSE 5-7min, R² 0.75-0.82")
    print(f"    Model 3: AUC > 0.85, Brier < 0.15")
    print(f"    Model 4: Classifier F1 > 0.80")
    print(f"{'='*60}\n")

    return 0 if succeeded == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
