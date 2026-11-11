# Implementation Roadmap

## Dependencies Between Phases

```
Phase 0: Data Collection (Copr side)
    │
    ├──▶ Phase 1: History DB + Report Endpoint (rpmeta side)
    │        │
    │        ├──▶ Phase 1b: CPU Hardware Calibration (rpmeta + Copr, soft dependency)
    │        │        │
    │        │        └──▶ Phase 3 (soft dependency: better duration accuracy)
    │        │
    │        └──▶ Phase 3: Flavor Optimizer + Flavor Catalog (rpmeta side)
    │                 │
    │                 └──▶ Phase 5: Copr Backend Integration (Copr side)
    │
    └──▶ Phase 2: Spec Analysis Heuristic (rpmeta side, soft dependency)
              │
              └──▶ Phase 3 (merge into prediction pipeline)

Phase 4: ML Model Retraining (rpmeta side, needs Phase 0 data)
    │
    └──▶ Phase 3 (merge into prediction pipeline)
```

Phases 0, 1, and 2 can start in parallel. Phase 2's nearest-neighbor lookup
(04-spec-analysis.md) is only as good as Phase 1's accumulated
`build_requires` data, but degrades gracefully to the static language table
when that corpus is empty -- so it's a soft dependency, not a blocker to
starting. Phase 1b is likewise a soft dependency of Phase 3: without it,
`_get_speed_factor()` always returns 1.0 (tier 3 of its own fallback), so
duration estimates and CPU confidence are just less accurate, not broken --
Phase 3 doesn't need to wait for it. Phase 3 integrates everything. Phase 4
requires weeks/months of collected data. Phase 5 is the Copr-side
integration.

---

## Phase 0: Data Collection (Copr Side)

**Goal:** Get every build's resource summary into rpmeta's History DB.

**Status: mostly already done, on the mock side.** Mock's `system_monitor`
plugin (`mock/py/mockbuild/plugins/system_monitor.py`) already implements
cgroup v2 periodic sampling with real per-phase hooks, anon/file memory
breakdown, OOM/thrashing/throttling detection, and kernel-exact peak
tracking -- see 01-data-collection.md for the full field list. **No
`CgroupSampler` needs to be built in `copr-rpmbuild`.** Network monitoring
was deliberately decided against (no network access during `%build`/`%check`).

**What's actually left to implement (Copr backend side):**
1. After each build, read `<results_dir>/system_monitor/overall_stats.json`
   and the per-phase `{phase}_stats.json` files
2. Attach `builder_cores`/`builder_flavor` from Copr's own builder pool config
3. Send the combined payload (including the raw, unclassified `phases` list)
   to rpmeta's `POST /report` (see 02-history-db.md for the exact payload
   shape) -- rpmeta does the bottleneck classification on ingest, Copr
   backend is purely a relay

**Estimated effort:** 1 day. This is a read-and-forward operation, not a new
sampler -- the estimate dropped from the original 3-4 days because Phase 0's
hard part (the sampler itself) is already implemented in mock.

**Testing:** Run a few builds, verify `overall_stats.json` and per-phase
files are produced with reasonable values. Compare `peak_anon_gb` against
`free -m` observations. Verify `oom_kill_count`/`workingset_refault_*` are
zero on a normal build and non-zero on a deliberately memory-starved one.

**Risks:**
- Some metrics may not be available on older kernels (`memory.peak` requires
  kernel >= 5.19, per-phase peak reset requires >= 6.12; cgroups v1 has a
  different file layout entirely). Copr builders should be on recent
  Fedora/RHEL with cgroups v2.

---

## Phase 1: History DB + Report Endpoint (rpmeta Side)

**Goal:** rpmeta stores resource profiles per (package, chroot) and serves
them in predictions.

**What to implement:**
1. Add PostgreSQL + SQLAlchemy dependencies to `pyproject.toml` (`server` extra)
2. Add `[receive_data]` config section (reuse from `receive-data` branch):
   `enabled`, `allowed_sources`, `max_timestamp_drift`, `database_url`
3. Create `rpmeta/db.py`:
   - SQLAlchemy model `BuildResourceHistory` (see 02-history-db.md for the
     full schema, including quality-signal columns, `cpu_model`,
     `kernel_peak_rss_gb`/`kernel_peak_swap_gb`/`kernel_peak_available`, and
     `phase_breakdown_latest`)
   - `init_db()` using `create_all()`
   - Session factory
