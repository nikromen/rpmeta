# History DB -- Layer 1 (No ML)

## Purpose

For known `(package_name, chroot)` combinations, use **real measured data**
instead of ML predictions. This covers ~80-90% of build requests (most packages
are built repeatedly) and is more accurate than any model.

## Why Not EMA

Exponential Moving Average (EMA) of build TIMES was considered and rejected:

1. **Feedback loop:** Package gets powerful builder → finishes faster → EMA
   drops → loses powerful builder → slow again → oscillation.
2. **Hardware dependency:** Build time depends on hardware. EMA of times on
   mixed hardware is meaningless.
3. **Canary builds:** Periodically building on normal hardware to "refresh" EMA
   makes the build system unpredictable for users.

Resource profiles (peak RAM, total CPU work, I/O volume) do NOT have these
problems because they are properties of the package, not the hardware --
**mostly**. See "Hardware Dependency of Resource Metrics" below: a few of
these metrics are still confounded by the hardware they were measured on
(mainly build parallelism), and the schema and lookup logic here are
designed specifically to detect and correct for that, not to pretend the
confound doesn't exist.

## Schema

One row per build, matching exactly what mock's `system_monitor` plugin
writes to `overall_stats.json` (see 01-data-collection.md) -- no per-phase
row multiplication, no separate tables.

