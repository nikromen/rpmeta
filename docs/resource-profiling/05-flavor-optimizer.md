# Flavor Optimizer -- Deterministic Flavor Ranking

## This Is NOT Machine Learning

The flavor optimizer is a pure constraint solver. Given a predicted resource
profile and a catalog of available hardware flavors, it filters out flavors
that don't satisfy the resource requirements and ranks the remaining ones
from most to least resource-efficient. It's arithmetic and filtering, not
prediction. Cost/pricing is out of scope entirely -- this is purely about
how well a flavor's capacity matches what the package actually needs.

## Why Separate from ML

ML answers: "How much RAM/CPU/I/O will this build need?"
Flavor optimizer answers: "Given those needs, which of the available
machines fit, and how well?"

The second question has a deterministic answer. If peak anon memory is
28 GB, any machine with <28 GB RAM is unsuitable. That's a fact, not a
prediction. Mixing this logic into an ML model would make it a black box
for no benefit.

Benefits of separation:
- **Debuggable:** "This build got mem-heavy because predicted
  peak_anon_gb=28GB and mem-heavy has 32GB while default has only 16GB."
  Clear reasoning.
- **Configurable:** Add/remove/modify flavors without retraining any model.
- **Deterministic:** Same resource profile → same ranking, always.

## Flavor Catalog

**Primary source: the caller sends its own flavor catalog with the request.**
Copr backend already knows what builder types it actually has (cores, RAM,
disk) -- rpmeta has no business keeping a second, independently maintained
copy of that inventory. A statically configured catalog inevitably drifts
out of sync with reality (a flavor added in Copr but forgotten in rpmeta's
config, or removed in Copr but still recommended by rpmeta).

### Request Schema Addition

`InputRecord` gains an optional field:

```python
class Flavor(BaseModel):
    name: str
    cores: int = Field(gt=0)
    ram_gb: float = Field(gt=0)
    storage_tier: str = "ssd"  # "hdd" | "ssd" | "nvme" | "network"
    cpu_model: Optional[str] = None  # e.g. "AMD EPYC 7763"
    description: Optional[str] = None  # free-text, not used in ranking
```

`storage_tier` is a real ranking input, not decoration -- see "I/O Bottleneck
Tie-Breaking" below. Copr backend already knows what storage each builder
type has, so populating it costs nothing extra.