4. Create `rpmeta/history.py`:
   - `store_report(session, report_data)` -- insert new record, including
     `_build_phase_profile()`/`_classify_single_phase()`/
     `_classify_phase_quality()` to turn the raw `phases` list from the
     report payload into `phase_breakdown_latest` entries (each carrying
     `duration_pct`, `cpu_saturation`, `cpu_core_seconds`, `io_wait_share`,
     `bottleneck`, `quality_flag`) (see 02-history-db.md)
   - `get_resource_profile(session, package_name, chroot)` -- filters out
     OOM-killed rows, splits `ram_rows`/`cpu_rows` by quality signals,
     computes independent `ram_confidence`/`cpu_confidence`, sets
     `wall_clock_seconds` from the latest row, and picks `bottleneck_hint`
     from the latest row's `phase_breakdown_latest`
     (see `get_resource_profile()` in 02-history-db.md) -- fixes a bug
     where `wall_clock_seconds` was silently dropped from the return value
     and `mean_rss_gb`/`mean_anon_gb` field naming was inconsistent between
     mock's output and this schema
   - `cleanup_old_records(session, keep_n=20)` -- retention
5. Reuse `rpmeta/server/auth.py` from `receive-data` branch (HMAC-SHA256)
6. Add `POST /v1/report` endpoint to `api.py`
7. Modify `POST /v1/predict` to check history DB first
8. Initialize DB in `cli/run.py` `serve` command
9. Unit tests using SQLite in-memory, including cases with OOM-killed rows,
   throttled rows, and thrashing rows to verify the quality filters exclude
   the right subset, plus a case with per-phase `quality_flag="thrashing"`
   on one phase but not another

**Estimated effort:** 5-7 days (up slightly from 4-6, to account for the
extra columns and per-phase quality-flag classification on top of the base
auth/CRUD/filtering work). Most of the auth code exists on `receive-data`.

**Testing:** Unit tests for DB operations (including quality-filter edge
cases), integration test with real PostgreSQL (or SQLite for CI), e2e test
of report → predict flow.

---

## Phase 1b: CPU Hardware Calibration (rpmeta + Copr Side)

**Goal:** Correct `cpu_user_seconds`/`cpu_system_seconds` for the fact that
different CPU models do the same work in different wall-clock core-seconds
(clock speed, IPC, turbo, throttling) -- see "CPU Hardware Normalization" in
02-history-db.md. Without this, mixing readings from a heterogeneous
builder fleet biases the History DB percentile the same way mixing core
counts would for RAM.