```sql
CREATE TABLE build_resource_history (
    id              SERIAL PRIMARY KEY,
    package_name    TEXT NOT NULL,
    chroot          TEXT NOT NULL,
    build_id        INTEGER,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Resource-need values (what get_resource_profile() aggregates into a
    -- ResourceProfile). peak_anon_gb is preferred over raw peak RSS because
    -- memory.current also counts reclaimable page cache, which inflates with
    -- available RAM even when the build doesn't genuinely need it.
    peak_anon_gb            REAL NOT NULL,
    peak_file_gb            REAL,
    mean_anon_gb            REAL,
    cpu_user_seconds        REAL NOT NULL,
    cpu_system_seconds      REAL NOT NULL,
    io_read_gb              REAL NOT NULL,
    io_write_gb             REAL NOT NULL,

    -- Hardware context (used to stratify/qualify lookups, not fed directly
    -- into the resource profile as a "need" value)
    builder_cores       INTEGER NOT NULL,
    builder_flavor      TEXT,
    wall_clock_seconds  INTEGER NOT NULL,

    -- CPU model identity, e.g. "AMD EPYC 7763" -- already sent today as
    -- InputRecord's cpu_model_name (existing field from the original
    -- build_duration-only system), just wasn't persisted here before. Used
    -- exclusively to look up a `hardware_calibration` row for cross-model
    -- speed normalization -- see "CPU Hardware Normalization" below.
    -- Nullable: older reports that predate this column just get treated as
    -- uncalibrated (speed_factor=1.0, discounted confidence).
    cpu_model           TEXT,

    -- Kernel-exact peak memory/swap for the whole build (`memory.peak`,
    -- `memory.swap.peak`), more precise than the sampled maximum. Stored
    -- for its own sake now (not just used transiently to cross-check
    -- `peak_anon_gb` and discarded) so measurement accuracy can be audited
    -- retroactively -- see 01-data-collection.md.
    kernel_peak_rss_gb      REAL,
    kernel_peak_swap_gb     REAL,
    kernel_peak_available   BOOLEAN NOT NULL DEFAULT FALSE,

    -- Data-quality / validity signals -- decide whether (and how much) to
    -- trust this row before it's aggregated. See "Hardware Dependency" below.
    oom_kill_count              INTEGER NOT NULL DEFAULT 0,
    workingset_refault_anon_total INTEGER NOT NULL DEFAULT 0,
    workingset_refault_file_total INTEGER NOT NULL DEFAULT 0,
    pgmajfault_total            INTEGER NOT NULL DEFAULT 0,
    cpu_nr_throttled            INTEGER NOT NULL DEFAULT 0,
    cpu_throttled_seconds       REAL NOT NULL DEFAULT 0,
    psi_cpu_total_usec          BIGINT NOT NULL DEFAULT 0,
    psi_mem_total_usec          BIGINT NOT NULL DEFAULT 0,
    psi_io_total_usec           BIGINT NOT NULL DEFAULT 0,
    swap_peak_gb                REAL NOT NULL DEFAULT 0,
    mem_events_high_count       INTEGER NOT NULL DEFAULT 0,
    mem_events_max_count        INTEGER NOT NULL DEFAULT 0,
    io_accounting_available_all_the_time BOOLEAN NOT NULL DEFAULT TRUE,
    sample_count        INTEGER,  -- number of cgroup samples collected
    sample_interval_seconds INTEGER,  -- sampling interval used

    -- Lightweight bottleneck hint, populated from this build's own
    -- per-phase stats. Only ever read from the LATEST row for a given
    -- (package_name, chroot) -- never aggregated/percentiled across
    -- history, to avoid multiplying the lookup logic per phase.
    -- Shape: [{"phase": str, "duration_pct": float, "cpu_saturation": float,
    --          "io_wait_share": float, "bottleneck": "cpu"|"io"|"memory"|"none",
    --          "quality_flag": "ok"|"oom"|"thrashing"}]
    -- `quality_flag` is per-phase, unlike the row-level `oom_kill_count`/
    -- `workingset_refault_*` columns above (which are summed over the
    -- *whole* build) -- it tells you e.g. "the %check phase specifically
    -- was thrashing, not %build", which the whole-build counters alone
    -- can't. Still diagnostic-only: it's read directly off the latest
    -- build like `bottleneck`, never used to filter/percentile history.
    phase_breakdown_latest JSONB,

    -- Optional: the package's BuildRequires at the time of this build,
    -- as sent by Copr backend (see 04-spec-analysis.md's "Data Sources for
    -- Spec Analysis"). Not used by get_resource_profile() at all -- its only
    -- consumer is Layer 3's nearest-neighbor lookup (04-spec-analysis.md),
    -- which needs a corpus of *other* packages' BuildRequires + real
    -- resource profiles to find similar packages for brand-new ones.
    -- Nullable: older reports or callers that don't send it just won't
    -- contribute to that corpus.
    build_requires JSONB,

    -- Indexing
    CONSTRAINT idx_pkg_chroot UNIQUE (package_name, chroot, build_id)
);

CREATE INDEX idx_history_build_requires
    ON build_resource_history USING GIN (build_requires)
    WHERE build_requires IS NOT NULL;

CREATE INDEX idx_history_lookup
    ON build_resource_history (package_name, chroot, recorded_at DESC);
```

A second, much smaller table holds one row per distinct CPU model seen
across all reports, used to normalize `cpu_user_seconds`/`cpu_system_seconds`
across hardware -- see "CPU Hardware Normalization" below:

