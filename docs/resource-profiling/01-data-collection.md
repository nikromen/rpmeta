# Data Collection

## Current State

Copr's own backend collects **zero runtime resource metrics** during builds.
The only per-build data is:

- `started_on` / `ended_on` timestamps (wall-clock)
- `results.json` with NEVRA (package metadata, no resource info)
- `hw_info.log` with static builder specs (lscpu + free output)

**However, this is now solved on the mock side, not something rpmeta/Copr
backend needs to build.** Mock's `system_monitor` plugin
(`mock/py/mockbuild/plugins/system_monitor.py`,
`docs/Plugin-SystemMonitor.md` in the mock repo) already implements
periodic cgroup v2 sampling with real per-phase tagging via mock's own
build hooks (not log-timestamp inference). What's described below as
"what to collect" and "implementation" reflects what that plugin *actually
produces today*, not a new component to build in `copr-rpmbuild`. The only
work remaining on the Copr side is: read the plugin's output files after
each build and forward the summary to rpmeta's `POST /report` endpoint
(see 02-history-db.md) -- no sampling code of its own.

## What to Collect

### CPU Metrics (from cgroups v2 `cpu.stat`)

| Metric            | cgroup file      | Field             | What it tells you                   |
|-------------------|------------------|-------------------|-------------------------------------|
| User CPU time     | `cpu.stat`       | `user_usec`       | Time spent in user-space code       |
| System CPU time   | `cpu.stat`       | `system_usec`     | Time spent in kernel (I/O, syscalls)|
| Throttled periods | `cpu.stat`       | `nr_throttled`    | How often CPU limit was hit         |
| Throttled time    | `cpu.stat`       | `throttled_usec`  | Total time throttled                |
| CPU pressure      | `cpu.pressure`   | `some ... total=` | Cumulative usec tasks waited for CPU |

**Derived metric:** `cpu_core_seconds = (user_usec + system_usec) / 1e6`.
Kept as an absolute value, not a ratio -- see "Why not `cpu_utilization_ratio`"
below.