`cpu_model` is optional but feeds directly into duration estimation: it's
looked up against the same `hardware_calibration` table used to normalize
history (see 02-history-db.md's "CPU Hardware Normalization"), so
`_estimate_duration_hours()` can denormalize `profile.cpu_core_seconds`
(reference-hardware seconds) back onto *this specific* flavor's real speed
before projecting a duration. If omitted, `relative_speed_factor` defaults
to 1.0 (assume reference speed) -- correct on average, but a real value is
strictly better whenever Copr backend knows which CPU model backs a flavor.

```python
class InputRecord(BaseModel):
    ...  # existing fields unchanged
    available_flavors: Optional[list[Flavor]] = None
    max_build_hours: Optional[float] = None  # see "Configurable Duration Ceiling" below
```

Example request payload:

```json
{
  "package_name": "rocblas",
  "chroot": "fedora-42-x86_64",
  "available_flavors": [
    {"name": "tiny",      "cores": 2,  "ram_gb": 4,  "storage_tier": "ssd"},
    {"name": "default",   "cores": 4,  "ram_gb": 16, "storage_tier": "ssd"},
    {"name": "cpu-heavy", "cores": 16, "ram_gb": 16, "storage_tier": "ssd"},
    {"name": "mem-heavy", "cores": 4,  "ram_gb": 64, "storage_tier": "ssd"},
    {"name": "full",      "cores": 16, "ram_gb": 64, "storage_tier": "nvme"}
  ]
}
```

The five example flavors (tiny/default/cpu-heavy/mem-heavy/full) shown
throughout this document are illustrative only -- they represent what a
caller *might* send, not a catalog owned or maintained by rpmeta.

### Fallback Catalog (Legacy / No Flavors Sent)

If `available_flavors` is omitted (an older Copr backend not yet updated,
or an ad-hoc test call), rpmeta falls back to a minimal static default
catalog loaded from its own config:

```toml
[[flavors]]
name = "default"
cores = 4
ram_gb = 16
storage_tier = "ssd"
description = "Fallback used only when the caller sends no available_flavors"
```

This fallback exists purely to keep the service usable during rollout and
for manual testing. It is **not** meant to track real Copr infrastructure --
once Copr backend always sends `available_flavors`, this config can be
reduced to a single generic entry or removed entirely. No per-architecture
catalog files are needed: the caller is responsible for only sending flavors
relevant to the architecture it's building for.

## Ranking Algorithm

The catalog passed into `rank_flavors()` is resolved once per request,
before any constraint solving happens:

```python
def resolve_catalog(input_data: InputRecord, arch: str) -> list[Flavor]:
    """
    Prefer the caller-supplied catalog. Fall back to the static default
    only when the caller hasn't been updated to send `available_flavors`.
    """
    if input_data.available_flavors:
        return input_data.available_flavors
    return load_fallback_catalog(arch)
```

`rank_flavors()` itself doesn't care where the catalog came from -- it's the
same deterministic filter-then-rank logic either way. Unlike a single
"pick one" selector, it returns *all* viable flavors ordered from most to
least resource-efficient, so the caller can see the full picture and decide
itself if it wants to deviate from the top recommendation (e.g. to prefer
whichever flavor currently has free capacity).

**Note on `profile.build_duration_minutes`:** this is not part of the
resource profile produced by Layer 1/2/3 (see 02-history-db.md,
03-ml-models.md, 04-spec-analysis.md) -- those only estimate hardware-
independent resource consumption, not time. `build_duration_minutes` is the
existing (unrelated, hardware-dependent) `build_duration` prediction --
the same value returned as the top-level `prediction` field -- attached to
the profile object before ranking so duration-based filtering has access to
it. When it's unavailable, `_estimate_duration_hours()` returns `None` and
duration-based filtering is simply skipped (never treated as "0 hours").

**Note on `profile.cpu_core_seconds`:** the resource profile does *not*
carry a precomputed `cpu_utilization_ratio`. That ratio's denominator is
whatever core count the historical build happened to run on, which is not
the candidate flavor's core count -- storing the ratio directly would bake
in a hardware confound (see "Hardware Dependency of Resource Metrics" in
02-history-db.md). Instead the profile carries the absolute
`cpu_core_seconds`, and the ratio against each *candidate* flavor's own
core count is computed fresh, right here, in `_compute_efficiency_index()`
and `_estimate_duration_hours()`.

**`profile.cpu_core_seconds` is reference-hardware seconds, not raw
seconds:** as of "CPU Hardware Normalization" in 02-history-db.md, History
DB no longer trusts raw `cpu_user_seconds`/`cpu_system_seconds` at face
value -- clock speed, IPC, and turbo/throttling behavior differ enough
across CPU models that mixing them unnormalized would reproduce the same
confound core-count mixing caused for RAM. `get_resource_profile()`
multiplies each row by its source hardware's `relative_speed_factor` before
percentiling, so what lands in `profile.cpu_core_seconds` is "how many
CPU-seconds this would have taken on the fleet's reference-speed hardware."
`_estimate_duration_hours()` below must **divide by the candidate flavor's
own `relative_speed_factor`** before using it -- this is the exact inverse
of the multiplication History DB did, not a new, independent correction.

**Note on `profile.ram_confidence` / `profile.cpu_confidence`:** History DB
(02-history-db.md) produces these as two separate numbers, because the rows
that get discounted for CPU-quota throttling aren't necessarily the same
rows discounted for memory thrashing. `rank_flavors()` uses each where it's
relevant -- `ram_confidence` for the RAM safety margin, `cpu_confidence` for
how much weight to give `estimated_hours`.

```python
from rpmeta.history_db import _get_speed_factor  # cross-model CPU speed lookup, see 02-history-db.md

# rpmeta config defaults (rpmeta/config.py) -- all three overridable per
# request via InputRecord, see "Configurable Duration Ceiling" and
# "Configurable Efficiency Weights" below.
DEFAULT_MAX_BUILD_HOURS = 8.0
DEFAULT_RAM_WEIGHT = 0.5
DEFAULT_CPU_WEIGHT = 0.5


class FlavorRanking(BaseModel):
    flavor: str
    efficiency_index: float
    estimated_hours: Optional[float]
    reason: str


def rank_flavors(
    profile: ResourceProfile,
    catalog: list[Flavor],
    session: Session,
    max_build_hours: float = DEFAULT_MAX_BUILD_HOURS,
    ram_weight: float = DEFAULT_RAM_WEIGHT,
    cpu_weight: float = DEFAULT_CPU_WEIGHT,
) -> list[FlavorRanking]:
    """
    Filter out flavors that don't satisfy the resource profile, then rank
    the rest from most to least resource-efficient (tightest safe fit first).

    The RAM safety margin scales continuously with `ram_confidence` via
    `_compute_safety_margin()`: confidence=1.0 -> 1.15x, confidence=0.0 ->
    1.5x. In practice: history with 5+ clean builds -> ~1.15x, a single
    historical build (confidence=0.2, see 02-history-db.md) -> ~1.43x, spec
    heuristic (typically confidence 0.1-0.4, see 04-spec-analysis.md) ->
    ~1.36x-1.47x. `memory_tightness_flag` (set when history shows swap use
    together with memory.high/max breaches, even without an outright OOM
    kill) pushes the margin up further, on top of whatever confidence gives.

    `max_build_hours`, `ram_weight`, `cpu_weight` all come from rpmeta's own
    config by default, but can be overridden per-request (see "Configurable
    Duration Ceiling" and "Configurable Efficiency Weights" below) -- neither
    is a resource-profile property, they're operator/caller policy about
    what "efficient" and "acceptable" mean for a given deployment.

    `session` is only needed to resolve each candidate flavor's
    `relative_speed_factor` (see `_estimate_duration_hours()`) -- rank_flavors
    itself does no other DB access.
    """
    safety = _compute_safety_margin(profile.ram_confidence)
    if profile.memory_tightness_flag:
        safety *= 1.1

    viable = []
    for flavor in catalog:
        # Hard constraint: RAM must fit peak anon memory with safety margin
        if flavor.ram_gb < profile.peak_anon_gb * safety:
            continue

        # Estimate wall-clock time on this flavor, filter out anything
        # that would still take unreasonably long
        estimated_hours = _estimate_duration_hours(profile, flavor, session)
        if estimated_hours and estimated_hours > max_build_hours:
            continue

        viable.append(FlavorRanking(
            flavor=flavor.name,
            efficiency_index=_compute_efficiency_index(
                profile, flavor, safety, ram_weight, cpu_weight
            ),
            estimated_hours=estimated_hours,
            reason=_explain_ranking(profile, flavor, safety),
        ))

    if not viable:
        # Nothing fits -- use largest available (fail-safe)
        largest = max(catalog, key=lambda f: f.ram_gb * f.cores)
        return [FlavorRanking(
            flavor=largest.name,
            efficiency_index=0.0,
            estimated_hours=None,
            reason="No flavor satisfies constraints, using largest available",
        )]

    # Most efficient (tightest safe fit) first
    viable.sort(key=lambda r: r.efficiency_index, reverse=True)
    return viable


def _compute_safety_margin(confidence: float) -> float:
    """
    Higher confidence → lower margin (tighter, more efficient fit is safe).
    confidence is 0.0 to 1.0.
    """
    # Linear interpolation: confidence=0 → 1.5x, confidence=1 → 1.15x
    return 1.5 - (confidence * 0.35)


# I/O-bound phases don't get faster with more cores or more RAM -- the only
# lever that helps is faster storage. This multiplies efficiency_index for
# flavors that offer it, so they outrank equally RAM/CPU-suited flavors with
# slower storage when profile.bottleneck_hint says it would actually matter.
_IO_BOTTLENECK_BOOST = {"nvme": 1.25, "ssd": 1.1, "hdd": 1.0, "network": 0.9}


def _avg_concurrent_cores(profile: ResourceProfile) -> Optional[float]:
    """
    Actually-measured average parallelism (in core-equivalents), from the
    historical build the profile was computed from -- NOT the theoretical
    core count of whatever machine it happened to run on. Many build graphs
    don't have enough independent targets to use all available cores, so
    this is usually well below `builder_cores`, and is the more honest cap
    to project onto a *different* flavor's core count: we have no evidence
    the workload would parallelize any better than it already did.
    """
    if not profile.cpu_core_seconds or not profile.wall_clock_seconds:
        return None
    return profile.cpu_core_seconds / profile.wall_clock_seconds


def _compute_efficiency_index(
    profile: ResourceProfile,
    flavor: Flavor,
    safety: float,
    ram_weight: float = DEFAULT_RAM_WEIGHT,
    cpu_weight: float = DEFAULT_CPU_WEIGHT,
) -> float:
    """
    Illustrative fit metric, tunable. 0.0 = flavor is wildly oversized for
    what the package needs (wasteful). 1.0+ = tightest safe fit (no idle
    capacity left over), possibly boosted above 1.0 when the flavor's
    storage tier addresses a detected I/O bottleneck that RAM/CPU fit alone
    wouldn't reward.

    Combines how tightly the flavor's RAM and CPU capacity match the
    resource profile's needs. `ram_weight`/`cpu_weight` default to an equal
    50/50 split (see "Configurable Efficiency Weights" below) -- a
    deployment that consistently sees more memory-bound than CPU-bound
    packages might raise `ram_weight` so a tight RAM fit outranks a tight
    CPU fit more often, without changing anything else about the algorithm.
    """
    needed_ram = profile.peak_anon_gb * safety
    ram_fit = min(needed_ram / flavor.ram_gb, 1.0)

    avg_cores = _avg_concurrent_cores(profile)
    # A flavor with far more cores than the workload's own measured
    # parallelism ceiling just leaves the rest idle -- wasted capacity.
    cpu_fit = min(avg_cores / flavor.cores, 1.0) if avg_cores else 0.5

    index = ram_fit * ram_weight + cpu_fit * cpu_weight

    if profile.bottleneck_hint == "io":
        index *= _IO_BOTTLENECK_BOOST.get(flavor.storage_tier, 1.0)

    return index


def _estimate_duration_hours(
    profile: ResourceProfile,
    flavor: Flavor,
    session: Session,
) -> Optional[float]:
    """
    Rough estimate of build duration on a given flavor, projected from
    reference-hardware CPU work (see the note on `profile.cpu_core_seconds`
    above), denormalized onto this flavor's own CPU speed.

    Phase-aware when `profile.phase_breakdown_latest` is available (History
    DB profiles), instead of one blended whole-build parallelism ceiling.
    This is what fixes an Amdahl's Law blind spot the single-ceiling
    version had: a near-serial %prep phase and a highly-parallel %build
    phase get *different* effective core counts, so adding cores correctly
    speeds up the estimate for the parallel phase while correctly not
    helping the serial one -- a single whole-build average smears the two
    together and either overstates or understates the benefit of more
    cores depending on which phase dominates wall-clock time.

    - CPU-bound work scales up to its own observed parallelism ceiling
      (per-phase where available, whole-build otherwise), not indefinitely
      with core count
    - Non-CPU work (I/O, memory-bound waiting) doesn't scale with cores
    - Swapping (peak_anon_gb > RAM) causes massive slowdown

    Returns None (not 0) when no CPU signal is available, so callers don't
    mistake "unknown" for "instant".
    """
    if not profile.cpu_core_seconds:
        return None

    # profile.cpu_core_seconds (and phase_breakdown_latest's per-phase
    # cpu_core_seconds) are reference-hardware seconds -- divide by this
    # flavor's own relative_speed_factor to get seconds *on this flavor*,
    # the exact inverse of the multiplication get_resource_profile() did.
    speed_factor = _get_speed_factor(session, flavor.cpu_model)

    if profile.phase_breakdown_latest and profile.wall_clock_seconds:
        total_hours = 0.0
        for phase in profile.phase_breakdown_latest:
            phase_wall_seconds = phase["duration_pct"] * profile.wall_clock_seconds
            phase_seconds_on_flavor = phase["cpu_core_seconds"] / speed_factor
            # Recovered directly as core-seconds / wall-seconds, same idea
            # as _avg_concurrent_cores() but scoped to this one phase --
            # no need to know the phase's original cpu_count at all.
            phase_concurrent_cores = (
                phase["cpu_core_seconds"] / phase_wall_seconds
                if phase_wall_seconds else 1.0
            )
            effective_cores = (
                min(flavor.cores, phase_concurrent_cores)
                if phase_concurrent_cores > 1 else 1.0
            )
            total_hours += (phase_seconds_on_flavor / effective_cores) / 3600
        adjusted_hours = total_hours
    else:
        # Fallback: no phase breakdown (Layer 2/3 profiles never populate
        # phase_breakdown_latest, and it's dropped -- see 03-ml-models.md,
        # 04-spec-analysis.md) -- one whole-build parallelism ceiling.
        # Less accurate for mixed serial/parallel workloads, but still
        # correct in the all-serial and all-parallel extremes.
        avg_cores = _avg_concurrent_cores(profile) or 1.0
        effective_cores = min(flavor.cores, avg_cores) if avg_cores > 1 else 1.0
        seconds_on_flavor = profile.cpu_core_seconds / speed_factor
        adjusted_hours = (seconds_on_flavor / effective_cores) / 3600

    # Memory penalty: if peak anon memory exceeds RAM, swapping kills
    # performance
    if profile.peak_anon_gb > flavor.ram_gb * 0.9:
        adjusted_hours *= 5.0  # catastrophic slowdown

    return adjusted_hours


def _explain_ranking(
    profile: ResourceProfile,
    flavor: Flavor,
    safety: float,
) -> str:
    """
    Human-readable explanation of why this flavor ranks where it does.
    Useful for debugging and logging.
    """
    reasons = []

    ram_needed = profile.peak_anon_gb * safety
    if ram_needed > 16:
        reasons.append(
            f"peak_anon={profile.peak_anon_gb:.1f}GB "
            f"(with {safety:.0%} margin: {ram_needed:.1f}GB) "
            f"requires {flavor.ram_gb}GB RAM"
        )

    if profile.bottleneck_hint == "cpu":
        reasons.append(
            f"dominant phase is CPU-bound, benefits from {flavor.cores} cores"
        )
    elif profile.bottleneck_hint == "io":
        reasons.append(
            f"dominant phase is I/O-bound (more cores won't help); "
            f"{flavor.storage_tier} storage tier"
        )
    elif profile.bottleneck_hint == "memory":
        reasons.append("dominant phase is memory-bound (thrashing risk)")

    if not reasons:
        reasons.append("satisfies all constraints")

    return "; ".join(reasons)
```

### Configurable Duration Ceiling

`max_build_hours` was previously hardcoded as a default argument
(`= 8.0`) with no way to change it short of editing code. It's now sourced
from rpmeta config by default, with an optional per-request override on
`InputRecord`:

```python
def resolve_max_build_hours(input_data: InputRecord, config: RpmetaConfig) -> float:
    return input_data.max_build_hours or config.default_max_build_hours
```

This is deliberately **not** the same thing as "make builds finish in a
20-40 minute window." That would require reasoning in wall-clock time,
which is exactly the hardware-dependent quantity this whole design avoids
optimizing against directly (see "Why Not EMA" in 02-history-db.md). What
`max_build_hours` *can* honestly do is act as a generous safety ceiling:
"never recommend a flavor so weak this would run past N hours" -- a
one-sided filter that excludes absurd outcomes, not a target the ranking
actively steers towards. A caller wanting builds to typically land in a
target window should instead treat `max_build_hours` as a coarse dial
(e.g. lower it for a queue that needs to drain fast) while accepting that
individual packages will still land wherever their actual resource profile
and the available catalog put them -- the efficiency-first ranking already
avoids handing out an oversized flavor "just in case," so a heavy package
naturally gets a stronger (faster) flavor and a light one naturally gets a
weaker (slower, cheaper) one without needing an explicit target-time input
at all.

### Configurable Efficiency Weights

`ram_weight`/`cpu_weight` in `_compute_efficiency_index()` default to an
even 50/50 split between how tightly RAM and CPU capacity are used. Like
`max_build_hours`, these come from rpmeta config, with the same
per-request override shape:

```python
class InputRecord(BaseModel):
    ...
    ram_weight: Optional[float] = None
    cpu_weight: Optional[float] = None
```

This is an operator-level tuning knob, not something an individual build
request would normally set -- it changes what "efficient" means for *every*
ranking on a given rpmeta deployment (e.g. a fleet that's chronically
RAM-constrained might raise `ram_weight` so the ranking rewards a tight RAM
fit more than a tight CPU fit, fleet-wide), whereas `max_build_hours` is
closer to a per-caller policy (e.g. a CI queue that needs to drain fast).
Both are exposed the same way on `InputRecord` for consistency, but are
expected to be set once in config and left alone in the common case.

## Response Format

The `/predict` endpoint returns the full ranking plus the top recommendation:

```json
{
    "prediction": 195,
    "resource_profile": {
        "peak_anon_gb": 31.2,
        "cpu_core_seconds": 41800,
        "wall_clock_seconds": 11700,
        "io_total_gb": 14.5,
        "ram_confidence": 1.0,
        "cpu_confidence": 1.0,
        "memory_tightness_flag": false,
        "bottleneck_hint": "cpu",
        "source": "history"
    },
    "flavor_ranking": [
        {
            "flavor": "mem-heavy",
            "efficiency_index": 0.73,
            "estimated_hours": 3.25,
            "reason": "peak_anon=31.2GB (with 115% margin: 35.9GB) requires 64GB RAM; dominant phase is CPU-bound, benefits from 4 cores"
        },
        {
            "flavor": "full",
            "efficiency_index": 0.39,
            "estimated_hours": 3.25,
            "reason": "peak_anon=31.2GB (with 115% margin: 35.9GB) requires 64GB RAM; dominant phase is CPU-bound, benefits from 16 cores"
        }
    ],
    "recommended_flavor": "mem-heavy",
    "catalog_source": "request",
    "used_configuration": {"time_format": "minutes"}
}
```

`cpu_core_seconds=41800` over `wall_clock_seconds=11700` means the historical
build averaged `41800/11700 ≈ 3.57` concurrent cores. Both `mem-heavy` (4
cores) and `full` (16 cores) have at least that many, so `_estimate_duration_hours()`
caps `effective_cores` at the workload's own observed parallelism ceiling for
*both* -- there's no evidence it would run any faster with 16 cores than with
4, so both get the identical `3.25` estimate. `mem-heavy` still ranks above
`full` in `efficiency_index` (0.73 vs 0.39) because `full`'s extra 12 cores
buy nothing here (`cpu_fit` drops from ~0.89 to ~0.22) -- pure wasted
capacity, with no compensating speed benefit to offset it.

### I/O Bottleneck Tie-Breaking

When `bottleneck_hint == "io"` (the dominant phase in the most recent build
spent most of its time waiting on disk, not CPU -- see 02-history-db.md's
`_classify_bottleneck()`), `_compute_efficiency_index()` boosts flavors with
faster `storage_tier` via `_IO_BOTTLENECK_BOOST`, and `_explain_ranking()`
says so explicitly instead of talking about cores:

```json
{
    "resource_profile": {
        "peak_anon_gb": 6.1,
        "cpu_core_seconds": 3200,
        "wall_clock_seconds": 9400,
        "io_total_gb": 40.2,
        "bottleneck_hint": "io",
        "ram_confidence": 0.8,
        "cpu_confidence": 0.8,
        "source": "history"
    },
    "flavor_ranking": [
        {
            "flavor": "io-optimized",
            "efficiency_index": 0.61,
            "estimated_hours": 2.61,
            "reason": "dominant phase is I/O-bound (more cores won't help); nvme storage tier"
        },
        {
            "flavor": "default-hdd",
            "efficiency_index": 0.49,
            "estimated_hours": 2.61,
            "reason": "dominant phase is I/O-bound (more cores won't help); hdd storage tier"
        }
    ],
    "recommended_flavor": "io-optimized",
    "catalog_source": "request"
}
```

Both candidate flavors here have identical `cores=8, ram_gb=8` -- same
`ram_fit`, same `cpu_fit`, same `estimated_hours` -- so this isolates exactly
what the tie-break contributes: `io-optimized`'s `nvme` tier applies a 1.25x
boost vs. `default-hdd`'s 1.0x, which is the entire reason it ranks first.
Without `storage_tier` in the `Flavor` model, these two would have looked
identical and the ranking would have had nothing to break the tie with.

- `flavor_ranking` lists every flavor from the caller's catalog that
  satisfies the resource profile, ordered from most to least efficient
  (tightest safe fit first). Flavors that don't fit (not enough RAM, or
  would take too long) are simply excluded, not included with a low score.