```sql
CREATE TABLE hardware_calibration (
    cpu_model               TEXT PRIMARY KEY,
    relative_speed_factor   REAL NOT NULL DEFAULT 1.0,
    calibration_source      TEXT NOT NULL DEFAULT 'uncalibrated',
        -- "regression" | "benchmark_prior" | "uncalibrated"
    coremark_score          REAL,  -- stress-ng --cpu, single-thread bogo-ops/sec
    stream_bandwidth_mbps   REAL,  -- stress-ng --stream, aggregate MB/s
    overlap_sample_count    INTEGER NOT NULL DEFAULT 0,
        -- how many cross-model (package, chroot) overlaps back the
        -- regression-derived factor; 0 for benchmark_prior/uncalibrated
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Keep the **last N records** per `(package_name, chroot)`. N=20 is reasonable --
enough for stable percentiles, small enough to not bloat the DB.

Old records (beyond N) can be archived or deleted by a periodic cleanup job.

## Hardware Dependency of Resource Metrics

Not every column above is equally hardware-independent. Being explicit about
which ones aren't -- and what mechanically corrects for it -- is what makes
this layer trustworthy instead of just optimistic:

| Metric | HW dependency | Why | Mitigation |
|---|---|---|---|
| `cpu_user_seconds` + `cpu_system_seconds` | Low w.r.t. core count, **Medium w.r.t. clock speed/IPC** | roughly constant *instruction count* for the same source code regardless of core count -- but the earlier claim that this made the metric flatly "hardware-independent" was wrong: clock frequency, IPC, turbo boost, and thermal throttling all change how many wall-clock CPU-seconds the *same* instructions take on different silicon | cross-model speed correction via `hardware_calibration` -- see "CPU Hardware Normalization" below. Core-count independence still holds and needs no fix. |
| `io_read_gb` + `io_write_gb` | Low | same files read/written regardless of disk speed | none needed |
| `peak_anon_gb` / `mean_anon_gb` | Medium | scales with build parallelism (more cores → more concurrent compiler/linker processes → more concurrent heap) | stratify by `builder_flavor`/`builder_cores` at query time (see below); `peak_anon_gb` itself already strips the opportunistic-page-cache confound that raw `memory.current` would have |
| `cpu_utilization_ratio` (if computed) | High if precomputed | the ratio's denominator (source build's core count) isn't the same as the candidate flavor's core count | never store/compare a precomputed ratio -- keep the absolute `cpu_user_seconds`/`cpu_system_seconds` and compute the ratio against the *candidate* flavor's cores at ranking time (see 05-flavor-optimizer.md) |
| `wall_clock_seconds` | High | this is exactly the metric EMA-of-build-time was rejected for | never used as a resource-need input, only as context |

The quality-signal columns (`oom_kill_count`, `workingset_refault_*`,
`cpu_nr_throttled`, `psi_*_total_usec`, `swap_peak_gb`, `mem_events_*`) are
themselves hardware/environment dependent (they only fire if *that specific*
builder had a tight memory ceiling or CPU quota) -- but that's fine, because
their job isn't to be hardware-independent values. Their job is to flag when
a *specific historical row* was distorted by its own hardware, so it can be
excluded or discounted before it pollutes the aggregate:

- `oom_kill_count > 0` -- the row's memory numbers are a ceiling the build
  hit and died against, not what it actually wanted (capped from above).
- `workingset_refault_anon/file_total > 0` or `pgmajfault_total > 0` without
  an OOM kill -- the build survived but was thrashing (evicted pages had to
  be brought back almost immediately), meaning its memory numbers are
  probably an *under*-estimate (capped from below).
- `cpu_nr_throttled > 0` / `cpu_throttled_seconds` a large fraction of
  `wall_clock_seconds` -- the row's CPU numbers are a lower bound (the build
  wanted more CPU than a quota allowed), not a natural reading.
- `psi_mem_total_usec` / `psi_cpu_total_usec` elevated relative to
  `wall_clock_seconds`, without a hard breach above -- a softer version of
  the same idea, used to discount rather than exclude.
- `swap_peak_gb > 0` together with `mem_events_high_count > 0` --
  corroborates that memory really was tight, used to widen the safety
  margin even when `peak_anon_gb` alone looks moderate.

## CPU Hardware Normalization (Cross-Model Speed Correction)

The row above for `cpu_user_seconds`/`cpu_system_seconds` admits something
the original plan didn't: two different CPU models running the *identical*
instructions produce different core-second readings, because clock
frequency, IPC (instructions per cycle), turbo boost behavior, and thermal
throttling all differ per model. A package built once on an old, low-clocked
builder and once on a new, high-IPC one will show two genuinely different
`cpu_user_seconds` values even with zero build-to-build variance in what the
compiler actually did. Left uncorrected, this pollutes the p95 the same way
mixing a 2-core and 32-core builder's *parallelism* would -- just along a
different axis (per-core speed instead of core count).

**Design goal:** correct for it mechanically (a stored per-CPU-model factor
applied at read/write time), not by asking a model to learn it implicitly --
consistent with the rest of Layer 1 being filter-and-stratify rather than
ML-based.

### Benchmark Input: `stress-ng`

Each distinct CPU model needs two numbers to characterize its speed
relative to others, both obtainable from `stress-ng` (already packaged in
Fedora, no new dependency):

- **`stress-ng --cpu 1 --cpu-method matrixprod --metrics -t 30`** (or
  similar single-thread CPU-bound method) → bogo-ops/sec. Captures
  clock-speed + IPC + turbo behavior for a mixed integer/float workload
  that's a reasonable generic stand-in for "compiling code" -- not chosen to
  match any specific language's instruction mix, just to be a stable,
  reproducible ranking signal.
- **`stress-ng --stream 1 --metrics -t 30`** → aggregate MB/s. Captures
  memory bandwidth, which matters for link-heavy/RAM-heavy phases in a way
  pure CPU throughput doesn't.

Both are collected **once per distinct CPU model**, not once per build --
run during builder provisioning/image build, not on the build's own
critical path. Deliberately excludes disk benchmarks (per earlier
discussion): `io_read_gb`/`io_write_gb` are already volume-based and
disk-speed-independent, and CoreMark isn't packaged in Fedora, which was the
other candidate considered and dropped for that reason.

### Auto-Calibrating Regression

Benchmark scores alone give a *relative* ranking of hardware, but not the
exact multiplicative correction factor needed to normalize `cpu_user_seconds`
-- that requires actual overlap data: the same package built on two
different CPU models. rpmeta already accumulates exactly that, for free, as
`build_resource_history` fills up across a heterogeneous builder fleet.

The regression separates each observed `cpu_user_seconds` reading into a
package effect (how much genuine CPU work this package needs) and a
hardware effect (how fast this specific CPU model does that work), fit
jointly across all observed `(package, cpu_model)` pairs:

```
log(cpu_user_seconds[package, cpu_model])
    = package_effect[package] + hardware_effect[cpu_model] + noise