**What to implement (Copr side):**
1. Run `stress-ng --cpu 1 --cpu-method matrixprod` and `stress-ng --stream 1`
   once per distinct CPU model during builder image provisioning (not on
   the build's own critical path)
2. Report `cpu_model`, `coremark_score` (stress-ng `--cpu` bogo-ops/sec),
   and `stream_bandwidth_mbps` to rpmeta -- either a small dedicated
   endpoint or piggybacked on the first `/report` call seen for that model
3. Include `cpu_model` on every subsequent `/report` payload (already
   covered by Phase 0's existing `overall_stats.json` relay, since mock's
   `hw_info` already has this)

**What to implement (rpmeta side):**
1. `hardware_calibration` table (see 02-history-db.md schema)
2. `_get_speed_factor(session, cpu_model)`, `_normalized_cpu_seconds()`,
   `_cpu_confidence()` (with the uncalibrated-fraction discount) in
   `rpmeta/history.py`, wired into `get_resource_profile()`
3. Nightly batch job (`rpmeta/calibration.py`) fitting the two-way
   fixed-effects regression (`package_effect` + `hardware_effect`) over
   `build_resource_history`, upserting `hardware_calibration` rows with
   `calibration_source='regression'` once `overlap_sample_count >= 5`
4. Benchmark-prior fallback: linear fit of `log(coremark_score)` against
   already-calibrated models' factors, for models with a benchmark but not
   yet enough regression overlap (`calibration_source='benchmark_prior'`)
5. Unit tests for the regression math (synthetic data with a known planted
   `hardware_effect`, verify recovery), the three-tier fallback selection,
   and the confidence discount

**Estimated effort:** 4-5 days rpmeta side (regression job + storage +
fallback logic), 1 day Copr side (benchmark step in provisioning + one
extra field on the report payload).

**Depends on:** Phase 1 (needs the `cpu_model` column and enough
`build_resource_history` rows for the regression to have overlap to work
with) -- but degrades to `relative_speed_factor=1.0` everywhere
(tier 3 of its own fallback) when absent, so **Phase 3 doesn't have to wait
for this phase to be useful**, only to be *maximally accurate*.

---

## Phase 2: Spec Analysis Heuristic (rpmeta Side)

**Goal:** Provide rough resource estimates for completely unknown packages,
preferring a nearest-neighbor match against real historical data over a
static per-language guess whenever one exists (see "Two Strategies" in
04-spec-analysis.md).

**What to implement:**
1. Create `rpmeta/spec_analyzer.py`:
   - `_jaccard_similarity(a: set[str], b: set[str]) -> float`
   - `find_similar_packages(session, build_requires, chroot, k=5)` --
     queries `build_resource_history` (requires the `build_requires` column
     from Phase 1, see 02-history-db.md) for similar, non-OOM-killed rows
   - `estimate_from_similar_packages(neighbors) -> Optional[ResourceProfile]`
   - `detect_language(build_requires: list[str]) -> str`
   - `detect_build_system(build_requires: list[str]) -> str`
   - `estimate_from_spec(session, chroot, build_requires, srpm_size_mb, has_check) -> ResourceProfile`
     -- tries nearest-neighbor first, falls back to the static language
     table only when no similar package is found
2. Language profiles as the fallback table (see 04-spec-analysis.md)
3. Add optional fields to `InputRecord` (backward-compatible):
   - `build_requires: Optional[list[str]]`
   - `srpm_size_mb: Optional[float]`
   - `has_check: Optional[bool]`
4. Integrate into `/predict` as Layer 3 fallback
5. Unit tests with sample spec files, including cases where a
   nearest-neighbor match exists vs. doesn't (falls through to the table)

**Estimated effort:** 3-4 days (up from 2-3 to account for the
nearest-neighbor query and similarity scoring). Pure Python, no ML
dependencies.

**Depends on:** the `build_requires` column existing on
`build_resource_history` (Phase 1) for the nearest-neighbor corpus to have
anything to search -- but degrades gracefully to the static table when that
corpus is empty, so **Phase 2 can still start immediately** without waiting
for Phase 1 to have accumulated data.

---

## Phase 3: Flavor Optimizer + Flavor Catalog (rpmeta Side)

**Goal:** Replace binary "powerful yes/no" with a ranked multi-flavor
recommendation. No cost/pricing involved -- ranking is by resource fit only.

**What to implement:**
1. Create `rpmeta/optimizer.py`:
   - `Flavor` data model (name, cores, ram_gb, storage_tier, cpu_model,
     description) -- `cpu_model` is optional, used only to denormalize
     duration estimates via Phase 1b's `hardware_calibration` table
   - `resolve_catalog(input_data, arch)` -- prefer `available_flavors` from
     the request, fall back to a static default config only if absent
   - `rank_flavors(profile, catalog, session, max_build_hours, ram_weight,
     cpu_weight)` constraint solver + efficiency ranking, using
     `ram_confidence`/`memory_tightness_flag` for the safety margin and
     `bottleneck_hint`/`storage_tier` for the I/O tie-break boost (see
     `_IO_BOTTLENECK_BOOST` in 05-flavor-optimizer.md); `session` is used
     only to resolve `relative_speed_factor` per candidate flavor
   - `_avg_concurrent_cores(profile)`, `_estimate_duration_hours(profile,
     flavor, session)` -- phase-aware when `profile.phase_breakdown_latest`
     is present (uses each phase's own observed parallelism ceiling instead
     of one whole-build average, fixing an Amdahl's Law blind spot for
     mixed serial/parallel workloads), denormalizing `cpu_core_seconds` by
     the candidate flavor's `relative_speed_factor` before projecting
   - `_compute_efficiency_index(profile, flavor, safety, ram_weight,
     cpu_weight)` -- weights configurable instead of a fixed 50/50 split
   - `explain_ranking(profile, flavor, safety)` human-readable reasoning
2. Add `available_flavors: Optional[list[Flavor]]`, `max_build_hours:
   Optional[float]`, `ram_weight: Optional[float]`, `cpu_weight:
   Optional[float]` to `InputRecord` -- `available_flavors` is the primary,
   caller-supplied catalog source (no config to sync); the other three are
   per-request overrides of otherwise-config-driven policy (see
   "Configurable Duration Ceiling"/"Configurable Efficiency Weights" in
   05-flavor-optimizer.md)
3. Add `default_max_build_hours`, `default_ram_weight`, `default_cpu_weight`
   to rpmeta's config (replacing the previously hardcoded `8.0`/`0.5`/`0.5`
   defaults baked into function signatures)