- `recommended_flavor` is just the `flavor` field of the first entry in
  `flavor_ranking` -- exposed as a separate field so simple consumers don't
  need to parse the list if they just want "the" recommendation.
- `catalog_source` is `"request"` when the ranking was computed against the
  caller's own `available_flavors`, or `"fallback_default"` when the caller
  didn't send any and rpmeta used its static default. Monitoring this field
  is a cheap way to notice Copr backend instances that haven't been updated
  to send their real flavor list yet.

## Backward Compatibility

The existing `/predict` response has:
```json
{"prediction": 195, "used_configuration": {"time_format": "minutes"}}
```

New fields (`resource_profile`, `flavor_ranking`, `recommended_flavor`,
`catalog_source`) are added as optional, and the new request field
(`available_flavors`) is also optional. This gives a two-sided,
independently rollout-able migration:

- Old Copr backend + new rpmeta: no `available_flavors` sent -> rpmeta uses
  the fallback catalog -> response includes a ranking computed against
  generic defaults (not very useful, but harmless). Old backend ignores the
  new fields and keeps using `prediction` with its existing threshold-based
  logic.
- New Copr backend + new rpmeta: `available_flavors` sent -> ranking is
  computed against the caller's real infrastructure -> backend switches to
  using `recommended_flavor` (or picks from `flavor_ranking` itself) for
  hardware assignment.

Either side can be updated first without breaking the other.