```

This is a standard two-way fixed-effects regression (equivalent to
package/hardware dummy variables in OLS on the log-transformed readings --
log-space because the relationship is multiplicative, not additive: a CPU
that's 20% faster should divide *every* package's reading by ~1.2, not
subtract a constant). `relative_speed_factor[cpu_model] =
exp(hardware_effect[cpu_model])`, normalized so the fleet-wide median model
sits at 1.0.

The regression only produces a trustworthy factor for a CPU model once
enough *overlapping* packages exist -- a model that's only ever built one
package can't be disentangled from "this package happens to be
CPU-light". `overlap_sample_count` (distinct packages shared with at least
one other calibrated model) gates this: below a threshold (5, matching the
`ram_confidence`/`cpu_confidence` ramp elsewhere in this doc), the
regression factor for that model isn't trusted yet.

The regression re-runs periodically (nightly batch job, not per-request) and
upserts `hardware_calibration` rows with `calibration_source='regression'`.

### Three-Tier Fallback (Cold Start)

A brand-new CPU model has no overlap data on day one, so the lookup falls
through three tiers, from best to worst:

1. **`calibration_source = 'regression'`** -- enough cross-package overlap
   exists (`overlap_sample_count >= 5`). Most accurate, used once available.
2. **`calibration_source = 'benchmark_prior'`** -- no regression yet, but
   `stress-ng` scores were collected for this model at provisioning time.
   `relative_speed_factor` is estimated from a simple linear fit of
   `log(coremark_score)` against the regression-derived factors of *already
   calibrated* models (a cheap prior, replaced by tier 1 the moment enough
   overlap accumulates). Better than assuming parity, worse than a real
   regression fit.
3. **`calibration_source = 'uncalibrated'`** -- brand new model, no
   benchmark run yet either (shouldn't normally happen if builder
   provisioning always runs the `stress-ng` step, but covers the case where
   it didn't). `relative_speed_factor = 1.0` (no correction applied) *and*
   `cpu_confidence` is discounted below -- treated as "don't yet trust this
   data's CPU numbers much," not "assume it's average."

### Applying the Correction

```python
def _normalized_cpu_seconds(row: BuildResourceHistory, session: Session) -> float:
    """
    Project this row's raw CPU-seconds (measured on row.cpu_model) onto a
    hardware-neutral reference speed, by multiplying by that model's
    relative_speed_factor. A model that's 2x faster than the fleet median
    reads *lower* raw seconds for the same work, so its readings get
    multiplied UP to reach what the median-speed hardware would have shown.
    Denormalized back down for a *specific* candidate flavor's own CPU
    model at ranking time -- see 05-flavor-optimizer.md.
    """
    raw = row.cpu_user_seconds + row.cpu_system_seconds
    return raw * _get_speed_factor(session, row.cpu_model)


