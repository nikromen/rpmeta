# Implementation Roadmap

## Dependencies Between Phases

```
Phase 0: Data Collection (Copr side)
    │
    ├──▶ Phase 1: History DB + Report Endpoint (rpmeta side)
    │        │
    │        └──▶ Phase 3: Cost Optimizer + Flavor Catalog (rpmeta side)
    │                 │
    │                 └──▶ Phase 5: Copr Backend Integration (Copr side)
    │
    └──▶ Phase 2: Spec Analysis Heuristic (rpmeta side, independent)
              │
              └──▶ Phase 3 (merge into prediction pipeline)

Phase 4: ML Model Retraining (rpmeta side, needs Phase 0 data)
    │
    └──▶ Phase 3 (merge into prediction pipeline)
```

Phases 0, 1, and 2 can start in parallel. Phase 3 integrates everything.
Phase 4 requires weeks/months of collected data. Phase 5 is the Copr-side
integration.

---

## Phase 0: Data Collection (Copr Side)

**Goal:** Start collecting cgroup + network metrics for every build via
periodic sampling. No changes to build behavior -- just recording data.

**Where:** `copr-rpmbuild`, wrapping `MockBuilder.run()`

**What to implement:**
1. `CgroupSampler` class: background daemon thread that reads cgroup metrics
   (`memory.current`, `memory.swap.current`, `cpu.stat`, `io.stat`,
   `*.pressure`) every 10 seconds throughout the build
2. Discover mock process cgroup path from `/proc/<pid>/cgroup`
3. Read `/proc/net/dev` before and after build for network bytes
4. After build: compute summary statistics from time-series (peak, mean,
   p95 RSS, cpu_utilization_ratio, sustained_high_rss_pct, I/O totals)
