# Resource-Aware Build Scheduling for rpmeta

## Problem Statement

rpmeta currently predicts **build duration** (minutes) from package metadata +
static hardware specs. This prediction drives a binary decision: "normal builder"
or "powerful builder" (threshold-based).

This approach has fundamental limitations:

1. **Build time is a CONSEQUENCE, not a cause.** A build takes long because it
   needs lots of CPU, or lots of RAM, or downloads many dependencies. Predicting
   time without understanding the resource profile is guessing at the symptom.

2. **Feedback loop with EMA/history of build times.** If a package gets a
   powerful builder because its predicted time is high, it finishes faster on
   powerful hardware, which lowers the time prediction, which removes the
   powerful builder next time, which makes it slow again. This oscillation is
   inherent to any time-based approach where the hardware assignment affects the
   measured time.

3. **Binary choice is wasteful.** Some packages need more RAM but not more CPU.
   Some are network-bound during dependency installation. "Normal vs powerful" is
   too coarse -- it either over-provisions (waste) or under-provisions (slow/OOM).

4. **No data.** Copr currently collects zero runtime resource metrics. Only
   wall-clock start/end timestamps. Without data, no model can learn resource
   profiles.

## Solution: Resource Profiling + Efficient Flavor Scheduling

Replace the single "predict build time" model with a multi-layered system:

```
┌─────────────────────────────────────────────────────────┐
│                    Build Request                        │
│           (package_name, chroot, spec file)             │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
              ┌─────────────────────┐
              │  Layer 1: History   │  Known (pkg, chroot)?
              │  DB Lookup (p95)    │  Use real measured data.
              └────────┬────────────┘
                       │ miss
                       ▼
              ┌─────────────────────┐
              │  Layer 2: ML Models │  Known package?
              │  (resource predict) │  Predict resource vector.
              └────────┬────────────┘
                       │ miss
                       ▼
              ┌─────────────────────┐
              │  Layer 3: Spec      │  Unknown package?
              │  Analysis (heurist) │  Estimate from spec file.
              └────────┬────────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  Resource Profile   │
              │  peak_anon_gb,      │
              │  cpu_core_seconds,  │
              │  io, bottleneck_hint│
              └────────┬────────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  Flavor Optimizer   │  NOT ML. Constraint solver.
              │  (flavor ranking)   │  Rank flavors that satisfy
              │                     │  constraints, most efficient
              │                     │  fit first. No cost/pricing.
              └────────┬────────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  Flavor Ranking +   │
              │  Recommended Flavor │
              │  (e.g. "mem-heavy") │
              └─────────────────────┘

                   During + after build:
              ┌─────────────────────┐
              │  mock's             │
              │  system_monitor     │──▶ samples.jsonl (raw time-series)
              │  plugin (cgroup v2  │──▶ {phase}_stats.json / overall_stats.json
              │  sampling, already  │──▶ History DB update via /report
              │  implemented)       │──▶ Retrain queue
              └─────────────────────┘
```

## Key Insight: Resource Profiles Are Hardware-Independent (Mostly)

Build time depends on hardware (faster CPU = less time). But resource
**consumption** is close to a property of the PACKAGE, not the machine --
with caveats the pipeline is specifically designed to detect and correct
for, not to hand-wave away:

| Metric                                | Depends on HW? | Why                                                                     |
|-----------------------------------------|-----------------|----------------------------------------------------------------------------|
| `cpu_core_seconds`                     | Low             | Total CPU work done is roughly constant for the same source code         |
| `io_total_gb`                          | Low             | Same data read/written regardless of disk speed                          |
| `peak_anon_gb`                         | Medium          | Scales somewhat with build parallelism (more cores → more concurrent processes); corrected for via stratification and quality filters in 02-history-db.md |
| `cpu_utilization_ratio` (never stored) | High if precomputed | Denominator would be whichever core count the *measuring* machine had -- computed fresh against the *candidate* flavor's cores instead, never persisted |
| `build_duration`                       | High            | Faster CPU/more cores = less time; never used as a resource-need input   |

Because the resource-need values above are (mostly) hardware-independent,
they do NOT suffer from the feedback loop problem that plagues time-based
predictions. A package that allocates 30 GB of genuine anonymous memory will
allocate close to that whether it runs on a 16 GB or 64 GB machine -- the
residual hardware dependency is handled explicitly by History DB's lookup
logic (stratification, OOM/thrashing/throttling filters), not ignored.

`sustained_high_rss_pct` and `net_download_gb`, present in earlier versions
of this document, were both dropped -- see 02-history-db.md and
03-ml-models.md for why.

## Flavor Catalog (Example)

Instead of binary "normal/powerful", think in terms of a catalog of hardware
profiles:

| Flavor     | CPU  | RAM   | Disk | Use Case                          |
|------------|------|-------|------|-----------------------------------|
| tiny       | 2    | 4 GB  | SSD  | noarch, Python, scripts           |
| default    | 4    | 16 GB | SSD  | Most packages                     |
| cpu-heavy  | 8-16 | 16 GB | SSD  | Large C/C++ compilations          |
| mem-heavy  | 4    | 32-64 GB | SSD | Linking, rocblas, large Rust   |
| full       | 16   | 64 GB | NVMe | Everything-intensive              |

This catalog is **not** owned or persisted by rpmeta. The caller (Copr
backend) sends its own `available_flavors` list with each prediction request
(see 05-flavor-optimizer.md) -- it already knows what builders it has, so
there is no separate config for rpmeta to keep in sync. rpmeta only falls
back to a minimal static default catalog when a caller doesn't send one
(e.g. during rollout of this feature).

## Document Index

- [01-data-collection.md](01-data-collection.md) -- What metrics to collect and how
- [02-history-db.md](02-history-db.md) -- History lookup layer (no ML)
- [03-ml-models.md](03-ml-models.md) -- ML prediction for unknown packages
- [04-spec-analysis.md](04-spec-analysis.md) -- Cold-start fallback from spec files
- [05-flavor-optimizer.md](05-flavor-optimizer.md) -- Deterministic flavor ranking (no cost/pricing)
- [06-implementation-roadmap.md](06-implementation-roadmap.md) -- Phased rollout plan