def _get_speed_factor(session: Session, cpu_model: Optional[str]) -> float:
    if not cpu_model:
        return 1.0  # pre-this-column report, can't calibrate -- treat as neutral
    calib = session.get(HardwareCalibration, cpu_model)
    return calib.relative_speed_factor if calib else 1.0


def _cpu_confidence(cpu_rows: list[BuildResourceHistory], session: Session) -> float:
    base = min(1.0, len(cpu_rows) / 5)
    uncalibrated_fraction = sum(
        1 for r in cpu_rows
        if _calibration_source(session, r.cpu_model) == "uncalibrated"
    ) / len(cpu_rows)
    # Rows measured on uncalibrated hardware carry an unknown speed bias --
    # discount confidence proportionally, on top of the usual sample-count
    # ramp, instead of silently trusting them at face value.
    return base * (1.0 - 0.5 * uncalibrated_fraction)


def _calibration_source(session: Session, cpu_model: Optional[str]) -> str:
    if not cpu_model:
        return "uncalibrated"
    calib = session.get(HardwareCalibration, cpu_model)
    return calib.calibration_source if calib else "uncalibrated"
```

The `ResourceProfile.cpu_core_seconds` value returned by
`get_resource_profile()` is therefore expressed in *reference-hardware*
core-seconds, not raw builder core-seconds -- 05-flavor-optimizer.md must
denormalize it against the *candidate* flavor's own `cpu_model` before
using it to estimate that flavor's build duration (dividing where this
section multiplies).

## Lookup Logic

```python
def get_resource_profile(
    session: Session,
    package_name: str,
    chroot: str,
    n: int = 20,
) -> Optional[ResourceProfile]:
    """
    Compute a p95 resource profile from the last N builds, after filtering
    out rows whose measurements were distorted by their own hardware/quota
    limits (see "Hardware Dependency of Resource Metrics" above).

    Returns None if no history exists, or if every row was disqualified --
    both cases fall through to Layer 2 (ML models).
    """
    rows = (
        session.query(BuildResourceHistory)
        .filter_by(package_name=package_name, chroot=chroot)
        .order_by(BuildResourceHistory.recorded_at.desc())
        .limit(n)
        .all()
    )

    if not rows:
        return None

    # Hard exclusion: a build that got OOM-killed never reached its real
    # peak -- its numbers are a ceiling it died against, not a measurement.
    valid = [r for r in rows if r.oom_kill_count == 0]
    if not valid:
        return None

    # RAM-specific filtering: drop rows that show thrashing (refault/major
    # faults without an OOM kill) -- their peak is an under-estimate, not a
    # clean reading. Fall back to the unfiltered valid set if that would
    # leave nothing (still better than no data at all, just lower confidence).
    ram_rows = [
        r for r in valid
        if not r.workingset_refault_anon_total and not r.workingset_refault_file_total
    ] or valid

    # CPU-specific filtering: drop rows where the CPU quota throttled the
    # build for a significant fraction of its runtime -- those seconds are
    # a lower bound, not what the build actually wanted.
    cpu_rows = [
        r for r in valid
        if r.cpu_throttled_seconds < 0.1 * r.wall_clock_seconds
    ] or valid

    memory_tightness_flag = any(
        r.swap_peak_gb > 0 and r.mem_events_high_count > 0 for r in ram_rows
    )

    return ResourceProfile(
        peak_anon_gb=percentile([r.peak_anon_gb for r in ram_rows], 95),
        cpu_core_seconds=percentile(
            [_normalized_cpu_seconds(r, session) for r in cpu_rows], 95
        ),
        wall_clock_seconds=rows[0].wall_clock_seconds,
        io_total_gb=percentile([r.io_read_gb + r.io_write_gb for r in valid], 95),
        ram_confidence=min(1.0, len(ram_rows) / 5),
        cpu_confidence=_cpu_confidence(cpu_rows, session),
        memory_tightness_flag=memory_tightness_flag,
        bottleneck_hint=_classify_bottleneck(rows[0].phase_breakdown_latest),
        build_count=len(valid),
        source="history",
    )


