# Cost Optimizer -- Deterministic Flavor Matching

## This Is NOT Machine Learning

The cost optimizer is a pure constraint solver. Given a predicted resource
profile and a catalog of available hardware flavors, it selects the cheapest
flavor that satisfies all resource requirements. It's arithmetic and filtering,
not prediction.

## Why Separate from ML

ML answers: "How much RAM/CPU/I/O will this build need?"
Cost optimizer answers: "Given those needs, which machine is cheapest?"

The second question has a deterministic answer. If peak RSS is 28 GB, any
machine with <28 GB RAM is unsuitable. That's a fact, not a prediction.
Mixing this logic into an ML model would make it a black box for no benefit.

Benefits of separation:
- **Debuggable:** "This build got mem-heavy because predicted peak_rss=28GB
  and mem-heavy has 32GB while default has only 16GB." Clear reasoning.
- **Configurable:** Add/remove/modify flavors without retraining any model.
- **Deterministic:** Same resource profile → same flavor, always.

## Flavor Catalog

Defined in config (TOML):

```toml
[[flavors]]
name = "tiny"
cores = 2
ram_gb = 4
disk_type = "ssd"
cost_per_hour = 0.03
description = "noarch, Python, scripts, small packages"

[[flavors]]
name = "default"
cores = 4
ram_gb = 16
disk_type = "ssd"
cost_per_hour = 0.08
description = "Standard builder for most packages"

[[flavors]]
name = "cpu-heavy"
cores = 16
ram_gb = 16
disk_type = "ssd"
cost_per_hour = 0.20
description = "Large C/C++ compilations, parallel builds"

[[flavors]]
name = "mem-heavy"
cores = 4
ram_gb = 64
disk_type = "ssd"
cost_per_hour = 0.25
description = "Memory-intensive linking, rocblas, large Rust"

[[flavors]]
name = "full"
cores = 16
ram_gb = 64
disk_type = "nvme"
cost_per_hour = 0.60
description = "Maximum resources for extreme builds"
```

### Architecture-Specific Catalogs

Different architectures have different available flavors. The catalog is
per-architecture:

```toml
[flavor_catalogs]
x86_64 = "flavors-x86_64.toml"
aarch64 = "flavors-aarch64.toml"
ppc64le = "flavors-ppc64le.toml"
s390x = "flavors-s390x.toml"
```

## Selection Algorithm