5. Read `memory.peak` once at end (kernel's high-water mark)
6. Write `resource_stats.json` (summary) to results directory
7. Write `resource_samples.jsonl` (raw time-series) to results directory

**Estimated effort:** 3-4 days. Slightly more than just start/end reads
due to the background thread, but no external dependencies.

**Testing:** Run a few builds, verify both output files are produced with
reasonable values. Compare `peak_rss_gb` against `free -m` observations.
Verify `sustained_high_rss_pct` distinguishes a linking spike from a
sustained-memory build.

**Risks:**
- cgroup path discovery may vary between systemd versions and mock
  isolation modes (nspawn vs simple chroot). Test on actual builders.
- Some metrics may not be available on older kernels (cgroups v1 has
  different file layout). Copr builders should be on recent Fedora (v2).
- Thread safety: the sampler thread only reads kernel files and appends to
  a list, no shared mutable state with the build process.

---

## Phase 1: History DB + Report Endpoint (rpmeta Side)

**Goal:** rpmeta stores resource profiles per (package, chroot) and serves
them in predictions.

**What to implement:**
1. Add PostgreSQL + SQLAlchemy dependencies to `pyproject.toml` (`server` extra)
2. Add `[receive_data]` config section (reuse from `receive-data` branch):
   `enabled`, `allowed_sources`, `max_timestamp_drift`, `database_url`
3. Create `rpmeta/db.py`:
   - SQLAlchemy model `BuildResourceHistory` (see 02-history-db.md for schema)
   - `init_db()` using `create_all()`
   - Session factory
4. Create `rpmeta/history.py`:
   - `store_report(session, report_data)` -- insert new record
   - `get_resource_profile(session, package_name, chroot)` -- p95 lookup
   - `cleanup_old_records(session, keep_n=20)` -- retention
5. Reuse `rpmeta/server/auth.py` from `receive-data` branch (HMAC-SHA256)
6. Add `POST /v1/report` endpoint to `api.py`
7. Modify `POST /v1/predict` to check history DB first
8. Initialize DB in `cli/run.py` `serve` command
9. Unit tests using SQLite in-memory

**Estimated effort:** 3-5 days. Most of the auth code exists on `receive-data`.

**Testing:** Unit tests for DB operations, integration test with real
PostgreSQL (or SQLite for CI), e2e test of report → predict flow.

---

## Phase 2: Spec Analysis Heuristic (rpmeta Side)

**Goal:** Provide rough resource estimates for completely unknown packages.

**What to implement:**
1. Create `rpmeta/spec_analyzer.py`:
   - `detect_language(build_requires: list[str]) -> str`
   - `detect_build_system(build_requires: list[str]) -> str`
   - `estimate_from_spec(build_requires, srpm_size_mb, has_check) -> ResourceProfile`
2. Language profiles (see 04-spec-analysis.md)
3. Add optional fields to `InputRecord` (backward-compatible):
   - `build_requires: Optional[list[str]]`
   - `srpm_size_mb: Optional[float]`
   - `has_check: Optional[bool]`
4. Integrate into `/predict` as Layer 3 fallback
5. Unit tests with sample spec files

**Estimated effort:** 2-3 days. Pure Python, no dependencies, mostly config.

**Can start immediately** -- no dependency on Phase 0 data.

---

## Phase 3: Cost Optimizer + Flavor Catalog (rpmeta Side)

**Goal:** Replace binary "powerful yes/no" with multi-flavor selection.

**What to implement:**
1. Create `rpmeta/optimizer.py`:
   - `Flavor` and `FlavorCatalog` data models
   - `select_flavor(profile, catalog)` constraint solver
   - `estimate_duration_hours(profile, flavor)` duration scaling
   - `explain_selection(profile, flavor)` human-readable reasoning
2. Flavor catalog config format (TOML, per-architecture)
3. Add `ResourceProfile` and `FlavorRecommendation` to API response
4. Update `/predict` endpoint to include flavor recommendation
5. Backward-compatible: new fields are optional in response

**Estimated effort:** 2-3 days. No ML, pure logic.

**Depends on:** Phase 1 (history DB) or Phase 2 (spec analysis) to provide
resource profiles. Can develop against mock data.

---

## Phase 4: ML Model Retraining (rpmeta Side)

**Goal:** Train resource prediction models from collected data.

**Prerequisites:** Weeks/months of Phase 0 data collection. Need sufficient
builds with `resource_stats.json` for meaningful training.

**What to implement:**
1. Extend `Record` in `dataset.py` with resource fields
2. Extend `CoprFetcher` to download `resource_stats.json`
3. Add resource targets to `constants.py` (`RESOURCE_TARGETS`)
4. Create per-target training wrapper in `trainer/trainer.py`
5. Create `MultiTargetPredictor` in `predictor.py`
6. Select appropriate transforms per target (log1p, sqrt, identity)
7. Optuna studies for each target
8. Integrate into prediction pipeline (Layer 2)

**Estimated effort:** 5-7 days for implementation, but requires data
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
   - Parse `flavor_recommendation` from `/predict` response
   - Add `rpmeta_report_build()` function calling `POST /report`
2. Modify `background_worker_build.py`:
   - Use recommended flavor for VM/container assignment
   - Call `rpmeta_report_build()` after `_mark_finished()` in `finally` block
   - Send resource_stats data from `resource_stats.json`
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
| 0     | Data collection (Copr)  | 3-4 days | Nothing    | Immediately  |
| 1     | History DB (rpmeta)     | 3-5 days | Nothing    | Immediately  |
| 2     | Spec analysis (rpmeta)  | 2-3 days | Nothing    | Immediately  |
| 3     | Cost optimizer (rpmeta) | 2-3 days | Phase 1+2  | After 1 or 2 |
| 4     | ML retraining (rpmeta)  | 5-7 days | Phase 0    | After months of data |
| 5     | Backend integration     | 3-4 days | Phase 0+1+3| After 0,1,3  |

**Minimum viable system:** Phases 0 + 1 + 3 + 5 = ~13-16 days of work.
This gives you: periodic cgroup sampling → history-based predictions →
multi-flavor selection → backend integration.

**Full system (with ML + spec analysis):** All phases = ~20-26 days of work,
spread over months (waiting for data collection).

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