def _classify_bottleneck(phase_breakdown: Optional[list[dict]]) -> str:
    """
    Diagnostic hint only -- picked from the single most recent build's own
    phase stats, never percentiled across history. See 05-flavor-optimizer.md
    for how it's used to break ties between otherwise-equivalent flavors.
    """
    if not phase_breakdown:
        return "none"

    dominant = max(phase_breakdown, key=lambda p: p["duration_pct"])
    return dominant["bottleneck"]
```

Both `peak_anon_gb` and `cpu_core_seconds` are computed from *independently*
filtered row sets (`ram_rows` vs. `cpu_rows`) -- a row with heavy CPU
throttling but a perfectly clean memory reading still contributes to the RAM
percentile, it's just excluded from the CPU one, and vice versa.

`wall_clock_seconds` is taken directly from `rows[0]` (the single most
recent build), the same convention `_classify_bottleneck()` uses -- unlike
`peak_anon_gb`/`cpu_core_seconds`, it's never percentiled across history
(percentiling wall-clock across builds that may have run on different
hardware would just reproduce the EMA-of-build-time problem this layer was
built to avoid). It exists on `ResourceProfile` purely so
`_avg_concurrent_cores()` in 05-flavor-optimizer.md has *a* real
`(cpu_core_seconds, wall_clock_seconds)` pair to compute observed
parallelism from, not to be a resource-need value in its own right.

## Why p95 Instead of Mean/Max

- **Mean** is too optimistic -- a package that usually uses 10 GB RAM but
  occasionally spikes to 30 GB will average 12 GB. Assigning 12 GB → OOM on
  the spike.
- **Max** is too pessimistic -- one outlier build that used 50 GB (maybe a bug
  or transient issue) would permanently over-provision.
- **p95** is the sweet spot -- covers 95% of builds safely, ignores extreme
  outliers. This is standard practice in cloud right-sizing (AWS Compute
  Optimizer uses p99 for memory).

## Confidence Score

RAM and CPU get **separate** confidence scores, because the quality filters
that produce `ram_rows` and `cpu_rows` discount different rows for different
reasons -- a package with lots of throttled-but-otherwise-clean history has
high RAM confidence but low CPU confidence, and pretending both are equally
trustworthy would either over- or under-provision one of the two dimensions.

```
ram_confidence = min(1.0, len(ram_rows) / 5)
cpu_confidence = min(1.0, len(cpu_rows) / 5) * (1.0 - 0.5 * uncalibrated_fraction)
```

Each ramps from 0.2 (one usable build) to 1.0 (five or more), before the
CPU side is further discounted by `uncalibrated_fraction` -- the share of
`cpu_rows` measured on a CPU model with no `hardware_calibration` entry yet
(see "CPU Hardware Normalization" above). A package with 5 clean builds all
on brand-new, unbenchmarked hardware gets `cpu_confidence=0.5`, not 1.0 --
plenty of *samples*, but an unknown speed bias in every one of them. Used by
the flavor optimizer to decide safety margins independently:

- Low confidence → higher safety margin (1.3x) → more conservative flavor
- High confidence → lower safety margin (1.15x) → tighter, more resource-efficient

`memory_tightness_flag` (set when historical rows show swap usage together
with `memory.high`/`memory.max` breaches, even without an outright OOM kill)
pushes the RAM safety margin up further, independent of `ram_confidence` --
confidence says "how many clean samples do we have", the tightness flag says
"and even the clean ones were cutting it close."

Even a single build gives some confidence (0.2) because one real measurement
is better than an ML prediction for a known package.

## Report Endpoint (`POST /report`)

The Copr backend calls this after each build, forwarding the contents of
mock's `overall_stats.json` almost verbatim (see 01-data-collection.md),
plus its own `builder_flavor` context and the list of raw per-phase stats
files (`{phase}_stats.json`). Deliberately, Copr backend does **not**
classify bottlenecks itself -- it just relays mock's numbers, and rpmeta
does the classification on ingest (see `store_report()` below), keeping all
the "what does this data mean" logic on the rpmeta side:

```json
POST /v1/report
{
    "package_name": "rocblas",
    "chroot": "fedora-42-x86_64",
    "build_id": 12345678,

    "peak_anon_gb": 27.8,
    "peak_file_gb": 3.4,
    "mean_anon_gb": 18.1,
    "cpu_user_seconds": 11200,
    "cpu_system_seconds": 340,
    "io_read_gb": 5.2,
    "io_write_gb": 12.8,

    "wall_clock_seconds": 14400,
    "builder_cores": 4,
    "builder_flavor": "mem-heavy",
    "cpu_model": "AMD EPYC 7763",

    "kernel_peak_rss_gb": 28.1,
    "kernel_peak_swap_gb": 0.0,
    "kernel_peak_available": true,

    "oom_kill_count": 0,
    "workingset_refault_anon_total": 1840,
    "workingset_refault_file_total": 60,
    "pgmajfault_total": 0,
    "cpu_nr_throttled": 0,
    "cpu_throttled_seconds": 0.0,
    "psi_cpu_total_usec": 820000,
    "psi_mem_total_usec": 15000,
    "psi_io_total_usec": 4200000,
    "swap_peak_gb": 0.0,
    "mem_events_high_count": 0,
    "mem_events_max_count": 0,
    "io_accounting_available_all_the_time": true,
    "sample_count": 1440,
    "sample_interval_seconds": 10,

    "phases": [
        {"phase": "build", "duration_seconds": 8424.0, "cpu_user_seconds": 10900,
         "cpu_system_seconds": 300, "cpu_count": 4,
         "psi_cpu_total_usec": 780000, "psi_io_total_usec": 90000,
         "oom_kill_count": 0, "workingset_refault_anon_total": 0,
         "workingset_refault_file_total": 0},
        {"phase": "check", "duration_seconds": 2340.0, "cpu_user_seconds": 220,
         "cpu_system_seconds": 30, "cpu_count": 4,
         "psi_cpu_total_usec": 12000, "psi_io_total_usec": 1400000,
         "oom_kill_count": 0, "workingset_refault_anon_total": 1840,
         "workingset_refault_file_total": 60}
    ],

    "build_requires": ["rust-packaging", "cargo", "gcc", "openblas-devel"]
}
```

`build_requires` is optional and entirely unused by `get_resource_profile()`
above -- it exists purely to grow the corpus that Layer 3's nearest-neighbor
lookup searches for brand-new packages (see "Nearest-Neighbor Lookup" in
04-spec-analysis.md). Copr backend already has this list at build submission
time (it's how dependencies get installed), so forwarding it costs nothing
extra -- and every reported build, regardless of which layer resolved its
own prediction, becomes a potential neighbor for some future unrelated
package.

Everything except `phases` maps directly onto columns of
`build_resource_history` above -- Copr backend doesn't need to compute
anything beyond reading mock's own output files and attaching
`builder_flavor`. `phases` is the raw list of mock's per-phase stats
(trimmed to just the fields needed downstream); `store_report()` turns it
into the stored `phase_breakdown_latest`:

```python
def store_report(session: Session, report: ReportPayload) -> None:
    row = BuildResourceHistory(
        ...,  # direct fields, as received
        phase_breakdown_latest=[
            _build_phase_profile(p, report.wall_clock_seconds)
            for p in report.phases
        ],
    )
    session.add(row)
    session.commit()