4. Minimal fallback catalog config (single default entry) for callers that
   haven't been updated to send `available_flavors` yet (legacy/testing only)
5. Add `ResourceProfile`, `flavor_ranking`, `recommended_flavor`, and
   `catalog_source` to API response
6. Update `/predict` endpoint to include the full flavor ranking
7. Backward-compatible: new request/response fields are all optional
8. Unit tests including a mixed-phase profile (serial `%prep` + parallel
   `%build`) to verify `_estimate_duration_hours()` gives different
   effective core counts per phase, and a case with an uncalibrated
   `cpu_model` to verify it falls back to `relative_speed_factor=1.0`
   instead of erroring

**Estimated effort:** 3-4 days (up from 2-3 to account for phase-aware
duration estimation and the configurable weights/ceiling). No ML, pure
logic.

**Depends on:** Phase 1 (history DB) or Phase 2 (spec analysis) to provide
resource profiles. Phase 1b (soft dependency) improves duration accuracy
but isn't required -- uncalibrated CPU models just get
`relative_speed_factor=1.0`. Can develop against mock data.

---

## Phase 4: ML Model Retraining (rpmeta Side)

**Goal:** Train resource prediction models from collected data.

**Prerequisites:** Weeks/months of Phase 0 data collection. Need sufficient
builds with `overall_stats.json` (see 01-data-collection.md) for meaningful
training.