**Why not `cpu_utilization_ratio`:** an earlier version of this doc proposed
`cpu_utilization_ratio = (user_usec + system_usec) / (wall_clock_usec *
cores)` as "hardware-independent". It isn't: the ratio's denominator is
whatever core count the *measuring* machine happened to have, so the same
package looks CPU-bound (ratio near 1.0) on a 2-core machine and idle (ratio
near 0.1) on a 32-core machine, despite doing identical work. The fix isn't
to compute a "better" ratio -- it's to never store one. Keep the absolute
`cpu_core_seconds` and compute utilization against whatever core count is
actually relevant (the candidate flavor's) at ranking time
(05-flavor-optimizer.md).

`nr_throttled`/`throttled_usec` matter beyond raw collection too: if a
historical build was throttled by a CPU quota for a meaningful fraction of
its runtime, its `cpu_core_seconds` reading is a lower bound (the build
wanted more CPU than it got), not a clean measurement -- see 02-history-db.md
for how this is used to discount such rows.

### Memory Metrics (from cgroups v2 `memory.*`)

| Metric                 | cgroup file           | Field                                                  | What it tells you |
|-------------------------|------------------------|---------------------------------------------------------|--------------------|
| Peak RSS (kernel-exact) | `memory.peak`          | (single value)                                          | Maximum memory used, kernel high-water mark |
| Peak swap               | `memory.swap.peak`     | (single value)                                          | Maximum swap used |
| Current usage           | `memory.current`       | (single value)                                          | Current memory (for time-series) |
| Anon vs. file split     | `memory.stat`          | `anon`, `file`                                          | How much of current usage is genuine need vs. reclaimable page cache |
| Major page faults       | `memory.stat`          | `pgmajfault`                                             | Disk-backed page faults (I/O hit) |
| Thrashing               | `memory.stat`          | `workingset_refault_anon`, `workingset_refault_file`     | A reclaimed page had to come back almost immediately -- stronger "genuinely too tight" signal than pressure alone |
| Swap usage              | `memory.swap.current`  | (single value)                                          | How much swap is used |
| Limit breaches          | `memory.events`        | `oom`, `oom_kill`, `max`, `high`, `low`                  | Exact counts of hitting/breaching configured limits, including fatal OOM kills |
| Memory pressure         | `memory.pressure`      | `some ... total=`                                       | Cumulative usec tasks waited for memory |

**Key metric:** `peak_anon_gb` = the `anon` value from `memory.stat` at the
sample where `memory.current` peaked, cross-checked against the kernel-exact
`memory.peak` for the whole build. This is preferred over raw
`memory.current`/`memory.peak` because the raw value also counts
reclaimable page cache, which inflates with how much RAM happens to be
free -- not with what the build actually needs resident.

**`oom_kill_count > 0` invalidates the reading entirely:** a build that got
OOM-killed never reached its real peak -- what got measured is a ceiling it
died against, not a resource need. See 02-history-db.md for how such rows
are excluded from percentile lookups rather than silently distorting them.

### I/O Metrics (from cgroups v2 `io.stat`)

| Metric        | cgroup file    | Field    | What it tells you              |
|---------------|----------------|----------|--------------------------------|
| Bytes read    | `io.stat`      | `rbytes` | Total data read from disk      |
| Bytes written | `io.stat`      | `wbytes` | Total data written from disk   |
| Read ops      | `io.stat`      | `rios`   | Number of read operations      |
| Write ops     | `io.stat`      | `wios`   | Number of write operations     |
| I/O pressure  | `io.pressure`  | `some ... total=` | Cumulative usec tasks waited for I/O |

Only **volume** (`rbytes`/`wbytes`) is used as a resource-need value.
Throughput (GB/s) is deliberately not computed or collected anywhere in this
pipeline -- it's a disk-speed property, not a package property, and would be
actively misleading if fed into flavor sizing the same way volume is.

### Network Metrics -- Decided: Not Collected

An earlier version of this doc proposed several options for network
accounting (`/proc/net/dev` delta, iptables accounting, proxy logs). This is
now explicitly **not implemented and not planned**: Copr builders don't have
network access during `%build`/`%check` -- the only network activity is
dependency resolution/download before the build proper starts, and
cgroup/namespace-level accounting for that narrow window isn't worth the
implementation complexity it would take. `net_download_gb` does not appear
anywhere in mock's output, the History DB schema, or the ML targets.

### Build Phase Timing

Mock/rpmbuild executes builds in well-defined phases with different resource
characteristics:

| Phase        | rpmbuild section | Resource characteristics                    |
|--------------|------------------|---------------------------------------------|
| dep_install  | (mock setup)     | Network-heavy (downloading packages)        |
| %prep        | `%prep`          | I/O (unpacking sources), usually fast       |
| %build       | `%build`         | CPU-heavy (compilation), often RAM-heavy    |
| %check       | `%check`         | CPU + RAM (running tests), sometimes slow   |
| %install     | `%install`       | I/O (copying files), usually fast           |
| packaging    | (rpmbuild)       | I/O + CPU (compression of RPMs)             |

**This is now exact, not inferred.** An earlier version of this doc noted
that per-phase timing isn't recoverable from `build.log` timestamps, and
proposed inferring phase transitions from time-series correlation (e.g. "a
CPU spike after a network-heavy period marks the dep_install → %build
transition") as a workaround. That workaround is no longer needed: mock's
`system_monitor` plugin tags every sample with `current_phase` set directly
from mock's own build hooks as they fire, not from post-hoc inference. Each
phase gets its own `{phase_name}_stats.json` (see below) with exact
boundaries.

## Implementation

**This entire section describes mock's existing `system_monitor` plugin**
(`mock/py/mockbuild/plugins/system_monitor.py`) -- not a component to build.
It's included here so this document stays the single source of truth for
what fields exist and what they mean, but "implementing periodic sampling"
is not a remaining task for rpmeta or Copr backend.

### Approach: Periodic Sampling (Time-Series)

A background thread (`Sampler` class) reads cgroup metrics every N seconds
(default interval documented in `docs/Plugin-SystemMonitor.md`, mock repo)
throughout the build, tagging each sample with whatever build phase is
currently active. This produces a time-series that is far more informative
than start/end deltas.

**Why periodic sampling is better than start/end snapshots:**

1. **Distinguishes spikes from sustained usage.** Two packages may both show
   a similar peak memory value, but one sustains it for hours (needs a big
   builder) while the other spikes briefly during linking (can survive on a
   smaller builder). Only the time-series reveals this -- even though the
   specific `sustained_high_rss_pct` metric originally proposed for this was
   later dropped, see below.

2. **Identifies bottleneck transitions.** Builds are not homogeneous -- they
   go through network-heavy (dep install), CPU-heavy (compile), memory-heavy
   (linking), and I/O-heavy (packaging) phases. Per-phase stats show which
   resource is the bottleneck in each one (see `bottleneck_hint` in
   03-ml-models.md / 05-flavor-optimizer.md).

3. **Richer features for ML.** From a time-series we can compute summary
   statistics that are far more predictive than just peak values.

4. **Negligible overhead.** Reading a handful of cgroup files every few
   seconds costs a fraction of a millisecond per sample. Effectively zero
   impact on build performance.

### Where It Runs

The sampler runs inside mock itself, as a plugin (`requires_api_version =
"1.1"`, `run_in_bootstrap = True`), sampling the cgroup that mock's
systemd-nspawn invocation runs in (with a `--slice` argument to avoid
counting unrelated nspawn containers on the same host). It starts and stops
around the whole build via mock's plugin hook system -- Copr backend doesn't
need to wrap `MockBuilder.run()` itself.

### What's Sampled (Per Tick)

| Metric                                        | Source file        | Notes |
|------------------------------------------------|----------------------|-------|
| RSS / swap (current)                           | `memory.current`, `memory.swap.current` | Single integers |
| Anon / file split (gauge)                      | `memory.stat` (`anon`, `file`) | Current values, not deltas |
| CPU cumulative                                 | `cpu.stat` (`user_usec`, `system_usec`) | Delta from previous sample |
| CPU throttling                                 | `cpu.stat` (`nr_periods`, `nr_throttled`, `throttled_usec`) | Delta from previous sample |
| I/O cumulative                                 | `io.stat` (`rbytes`, `wbytes`, `rios`, `wios`) | Delta from previous sample |
| CPU / memory / I/O pressure                    | `*.pressure` (`some ... total=`) | Cumulative microseconds |
| Memory limit breaches                          | `memory.events` (`oom`, `oom_kill`, `max`, `high`, `low`) | Delta from previous sample |
| Thrashing / major faults                       | `memory.stat` (`pgmajfault`, `workingset_refault_anon`, `workingset_refault_file`) | Delta from previous sample |
| Process count                                  | `pids.current`      | Single integer |

Plus, once at the end of the whole build (not per phase -- resetting these
between phases isn't possible on kernels older than 6.12):
- `memory.peak`, `memory.swap.peak` -- kernel-tracked exact high-water marks,
  more precise than the sampled maximum, written only to `overall_stats.json`
  as `kernel_peak_rss_gb`/`kernel_peak_swap_gb` (with a `kernel_peak_available`
  flag for when the files aren't readable).

### Output Files

Written under `<results_dir>/system_monitor/`:

#### `{phase_name}_stats.json` and `overall_stats.json` -- Summary statistics (primary)

One file per phase plus one aggregate. Used by the `/report` endpoint and
the fetcher for model training:

```json
{
    "phase": "build",
    "duration_seconds": 8424.0,
    "peak_rss_gb": 28.4,
    "swap_peak_gb": 0.0,
    "mean_anon_gb": 12.3,
    "peak_anon_gb": 27.8,
    "peak_file_gb": 0.6,
    "cpu_user_seconds": 11200,
    "cpu_system_seconds": 340,
    "cpu_pct_max": 3.9,
    "cpu_count": 4,
    "cpu_nr_periods": 8420,
    "cpu_nr_throttled": 0,
    "cpu_throttled_seconds": 0.0,
    "io_read_gb": 5.2,
    "io_write_gb": 12.8,
    "io_read_iops_total": 41200,
    "io_write_iops_total": 98300,
    "io_accounting_available_all_the_time": true,
    "psi_cpu_total_usec": 820000,
    "psi_mem_total_usec": 15000,
    "psi_io_total_usec": 4200000,
    "oom_count": 0,
    "oom_kill_count": 0,
    "mem_events_max_count": 0,
    "mem_events_high_count": 0,
    "mem_events_low_count": 0,
    "pgmajfault_total": 0,
    "workingset_refault_anon_total": 0,
    "workingset_refault_file_total": 0,
    "sample_count": 1684,
    "sample_interval_seconds": 5
}
```

`overall_stats.json` additionally has `kernel_peak_available`,
`kernel_peak_rss_gb`, `kernel_peak_swap_gb` (see above).

Two fields from earlier versions of this doc, and of mock's own plugin, were
removed rather than renamed -- see 02-history-db.md and 03-ml-models.md for
the full reasoning:
- `p95_rss_gb` -- an arbitrary percentile choice computed *within* a single
  build's own samples, easily confused with History DB's cross-build p95.
- `sustained_high_rss_pct` -- a self-referential threshold (80% of that same
  build's own peak) that doesn't compare meaningfully across builds.

`peak_anon_gb`/`peak_file_gb` are the anon/file breakdown taken from the
same sample where `peak_rss_gb` occurred -- preferred over `peak_rss_gb`
itself for flavor sizing, since `memory.current` also counts reclaimable
page cache that inflates with available RAM rather than genuine need.
`mean_anon_gb` (mean of sampled `anon_bytes`, not raw `memory.current`) is
the same anon-vs-file preference applied to the mean instead of the peak --
an earlier version of this doc/plugin computed `mean_rss_gb` here instead,
which doesn't match what 02-history-db.md's `mean_anon_gb` column expects.

#### `samples.jsonl` -- Raw time-series (secondary)

One JSON object per line, one line per sample, covering the whole build
(not split by phase -- `phase` is a field on each sample). Used for detailed
post-hoc analysis, debugging slow builds, and computing additional ML
features beyond the three current training targets.

### Reporting to rpmeta

After the build completes, Copr backend reads `overall_stats.json` (plus
`builder_cores`/`builder_flavor` from its own configuration) and sends it to
rpmeta via `POST /report` (see 02-history-db.md). Only the summary is sent --
the raw time-series stays in the results directory for offline analysis and
fetcher access.

### Overhead Considerations

- Reading a handful of cgroup files per sample: a fraction of a millisecond
  (kernel-maintained counters, no application-level instrumentation)
- `overall_stats.json` / `{phase}_stats.json` size: a few KB total
- `samples.jsonl` size: grows with build length and sample count, still
  negligible compared to build logs and RPM artifacts

Total overhead: effectively zero.