```python
def select_flavor(
    profile: ResourceProfile,
    catalog: list[Flavor],
    max_build_hours: float = 8.0,
) -> FlavorSelection:
    """
    Select the cheapest flavor that satisfies the resource profile.

    Safety margins depend on prediction confidence:
    - High confidence (history with many builds): 1.15x
    - Medium confidence (few builds or ML): 1.3x
    - Low confidence (spec heuristic): 1.5x
    """
    safety = _compute_safety_margin(profile.confidence)

    viable = []
    for flavor in catalog:
        # Hard constraint: RAM must fit peak RSS with safety margin
        if flavor.ram_gb < profile.peak_rss_gb * safety:
            continue

        # Estimate wall-clock time on this flavor
        estimated_hours = _estimate_duration_hours(profile, flavor)
        if estimated_hours > max_build_hours:
            continue

        # Compute total cost for this build on this flavor
        cost = flavor.cost_per_hour * estimated_hours

        viable.append(FlavorSelection(
            flavor=flavor,
            estimated_hours=estimated_hours,
            estimated_cost=cost,
            reason=_explain_selection(profile, flavor, safety),
        ))

    if not viable:
        # Nothing fits -- use largest available (fail-safe)
        largest = max(catalog, key=lambda f: f.ram_gb * f.cores)
        return FlavorSelection(
            flavor=largest,
            estimated_hours=None,
            estimated_cost=None,
            reason="No flavor satisfies constraints, using largest available",
        )

    # Sort by cost, pick cheapest
    viable.sort(key=lambda s: s.estimated_cost)
    return viable[0]


def _compute_safety_margin(confidence: float) -> float:
    """
    Higher confidence → lower margin (more cost-efficient).
    confidence is 0.0 to 1.0.
    """
    # Linear interpolation: confidence=0 → 1.5x, confidence=1 → 1.15x
    return 1.5 - (confidence * 0.35)


def _estimate_duration_hours(
    profile: ResourceProfile,
    flavor: Flavor,
) -> float:
    """
    Rough estimate of build duration on a given flavor.

    Based on the CPU utilization ratio and available cores:
    - CPU-bound work scales (imperfectly) with core count
    - Non-CPU work (I/O, network) doesn't scale with cores
    - Swapping (peak_rss > RAM) causes massive slowdown
    """
    if profile.build_duration_minutes is None:
        # No duration estimate available, can't filter by time
        return 0

    baseline_minutes = profile.build_duration_minutes
    cpu_ratio = profile.cpu_utilization_ratio

    # CPU-bound portion scales with cores (Amdahl's law, imperfect)
    baseline_cores = 4  # assume profile was measured on default builder
    core_speedup = min(
        flavor.cores / baseline_cores,
        4.0,  # diminishing returns cap
    )

    # Memory penalty: if peak RSS exceeds RAM, swapping kills performance
    mem_penalty = 1.0
    if profile.peak_rss_gb > flavor.ram_gb * 0.9:
        mem_penalty = 5.0  # catastrophic slowdown

    adjusted_minutes = baseline_minutes * (
        cpu_ratio / core_speedup +  # CPU-bound part (scales)
        (1 - cpu_ratio)             # non-CPU part (doesn't scale)
    ) * mem_penalty

    return adjusted_minutes / 60


def _explain_selection(
    profile: ResourceProfile,
    flavor: Flavor,
    safety: float,
) -> str:
    """
    Human-readable explanation of why this flavor was selected.
    Useful for debugging and logging.
    """
    reasons = []

    rss_needed = profile.peak_rss_gb * safety
    if rss_needed > 16:
        reasons.append(
            f"peak_rss={profile.peak_rss_gb:.1f}GB "
            f"(with {safety:.0%} margin: {rss_needed:.1f}GB) "
            f"requires {flavor.ram_gb}GB RAM"
        )

    if profile.cpu_utilization_ratio > 0.8:
        reasons.append(
            f"cpu_ratio={profile.cpu_utilization_ratio:.2f} "
            f"(CPU-bound, benefits from {flavor.cores} cores)"
        )

    if not reasons:
        reasons.append(f"cheapest flavor satisfying all constraints")

    return "; ".join(reasons)
```

## Response Format

The `/predict` endpoint returns the selection along with explanation:

```json
{
    "prediction": 195,
    "resource_profile": {
        "peak_rss_gb": 31.2,
        "cpu_utilization_ratio": 0.95,
        "io_total_gb": 14.5,
        "net_download_gb": 0.1,
        "confidence": 0.9,
        "source": "history"
    },
    "flavor_recommendation": {
        "flavor": "mem-heavy",
        "estimated_hours": 3.2,
        "estimated_cost": 0.80,
        "reason": "peak_rss=31.2GB (with 115% margin: 35.9GB) requires 64GB RAM; cpu_ratio=0.95 (CPU-bound, benefits from 4 cores)"
    },
    "used_configuration": {"time_format": "minutes"}
}
```

## Backward Compatibility

The existing `/predict` response has:
```json
{"prediction": 195, "used_configuration": {"time_format": "minutes"}}
```

New fields (`resource_profile`, `flavor_recommendation`) are added as optional.
Existing consumers (Copr backend) continue working -- they read `prediction`
and ignore unknown fields.

The Copr backend can be updated to use `flavor_recommendation.flavor` for
hardware assignment when ready, while still falling back to the
threshold-based logic if the field is absent.

## Cost Data Sources

For actual cloud deployments, `cost_per_hour` comes from cloud provider pricing.
For on-premise Copr (AWS-like infra), assign relative costs based on resource
proportions:

```
cost ≈ (cores / baseline_cores) * core_weight +
       (ram_gb / baseline_ram) * ram_weight +
       disk_multiplier
```

The absolute numbers don't matter as long as the relative ordering is correct
(cheaper flavors have lower cost). The optimizer selects the cheapest viable
option.
