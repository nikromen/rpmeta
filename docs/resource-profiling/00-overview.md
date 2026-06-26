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

## Solution: Resource Profiling + Cost-Optimal Scheduling

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
              │  peak_rss, cpu_     │
              │  ratio, io, net     │
              └────────┬────────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  Cost Optimizer     │  NOT ML. Constraint solver.
              │  (flavor matching)  │  Pick cheapest flavor that
              │                     │  satisfies all constraints.
              └────────┬────────────┘
                       │
                       ▼
              ┌─────────────────────┐
              │  Assigned Flavor    │
              │  (e.g. "mem-heavy") │
              └─────────────────────┘

                    After build:
              ┌─────────────────────┐
              │  Collect cgroup +   │
              │  network + phase    │──▶ History DB update
              │  metrics            │──▶ Retrain queue
              └─────────────────────┘
```

## Key Insight: Resource Profiles Are Hardware-Independent

Build time depends on hardware (faster CPU = less time). But resource
**consumption** is a property of the PACKAGE:

| Metric              | Depends on HW? | Why                                      |
|---------------------|-----------------|------------------------------------------|
| peak_rss_gb         | No              | Code allocates same memory on any machine |
| io_write_gb         | No              | Same data written regardless of disk speed |
| cpu_utilization     | No              | Ratio of CPU time to wall-clock * cores   |
| network_download_gb | No              | Same crates/modules downloaded anywhere   |
| build_duration      | YES             | Faster CPU/more cores = less time         |

Because resource profiles are hardware-independent, they do NOT suffer from the
feedback loop problem that plagues time-based predictions. A package that
allocates 30 GB RAM will allocate 30 GB whether it runs on a 16 GB or 64 GB
machine.

## Flavor Catalog (Example)

Instead of binary "normal/powerful", define a catalog of hardware profiles:

| Flavor     | CPU  | RAM   | Disk | Use Case                          |
|------------|------|-------|------|-----------------------------------|
| tiny       | 2    | 4 GB  | SSD  | noarch, Python, scripts           |
| default    | 4    | 16 GB | SSD  | Most packages                     |
| cpu-heavy  | 8-16 | 16 GB | SSD  | Large C/C++ compilations          |
| mem-heavy  | 4    | 32-64 GB | SSD | Linking, rocblas, large Rust   |
| full       | 16   | 64 GB | NVMe | Everything-intensive              |

~5 flavors per architecture. Adding a new flavor = adding a line to config,
no code changes.

## Document Index

- [01-data-collection.md](01-data-collection.md) -- What metrics to collect and how
- [02-history-db.md](02-history-db.md) -- History lookup layer (no ML)
- [03-ml-models.md](03-ml-models.md) -- ML prediction for unknown packages
- [04-spec-analysis.md](04-spec-analysis.md) -- Cold-start fallback from spec files
- [05-cost-optimizer.md](05-cost-optimizer.md) -- Deterministic flavor matching
- [06-implementation-roadmap.md](06-implementation-roadmap.md) -- Phased rollout plan