def _build_phase_profile(phase: dict, total_wall_clock: float) -> dict:
    cpu_saturation = (
        (phase["cpu_user_seconds"] + phase["cpu_system_seconds"])
        / (phase["duration_seconds"] * phase["cpu_count"])
    ) if phase["duration_seconds"] and phase["cpu_count"] else 0.0

    io_wait_share = (
        phase.get("psi_io_total_usec", 0) / (phase["duration_seconds"] * 1e6)
    ) if phase["duration_seconds"] else 0.0

    mem_wait_share = (
        phase.get("psi_mem_total_usec", 0) / (phase["duration_seconds"] * 1e6)
    ) if phase["duration_seconds"] else 0.0

    return {
        "phase": phase["phase"],
        "duration_pct": phase["duration_seconds"] / total_wall_clock,
        "cpu_saturation": cpu_saturation,
        "io_wait_share": io_wait_share,
        "bottleneck": _classify_single_phase(cpu_saturation, io_wait_share, mem_wait_share),
        "quality_flag": _classify_phase_quality(phase),
    }


def _classify_single_phase(cpu_saturation: float, io_wait_share: float, mem_wait_share: float) -> str:
    if mem_wait_share > 0.3:
        return "memory"
    if io_wait_share > 0.3 and cpu_saturation < 0.5:
        return "io"
    if cpu_saturation > 0.8:
        return "cpu"
    return "none"


