# ML Models -- Layer 2

## When ML Is Used

ML prediction is the fallback when history DB has no data for a
`(package_name, chroot)` pair. This happens when:

- A package is built on a new chroot for the first time
- A new package is added to Copr (if it's in the training data from Koji/Copr)
- After model retraining includes new packages from fetched data

For completely unknown packages (not in category_maps), ML returns -1 and
the system falls through to Layer 3 (spec analysis).

## Current Model Architecture

rpmeta currently has one model predicting one target:

```
13 features → [XGBoost or LightGBM] → build_duration (minutes)
```

Features (from `constants.py`):
- Categorical (9): package_name, version, os, os_family, os_version, os_arch,
  cpu_model_name, cpu_arch, cpu_model
- Numerical (4): epoch, cpu_cores, ram, swap

Target: `build_duration` (minutes, log1p-transformed)

## New: Multiple Resource Targets

Instead of one target, predict 4 hardware-independent resource metrics:

```
Same 13 features → [Model 1] → peak_rss_gb
Same 13 features → [Model 2] → cpu_utilization_ratio
Same 13 features → [Model 3] → io_total_gb
Same 13 features → [Model 4] → net_download_gb
(optionally)     → [Model 5] → build_duration (keep existing)
```

### Why These Targets Are Hardware-Independent

- `peak_rss_gb`: Memory allocation is a property of the code, not the machine.
- `cpu_utilization_ratio`: Fraction of available CPU used. A CPU-bound build
  (ratio ~0.95) is CPU-bound on any hardware.
- `io_total_gb`: Data volume read/written doesn't depend on disk speed.
- `net_download_gb`: Same dependencies downloaded regardless of network speed.

### Why Separate Models (Not Multi-Output)

1. **Reuse existing code:** `Model`, `ModelTrainer`, `Predictor`, Optuna study
   pipeline -- all work unchanged. Just run the pipeline 4 times with different
   targets.

2. **Per-target optimization:** Each target may need different:
   - Transform: `log1p` for minutes, identity for ratio (0-1), `sqrt` for GB
   - Hyperparameters: optimal tree depth for RAM prediction differs from I/O
   - Outlier removal: IQR bounds differ per metric

3. **Independent evaluation:** Can evaluate accuracy of each target separately.
   Model for `peak_rss_gb` might be excellent while `net_download_gb` is poor.

4. **RAM overhead:** ~300 MB per model × 5 = ~1.5 GB total. Acceptable for an
   internal prediction service handling a few requests per minute.

### Multi-Output Alternative

If RAM becomes a concern, a single multi-output model shares tree structures
and would use ~400-500 MB total. Trade-offs:

- Shared hyperparameters (not optimal per-target)
- Shared loss function (how to weight errors across different units?)
- More changes to `model.py` and `trainer/base.py`
- Better cross-target consistency (predictions are correlated)

Recommendation: start with separate models for simplicity.

## Code Changes Required

### `constants.py` -- Add new targets

```python
# Existing
TARGET = "build_duration"

# New
RESOURCE_TARGETS = {
    "peak_rss_gb": {"transform": "log1p", "unit": "GB"},
    "cpu_utilization_ratio": {"transform": "identity", "unit": "ratio"},
    "io_total_gb": {"transform": "sqrt", "unit": "GB"},
    "net_download_gb": {"transform": "sqrt", "unit": "GB"},
}

ALL_TARGETS = [TARGET] + list(RESOURCE_TARGETS.keys())
```

### `dataset.py` -- Extend Record

```python
class Record(InputRecord):
    build_duration: int = Field(gt=0)  # existing

    # New resource fields
    peak_rss_gb: float = Field(ge=0)
    cpu_utilization_ratio: float = Field(ge=0, le=1)
    io_total_gb: float = Field(ge=0)
    net_download_gb: float = Field(ge=0)
```

`InputRecord` is unchanged -- it's what the API receives for prediction.

### `model.py` -- No changes needed

`Model.TARGET_FUNC` and `INVERSE_FUNC` could be overridden per-target in
subclasses, or the transform can be applied in the training manager.
The core `predict()` method works as-is.

### `trainer/trainer.py` -- Iterate over targets

```python
class ModelTrainingManager:
    def run_all_targets(self, targets: list[str] = None):
        if targets is None:
            targets = ALL_TARGETS

        for target in targets:
            self.y = self.df[target]
            # Apply target-specific transform and preprocessing
            # Run existing training pipeline
            models.append(self.run())

        return models
```

The `_preprocess_dataset()`, `_categorize_get_categories_mapping()`,
`run_study()`, and `run()` methods work unchanged for each target.

### `trainer/models.py` -- No changes needed

`XGBoostModelTrainer` and `LightGBMModelTrainer` are target-agnostic.
They train on whatever `y` is provided.

### `predictor.py` -- New MultiTargetPredictor

```python
class MultiTargetPredictor:
    def __init__(self, predictors: dict[str, Predictor], config: Config):
        self.predictors = predictors
        self.config = config

    @classmethod
    def load(cls, model_dir: Path, model_name: ModelEnum,
             category_maps_path: Path, config: Config):
        predictors = {}
        for target in ALL_TARGETS:
            target_model_dir = model_dir / target
            if target_model_dir.exists():
                predictors[target] = Predictor.load(
                    target_model_dir, model_name, category_maps_path, config
                )
        return cls(predictors, config)

    def predict_resource_profile(
        self, input_data: InputRecord
    ) -> ResourceProfile:
        results = {}
        for target, predictor in self.predictors.items():
            results[target] = predictor.predict(input_data, self.config.model.behavior)

        return ResourceProfile(**results)
```

### `server/api.py` -- Updated predict endpoint

The `/predict` endpoint returns an enriched response:

```python
class PredictionResponse(BaseModel):
    prediction: int  # build_duration in minutes (backward compatible)
    resource_profile: Optional[ResourceProfile]  # new
    recommended_flavor: Optional[str]  # from cost optimizer
    source: str  # "history", "model", or "spec_heuristic"
    used_configuration: ModelBehavior
```

## Feature Engineering Improvements (Future)

When richer data is available, additional features could improve predictions:

### From spec file (static, pre-build)
- `primary_language`: C/C++/Rust/Go/Python/Java/Perl/Other
- `srpm_size_mb`: compressed source size
- `num_build_requires`: dependency count
- `has_check_section`: whether tests run
- `build_system_type`: make/cmake/meson/cargo/setuptools/go

### From dependency graph
- `dep_tree_depth`: how deep the dependency chain goes
- `total_dep_install_size_mb`: total size of BuildRequires packages

These features would be added to `ALL_FEATURES` in `constants.py` and
to `InputRecord` in `dataset.py`. They improve predictions especially for
unknown packages where `package_name` isn't in category_maps.

## Training Data Pipeline

### Extending the Fetcher

Current fetchers (`KojiFetcher`, `CoprFetcher`) fetch build duration and
hw_info. They need to be extended to also fetch `resource_stats.json` from
build results (once Copr starts producing it).

```python
class CoprFetcher(Fetcher):
    def _fetch_resource_stats(self, result_url: str) -> Optional[dict]:
        url = f"{result_url}/resource_stats.json"
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json()
        return None
```

### Bootstrapping

Initially there will be no resource data. The system gracefully degrades:

1. No `resource_stats.json` → skip resource targets, train only `build_duration`
2. Partial data → train resource models on available subset
3. Full data → train all models

During the bootstrapping period, the system operates in "current mode"
(build_duration prediction only) and collects resource data for future
model training.