**What to implement:**
1. Extend `Record` in `dataset.py` with resource fields
2. Extend `CoprFetcher` to download `overall_stats.json`
3. Add resource targets to `constants.py` (`RESOURCE_TARGETS`), each with a
   `quantiles` list -- `[0.5, 0.95]` for `peak_anon_gb`/`cpu_core_seconds`,
   `[0.5]` for `io_total_gb` (see "Quantile Prediction Instead of a Flat
   Confidence Constant" in 03-ml-models.md)
4. Add `quantile_alpha` support to `XGBoostModelTrainer`/`LightGBMModelTrainer`
   (`trainer/models.py`) and thread it through `run_all_targets()`
   (`trainer/trainer.py`) -- one model trained per (target, quantile) pair
5. Create `MultiTargetPredictor` in `predictor.py`, keyed by `(target,
   quantile)`; `predict_resource_profile()` uses the `p95` prediction as the
   `ResourceProfile` value and derives `ram_confidence`/`cpu_confidence`
   from the `p50`/`p95` spread (`_confidence_from_quantile_spread()`) --
   replaces the old flat `ml_confidence` config constant entirely
6. Select appropriate transforms per target (log1p, sqrt)
7. Optuna studies for each (target, quantile) combination
8. Integrate into prediction pipeline (Layer 2)

**Estimated effort:** 6-8 days for implementation (up from 5-7 to account
for the quantile training/serving path), but requires data
collection period first (1-3 months of builds).

**This phase is optional for initial deployment.** The system works with
just history DB + spec heuristics. ML improves predictions for known-but-
not-yet-built-here packages.

---

## Phase 5: Copr Backend Integration (Copr Side)

**Goal:** Copr backend uses rpmeta's flavor recommendation for builder
assignment and reports resource stats after builds.

**What to implement:**
1. Modify `copr_backend/rpmeta.py`:
   - Build the `available_flavors` list from Copr's own builder pool config
     and include it in every `/predict` request (see 05-flavor-optimizer.md)
   - Parse `recommended_flavor` (and `flavor_ranking`, `catalog_source` for
     monitoring) from the `/predict` response
   - Add `rpmeta_report_build()` function calling `POST /report`
2. Modify `background_worker_build.py`:
   - Use recommended flavor for VM/container assignment
   - Call `rpmeta_report_build()` after `_mark_finished()` in `finally` block
   - Send the data from mock's `system_monitor/overall_stats.json` and
     per-phase `{phase}_stats.json` files (see 01-data-collection.md and
     the `/report` payload shape in 02-history-db.md)
   - Attach `cpu_model` (from the builder's `hw_info.log`/`lscpu`, same
     source Copr already reads for static builder specs -- see "Current
     State" in 01-data-collection.md) alongside `builder_cores`/
     `builder_flavor`, needed by Phase 1b's CPU normalization
   - Include `build_requires` in both `/predict` (so Layer 3's
     nearest-neighbor lookup has something to match against for new
     packages) and `/report` (so this build grows the corpus for future
     lookups) -- see 04-spec-analysis.md
3. Add rpmeta HMAC secret to backend config (`copr-be.conf`)
4. Graceful degradation: if rpmeta is unavailable or returns no recommendation,
   fall back to current logic (threshold-based powerful assignment)

**Estimated effort:** 3-4 days.

**Depends on:** Phase 0 (data collection), Phase 1 (report endpoint),
Phase 3 (flavor recommendation in predict response).

---

## Summary Timeline

| Phase | What                    | Effort   | Depends On | Can Start    |
|-------|-------------------------|----------|------------|--------------|
| 0     | Data collection (Copr)  | 1 day    | Nothing    | Immediately  |
| 1     | History DB (rpmeta)     | 5-7 days | Nothing    | Immediately  |
| 1b    | CPU hardware calibration (rpmeta+Copr) | 5-6 days | Phase 1 (soft) | After Phase 1 starts |
| 2     | Spec analysis (rpmeta)  | 3-4 days | Nothing (soft: Phase 1) | Immediately  |
| 3     | Flavor optimizer (rpmeta) | 3-4 days | Phase 1+2  | After 1 or 2 |
| 4     | ML retraining (rpmeta)  | 6-8 days | Phase 0    | After months of data |
| 5     | Backend integration     | 3-4 days | Phase 0+1+3| After 0,1,3  |

**Minimum viable system:** Phases 0 + 1 + 3 + 5 = ~12-16 days of work (down
from the original estimate now that Phase 0 is mostly a read-and-forward
operation, since mock already implements the sampler). This gives you:
mock's existing cgroup sampling → history-based predictions → multi-flavor
selection → backend integration. Phase 1b is deliberately excluded from
the minimum viable set -- CPU duration estimates are just less accurate
(flat `relative_speed_factor=1.0`) without it, nothing is broken.

**Full system (with ML + spec analysis + CPU calibration):** All phases =
~26-34 days of work, spread over months (waiting for data collection).

## Considered and Rejected Approaches

### EMA (Exponential Moving Average) of build times
Rejected due to feedback loop: powerful builder → faster build → lower EMA →
lose powerful builder → slow build → higher EMA → cycle repeats.
See 02-history-db.md for detailed analysis.

### Bandit / Reinforcement Learning for flavor selection
Rejected because exploration (trying suboptimal flavors) makes the build
system unpredictable for users. Build systems must be deterministic.

### Canary builds on different hardware
Rejected because users expect consistent build performance. Deliberately
running a build on slower hardware "to collect data" is unacceptable UX.

### Single model predicting flavor directly
Rejected because it's a black box. Can't explain why a flavor was chosen,
can't adjust without retraining. Separating prediction (ML) from selection
(constraint solver) gives transparency and configurability.

### ML classifier for `bottleneck_hint` (predicting cpu/io/memory/none)
Considered as a way to give Layer 2/3 profiles a real bottleneck signal
instead of always `"none"` (see 03-ml-models.md). Rejected for now: the
training labels would come from *other* packages' `_classify_bottleneck()`
output (02-history-db.md), and whether "package X's dominant phase was
I/O-bound" generalizes from static features to a completely different,
never-built package is unproven. Revisit once there's enough
`phase_breakdown_latest` history to evaluate it empirically instead of
speculatively.

### `mean_anon_gb` (or similar spike-vs-sustained target) as an ML target
Already flagged as "collected, not yet a training target" in
03-ml-models.md's feature table. Not pursued alongside the quantile-based
confidence work in this round -- the quantile spread already gives a
principled per-prediction confidence signal without needing a fourth
target model; revisit only if quantile confidence proves insufficient in
practice.

### Broader AI/ML uses of the same collected data (anomaly detection,
### pre-build OOM-risk classifier, cross-chroot transfer learning,
### time-series shape analysis, offline package clustering for catalog design)
All discussed and explicitly not pursued as part of this pipeline. They
answer different questions than "which flavor should this build use"
(build health monitoring, packaging QA, catalog design feedback) and would
each need their own scoping, training data, and evaluation criteria. Worth
revisiting as separate initiatives once the core resource-profiling +
flavor-ranking pipeline (Phases 0-5) is in production and the underlying
data (`build_resource_history`, `samples.jsonl`) has accumulated enough
history to make them worthwhile.