def _classify_phase_quality(phase: dict) -> str:
    """
    Per-phase counterpart to the row-level oom_kill_count/workingset_refault_*
    filters in get_resource_profile() -- but diagnostic-only, since
    phase_breakdown_latest is never filtered/percentiled, only read from the
    latest build. Lets a report surface e.g. "%check was thrashing, %build
    was clean" even though the whole-build totals just add both together.
    """
    if phase.get("oom_kill_count", 0) > 0:
        return "oom"
    if (
        phase.get("workingset_refault_anon_total", 0) > 0
        or phase.get("workingset_refault_file_total", 0) > 0
    ):
        return "thrashing"
    return "ok"
```

The `_classify_bottleneck()` used by `get_resource_profile()` earlier in
this document then just picks the dominant phase's already-computed
`bottleneck` field out of `phase_breakdown_latest` -- the per-phase
classification happens once, here, at write time, not on every read.

Protected by HMAC-SHA256 authentication (reuse `auth.py` from the
`receive-data` branch). See the existing implementation for details on
headers (`X-RpmEta-Source`, `X-RpmEta-Signature-256`, `X-RpmEta-Timestamp`).

## Data Retention

- Keep last 20 records per `(package_name, chroot)`.
- Periodic cleanup job (daily cron or DB trigger) deletes older records.
- Estimated DB size: ~100K unique (pkg, chroot) × 20 records × ~500 bytes
  (wider row now, including quality-signal columns and the JSONB phase hint)
  = ~1 GB. Still trivial for PostgreSQL.
- Optionally archive old records to a separate table for trend analysis
  and model retraining.
