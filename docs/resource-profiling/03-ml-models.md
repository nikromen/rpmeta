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

Instead of one target, predict multiple hardware-independent resource metrics.
Thanks to periodic sampling during builds (see 01-data-collection.md), we have
rich time-series derived features as targets:

```
Same 13 features → [Model 1] → peak_anon_gb
Same 13 features → [Model 2] → cpu_core_seconds
Same 13 features → [Model 3] → io_total_gb
(optionally)     → [Model 4] → build_duration (keep existing)
```

Two targets from the original proposal were dropped after reviewing what's
actually collectible and useful (see "Hardware Dependency" below and
02-history-db.md for the full reasoning):

- `sustained_high_rss_pct` -- removed. It baked in an arbitrary, self-
  referential threshold (samples above 80% of *that build's own* peak), which
  doesn't compare meaningfully across builds and isn't produced by mock's
  `system_monitor` plugin anymore (see 01-data-collection.md).
- `net_download_gb` -- removed. Copr builders don't have network access
  during `%build`/`%check`; the only network activity is dependency
  resolution, and cgroup-level accounting for it isn't worth the complexity.
  Not collected by mock.
- `cpu_utilization_ratio` was replaced by `cpu_core_seconds` (see below) --
  the ratio bakes in a hardware confound (its denominator is whatever core
  count the training build happened to run on) that the absolute value
  doesn't have.

### The `ResourceProfile` Schema

This is the object all three layers (History DB, ML, Spec Analysis) produce,
and the flavor optimizer (05-flavor-optimizer.md) consumes:

```python
class ResourceProfile(BaseModel):
    peak_anon_gb: float                     # memory actually needed (not reclaimable cache)
    cpu_core_seconds: Optional[float]        # total CPU work, hardware-independent
    wall_clock_seconds: Optional[float]      # duration of the measurement this came from
    io_total_gb: float

    ram_confidence: float                    # 0.0-1.0, see 02-history-db.md
    cpu_confidence: float                    # 0.0-1.0, independently discounted
    memory_tightness_flag: bool = False      # swap + memory.high/max breaches in history

    bottleneck_hint: str = "none"            # "cpu" | "io" | "memory" | "none"

    build_count: int = 0
    source: str                              # "history" | "model" | "spec_heuristic"
```

`wall_clock_seconds` is carried alongside `cpu_core_seconds` specifically so
`_avg_concurrent_cores()` in 05-flavor-optimizer.md can compute the
workload's actually-observed parallelism (`cpu_core_seconds /
wall_clock_seconds`) instead of assuming a fixed baseline core count.
`bottleneck_hint` is a diagnostic signal only (see 02-history-db.md's
`_classify_bottleneck()`) -- it's read from the single most recent build's
phase breakdown when the source is History DB, and is `"none"` when the
source is Layer 2/3, since neither ML predictions nor spec heuristics have
a real build's phase timeline to look at.

### Why These Targets Are Hardware-Independent (Mostly)

- `peak_anon_gb`: Memory allocation is close to a property of the code, not
  the machine -- with the caveat that it still scales somewhat with build
  parallelism (more cores → more concurrent compiler/linker processes).
  Using `anon` (not raw RSS) already removes the *other* HW confound, where
  `memory.current` inflates with how much RAM happens to be free for
  opportunistic page caching.
- `cpu_core_seconds`: Total CPU work done is roughly constant for the same
  source code regardless of core count -- this is the metric that doesn't
  need a hardware confound correction, unlike a precomputed utilization
  ratio would.
- `io_total_gb`: Data volume read/written doesn't depend on disk speed.

None of this is claimed to be perfectly hardware-independent -- see
"Hardware Dependency of Resource Metrics" in 02-history-db.md for the exact
confounds and how History DB's lookup logic corrects for them before they
reach a `ResourceProfile`. The ML models here inherit the same training
data, so the same caveats (mainly: `peak_anon_gb` still correlates with
`builder_cores`) apply to what they predict -- which is exactly why
`cpu_cores`/`ram` are already in the model's numerical feature set below:
letting the model condition on hardware explicitly is a better fix for that
correlation than pretending the target has none.

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

4. **RAM overhead:** ~300 MB per model. With quantiles, the model count is
   `build_duration` (1) + `peak_anon_gb` (2: p50/p95) + `cpu_core_seconds`
   (2: p50/p95) + `io_total_gb` (1) = 6 models, ~1.8 GB total. Acceptable
   for an internal prediction service handling a few requests per minute.

### Multi-Output Alternative

If RAM becomes a concern, a single multi-output model shares tree structures
and would use ~400-500 MB total. Trade-offs:

- Shared hyperparameters (not optimal per-target)
- Shared loss function (how to weight errors across different units?)
- More changes to `model.py` and `trainer/base.py`
- Better cross-target consistency (predictions are correlated)

Recommendation: start with separate models for simplicity.

## Quantile Prediction Instead of a Flat Confidence Constant

An earlier version of this design predicted a single point estimate per
target and paired it with a **fixed, config-wide `ml_confidence` constant**
applied identically to every ML-sourced `ResourceProfile`, regardless of
whether the specific package resembles the training data closely or is a
wild extrapolation. That's inconsistent with History DB's philosophy
(02-history-db.md), where confidence is *earned* per-lookup from how much
real evidence backs it up (`build_count`) -- ML should do the analogous
thing per-prediction, not use one number for every package it's ever asked
about.

**Fix: train quantile regressors, not point estimates, for `peak_anon_gb`
and `cpu_core_seconds`.** Both XGBoost (`objective="reg:quantileerror"`,
`quantile_alpha`) and LightGBM (`objective="quantile"`, `alpha`) support
this natively -- it's a training-time objective change, not a different
model architecture. Predict both `p50` and `p95` for each target:

- `p95` becomes the value stored in `ResourceProfile` (consistent with how
  History DB uses p95 of real measurements, not a mean) -- this is what
  actually feeds the RAM/CPU safety constraint in 05-flavor-optimizer.md.
- The **gap between `p50` and `p95`, relative to `p50`**, becomes the
  confidence signal: a narrow gap means the model has seen enough similar
  packages that it's confident the outcome is close to the median; a wide
  gap means it's extrapolating and the p95 could be far off.

```python
def _confidence_from_quantile_spread(p50: float, p95: float) -> float:
    """
    Narrow gap between p50 and p95 (relative to p50) -> the model has
    plenty of similar training examples and is confident. Wide gap -> the
    model is extrapolating past what it's actually seen. Mirrors History
    DB's build-count-based confidence, but derived from prediction
    uncertainty instead of sample count -- replaces the old flat
    `ml_confidence` config constant entirely.
    """
    if p50 <= 0:
        return 0.2  # degenerate case, e.g. a near-zero prediction
    relative_spread = (p95 - p50) / p50
    return max(0.1, min(1.0, 1.0 - relative_spread))
```

`io_total_gb` keeps a single point estimate (`p50` only) -- it isn't used
as a hard constraint anywhere in `rank_flavors()` (05-flavor-optimizer.md),
just carried for context, so the extra model/training cost of a quantile
pair isn't justified for it.

`self.config.model.ml_confidence` (the old flat constant) is removed
entirely from the config -- there's no longer a single number to configure.

## Code Changes Required

### `constants.py` -- Add new targets

```python
# Existing
TARGET = "build_duration"

# New -- targets derived from periodic cgroup sampling (see 01-data-collection.md)
# quantiles=[0.5, 0.95] means two models get trained per target (see
# "Quantile Prediction Instead of a Flat Confidence Constant" above);
# quantiles=[0.5] means just one, a plain point estimate.
RESOURCE_TARGETS = {
    "peak_anon_gb": {"transform": "log1p", "unit": "GB", "quantiles": [0.5, 0.95]},
    "cpu_core_seconds": {"transform": "log1p", "unit": "core-seconds", "quantiles": [0.5, 0.95]},
    "io_total_gb": {"transform": "sqrt", "unit": "GB", "quantiles": [0.5]},
}

ALL_TARGETS = [TARGET] + list(RESOURCE_TARGETS.keys())
```

### `dataset.py` -- Extend Record

```python
class Record(InputRecord):
    build_duration: int = Field(gt=0)  # existing

    # New resource fields (from overall_stats.json, see 01-data-collection.md)
    peak_anon_gb: float = Field(ge=0)
    cpu_core_seconds: float = Field(ge=0)
    io_total_gb: float = Field(ge=0)

    # Quality signals -- rows where these indicate a distorted measurement
    # (OOM kill, heavy throttling, thrashing) are excluded before training,
    # the same way 02-history-db.md excludes them from percentile lookups.
    oom_kill_count: int = Field(ge=0, default=0)
    workingset_refault_anon_total: int = Field(ge=0, default=0)
    workingset_refault_file_total: int = Field(ge=0, default=0)
    cpu_throttled_seconds: float = Field(ge=0, default=0)
```

`InputRecord` is unchanged -- it's what the API receives for prediction.

### `model.py` -- No changes needed

`Model.TARGET_FUNC` and `INVERSE_FUNC` could be overridden per-target in
subclasses, or the transform can be applied in the training manager.
The core `predict()` method works as-is.

### `trainer/trainer.py` -- Iterate over targets AND quantiles

```python
class ModelTrainingManager:
    def run_all_targets(self, targets: list[str] = None):
        if targets is None:
            targets = ALL_TARGETS

        models = []
        for target in targets:
            self.y = self.df[target]
            quantiles = RESOURCE_TARGETS.get(target, {}).get("quantiles", [])
            if len(quantiles) < 2:
                # build_duration (not in RESOURCE_TARGETS), and any target
                # with a single quantile (e.g. io_total_gb) -- plain point
                # estimate, quantile_alpha=None, unchanged from today.
                models.append(self.run(quantile_alpha=None))
                continue

            # 2+ quantiles (peak_anon_gb, cpu_core_seconds): one model per
            # quantile, so downstream code can derive confidence from the
            # p50/p95 spread (see "Quantile Prediction..." above).
            for q in quantiles:
                models.append(self.run(quantile_alpha=q))

        return models
```

The `_preprocess_dataset()`, `_categorize_get_categories_mapping()`, and
`run_study()` methods work unchanged for each target/quantile combination.
`run()` gains a `quantile_alpha: Optional[float]` parameter, threaded down
to the underlying trainer.

### `trainer/models.py` -- Quantile objective

`XGBoostModelTrainer`/`LightGBMModelTrainer` were previously target-agnostic
(trained on whatever `y` is provided, with a fixed regression objective).
They now also accept `quantile_alpha`, switching the training objective
when it's set:

```python
class XGBoostModelTrainer(ModelTrainer):
    def _build_model(self, quantile_alpha: Optional[float] = None, **params):
        if quantile_alpha is not None:
            params["objective"] = "reg:quantileerror"
            params["quantile_alpha"] = quantile_alpha
        return xgboost.XGBRegressor(**params)


class LightGBMModelTrainer(ModelTrainer):
    def _build_model(self, quantile_alpha: Optional[float] = None, **params):
        if quantile_alpha is not None:
            params["objective"] = "quantile"
            params["alpha"] = quantile_alpha
        return lightgbm.LGBMRegressor(**params)
```

When `quantile_alpha is None` (the `build_duration` target, and any target
with `quantiles: [0.5]` treated as a plain point estimate rather than an
explicit quantile loss), behavior is unchanged from today.

### `predictor.py` -- New MultiTargetPredictor

```python
class MultiTargetPredictor:
    def __init__(self, predictors: dict[tuple[str, float], Predictor], config: Config):
        self.predictors = predictors  # keyed by (target, quantile)
        self.config = config

    @classmethod
    def load(cls, model_dir: Path, model_name: ModelEnum,
             category_maps_path: Path, config: Config):
        predictors = {}
        for target in ALL_TARGETS:
            quantiles = RESOURCE_TARGETS.get(target, {}).get("quantiles", [0.5])
            for q in quantiles:
                key = (target, q)
                target_model_dir = model_dir / target / f"p{int(q * 100)}"
                if target_model_dir.exists():
                    predictors[key] = Predictor.load(
                        target_model_dir, model_name, category_maps_path, config
                    )
        return cls(predictors, config)

    def predict_resource_profile(
        self, input_data: InputRecord
    ) -> ResourceProfile:
        def _predict(target: str, q: float) -> Optional[float]:
            predictor = self.predictors.get((target, q))
            if predictor is None:
                return None
            return predictor.predict(input_data, self.config.model.behavior)

        peak_anon_p50 = _predict("peak_anon_gb", 0.5)
        peak_anon_p95 = _predict("peak_anon_gb", 0.95)
        cpu_p50 = _predict("cpu_core_seconds", 0.5)
        cpu_p95 = _predict("cpu_core_seconds", 0.95)
        io_total = _predict("io_total_gb", 0.5)

        return ResourceProfile(
            # p95 is the value that actually feeds the RAM/CPU safety
            # constraint in 05-flavor-optimizer.md, same role p95-of-history
            # plays in 02-history-db.md's get_resource_profile().
            peak_anon_gb=peak_anon_p95,
            cpu_core_seconds=cpu_p95,
            io_total_gb=io_total,
            # ML has no per-build wall_clock_seconds to derive observed
            # parallelism from -- 05-flavor-optimizer.md's
            # _avg_concurrent_cores() falls back to a neutral cpu_fit (0.5)
            # when this is None, rather than guessing a baseline core count.
            wall_clock_seconds=None,
            ram_confidence=_confidence_from_quantile_spread(peak_anon_p50, peak_anon_p95),
            cpu_confidence=_confidence_from_quantile_spread(cpu_p50, cpu_p95),
            memory_tightness_flag=False,  # no history to detect this from
            bottleneck_hint="none",  # no real build timeline to classify
            build_count=0,
            source="model",
        )
```

### `server/api.py` -- Updated predict endpoint

The `/predict` endpoint returns an enriched response:

```python
class PredictionResponse(BaseModel):
    prediction: int  # build_duration in minutes (backward compatible)
    resource_profile: Optional[ResourceProfile]  # new
    flavor_ranking: Optional[list[FlavorRanking]]  # from flavor optimizer, see 05-flavor-optimizer.md
    recommended_flavor: Optional[str]  # top of flavor_ranking, for convenience
    catalog_source: Optional[str]  # "request" or "fallback_default", see 05-flavor-optimizer.md
    source: str  # "history", "model", or "spec_heuristic"
    used_configuration: ModelBehavior
```

The request side gains a matching optional field, `available_flavors`, so
the caller can supply its own flavor catalog instead of rpmeta owning a
static one -- see "Flavor Catalog" in 05-flavor-optimizer.md for the full
`Flavor` schema and fallback behavior. There is no cost/pricing concept
anywhere in this pipeline -- ranking is based purely on how efficiently a
flavor's capacity matches the predicted resource profile.

## Feature Engineering Improvements (Future)

When richer data is available, additional features could improve predictions:

### From time-series sampling (post-build, for training targets)

The periodic sampler (mock's `system_monitor` plugin, see 01-data-collection.md)
produces a time-series from which richer summary statistics could be
extracted, beyond the current three training targets:

| Feature          | Derived from                | What it tells the model                  | Status |
|------------------|------------------------------|-------------------------------------------|--------|
| `mean_anon_gb`   | mean of sampled anon memory  | Typical memory usage                      | already collected, not yet a training target |
| `rss_variance`   | variance of sampled RSS      | Stable vs spiky memory pattern            | would need new aggregation in mock |
| `bottleneck_hint`| per-phase CPU/IO/PSI shares   | Which resource actually gates this build  | already collected, used only as a ranking tie-break (05-flavor-optimizer.md), not a training target |

Two rows from the original version of this table were removed outright, not
just renamed:

- `p95_rss_gb`, `sustained_high_rss_pct` -- dropped from mock itself
  (01-data-collection.md) for baking in arbitrary, self-referential
  thresholds. There's nothing left to derive them from.
- `io_rate_mean_mbps`, `io_rate_max_mbps` -- disk throughput is a hardware
  property, not a package property (the same I/O volume looks like a very
  different "rate" on NVMe vs. network storage). mock never produces these;
  `io_total_gb` (volume, not rate) is the metric that's actually
  hardware-independent.

Any of the remaining rows would be used as **training targets** (what the
model predicts) and also stored in the history DB, the same way the three
current targets are.

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
hw_info. They need to be extended to also fetch mock's `overall_stats.json`
(summary statistics, see 01-data-collection.md) from build results once
Copr starts forwarding it. Optionally, the fetcher can also fetch
`samples.jsonl` (raw time-series) for computing additional derived features
during training.

```python
class CoprFetcher(Fetcher):
    def _fetch_resource_stats(self, result_url: str) -> Optional[dict]:
        """Fetch summary stats computed from periodic cgroup sampling."""
        url = f"{result_url}/system_monitor/overall_stats.json"
        resp = requests.get(url)
        if resp.status_code == 200:
            return resp.json()
        return None

    def _fetch_resource_samples(self, result_url: str) -> Optional[list[dict]]:
        """Fetch raw time-series for advanced feature extraction."""
        url = f"{result_url}/system_monitor/samples.jsonl"
        resp = requests.get(url)
        if resp.status_code == 200:
            return [json.loads(line) for line in resp.text.splitlines()]
        return None
```

### Bootstrapping

Initially there will be no resource data. The system gracefully degrades:

1. No `overall_stats.json` → skip resource targets, train only `build_duration`
2. Partial data → train resource models on available subset
3. Full data → train all models

During the bootstrapping period, the system operates in "current mode"
(build_duration prediction only) and collects resource data for future
model training.
