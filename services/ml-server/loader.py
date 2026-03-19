"""
ARIA — ML Server: Model Loader
================================
Loads all 4 model artifacts at startup into a shared registry.
Called once during FastAPI lifespan — never at request time.

Design:
  - Single ModelRegistry instance held in app.state
  - Each model entry: {model, features, metadata, shap_importance}
  - Models 3 and 4 have classifier + regressor pairs
  - Graceful degradation: if one model fails to load, the others
    still serve. Health endpoint reports which are degraded.
"""

import json
import joblib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)

MODELS_DIR = Path(__file__).parent / "models"


# ── Per-model artifact container ─────────────────────────────

@dataclass
class SingleModelArtifacts:
    """One model file + its feature list + metadata."""
    model:            Any
    features:         list[str]
    metadata:         dict
    shap_importance:  dict
    artifact_dir:     str


@dataclass
class DualModelArtifacts:
    """Classifier + Regressor pair (Models 3 and 4)."""
    classifier:           Any
    classifier_features:  list[str]
    regressor:            Any
    regressor_features:   list[str]
    metadata:             dict
    shap_importance:      dict       # keyed by 'classifier' and 'regressor'
    artifact_dir:         str


@dataclass
class ModelRegistry:
    """
    Holds all loaded artifacts.
    Access via app.state.registry throughout the FastAPI app.
    """
    model1_persona:   Optional[SingleModelArtifacts] = None
    model2_duration:  Optional[SingleModelArtifacts] = None
    model3_deadzone:  Optional[DualModelArtifacts]   = None
    model4_earnings:  Optional[DualModelArtifacts]   = None
    load_errors:      dict = field(default_factory=dict)

    def is_healthy(self) -> bool:
        return all([
            self.model1_persona  is not None,
            self.model2_duration is not None,
            self.model3_deadzone is not None,
            self.model4_earnings is not None,
        ])

    def model_status(self) -> dict:
        statuses = {}
        for name, attr in [
            ("model1_persona",  self.model1_persona),
            ("model2_duration", self.model2_duration),
            ("model3_deadzone", self.model3_deadzone),
            ("model4_earnings", self.model4_earnings),
        ]:
            if attr is None:
                statuses[name] = {
                    "loaded": False,
                    "version": None,
                    "artifact_dir": None,
                    "error": self.load_errors.get(name),
                }
            else:
                statuses[name] = {
                    "loaded": True,
                    "version": attr.metadata.get("model_version"),
                    "artifact_dir": attr.artifact_dir,
                }
        return statuses


# ── Helpers ───────────────────────────────────────────────────

def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_joblib(path: Path) -> Any:
    return joblib.load(path)


# ── Loaders ───────────────────────────────────────────────────

def _load_model1(models_dir: Path) -> SingleModelArtifacts:
    d = models_dir / "model1_persona"
    return SingleModelArtifacts(
        model           = _load_joblib(d / "model.joblib"),
        features        = _load_json(d / "features.json"),
        metadata        = _load_json(d / "metadata.json"),
        shap_importance = _load_json(d / "shap_importance.json"),
        artifact_dir    = str(d),
    )


def _load_model2(models_dir: Path) -> SingleModelArtifacts:
    d = models_dir / "model2_duration"
    return SingleModelArtifacts(
        model           = _load_joblib(d / "model.joblib"),
        features        = _load_json(d / "features.json"),
        metadata        = _load_json(d / "metadata.json"),
        shap_importance = _load_json(d / "shap_importance.json"),
        artifact_dir    = str(d),
    )


def _load_model3(models_dir: Path) -> DualModelArtifacts:
    d = models_dir / "model3_deadzone"

    # shap_importance.json may be a flat dict or a nested dict with
    # 'classifier' and 'regressor' keys depending on training script version
    shap_raw = _load_json(d / "shap_importance.json")
    if "classifier" in shap_raw and "regressor" in shap_raw:
        shap = shap_raw
    else:
        # flat dict — treat as classifier importance
        shap = {"classifier": shap_raw, "regressor": {}}

    return DualModelArtifacts(
        classifier          = _load_joblib(d / "classifier.joblib"),
        classifier_features = _load_json(d / "classifier_features.json"),
        regressor           = _load_joblib(d / "regressor.joblib"),
        regressor_features  = _load_json(d / "regressor_features.json"),
        metadata            = _load_json(d / "metadata.json"),
        shap_importance     = shap,
        artifact_dir        = str(d),
    )


def _load_model4(models_dir: Path) -> DualModelArtifacts:
    d = models_dir / "model4_earnings"

    shap_raw = _load_json(d / "shap_importance.json")
    if "regressor" in shap_raw and "classifier" in shap_raw:
        shap = shap_raw
    else:
        shap = {"regressor": shap_raw, "classifier": {}}

    return DualModelArtifacts(
        classifier          = _load_joblib(d / "classifier.joblib"),
        classifier_features = _load_json(d / "classifier_features.json"),
        regressor           = _load_joblib(d / "regressor.joblib"),
        regressor_features  = _load_json(d / "regressor_features.json"),
        metadata            = _load_json(d / "metadata.json"),
        shap_importance     = shap,
        artifact_dir        = str(d),
    )


# ── Public entry point ────────────────────────────────────────

def load_all_models(models_dir: Path = MODELS_DIR) -> ModelRegistry:
    """
    Load all 4 models into a registry.
    Each model is loaded independently — failure in one does not
    block the others. load_errors records any failures.
    """
    registry = ModelRegistry()

    loaders = [
        ("model1_persona",  _load_model1),
        ("model2_duration", _load_model2),
        ("model3_deadzone", _load_model3),
        ("model4_earnings", _load_model4),
    ]

    for name, loader_fn in loaders:
        try:
            artifact = loader_fn(models_dir)
            setattr(registry, name, artifact)
            version = artifact.metadata.get("model_version", "unknown")
            logger.info(f"  ✅ {name} loaded — version={version}  dir={artifact.artifact_dir}")
        except Exception as e:
            registry.load_errors[name] = str(e)
            logger.error(f"  ❌ {name} failed to load: {e}")

    loaded   = sum(1 for n, _ in loaders if getattr(registry, n) is not None)
    failed   = len(loaders) - loaded
    logger.info(f"Model registry: {loaded}/4 loaded, {failed} failed")

    return registry
