# Data Collection

## Current State

Copr collects **zero runtime resource metrics** during builds. The only
per-build data is:

- `started_on` / `ended_on` timestamps (wall-clock)
- `results.json` with NEVRA (package metadata, no resource info)
- `hw_info.log` with static builder specs (lscpu + free output)

There is no CPU usage, memory tracking, I/O accounting, or network monitoring.

## What to Collect

### CPU Metrics (from cgroups v2 `cpu.stat`)

| Metric            | cgroup file      | Field             | What it tells you                   |
|-------------------|------------------|-------------------|-------------------------------------|
| User CPU time     | `cpu.stat`       | `user_usec`       | Time spent in user-space code       |
| System CPU time   | `cpu.stat`       | `system_usec`     | Time spent in kernel (I/O, syscalls)|
| Throttled periods | `cpu.stat`       | `nr_throttled`    | How often CPU limit was hit         |
| Throttled time    | `cpu.stat`       | `throttled_usec`  | Total time throttled                |
| CPU pressure      | `cpu.pressure`   | `some avg10/60/300` | % time tasks waited for CPU       |

**Derived metric:** `cpu_utilization_ratio = (user_usec + system_usec) / (wall_clock_usec * cores)`
This is hardware-independent and tells you if the build is CPU-bound (>0.8)
or waiting on something else (<0.3).

### Memory Metrics (from cgroups v2 `memory.*`)

| Metric            | cgroup file         | Field          | What it tells you                  |
|-------------------|---------------------|----------------|------------------------------------|
| Peak RSS          | `memory.peak`       | (single value) | Maximum memory used                |
| Current usage     | `memory.current`    | (single value) | Current memory (for time-series)   |
| Swap usage        | `memory.swap.current` | (single value) | How much swap is used            |
| Page faults major | `memory.stat`       | `pgmajfault`   | Disk-backed page faults (I/O hit) |
| Memory pressure   | `memory.pressure`   | `some avg10`   | % time tasks waited for memory    |

**Key metric:** `peak_rss_gb = memory.peak / (1024^3)`. This is the most
important metric for flavor selection -- if peak RSS > builder RAM, the build
swaps and becomes catastrophically slow.

### I/O Metrics (from cgroups v2 `io.stat`)

| Metric        | cgroup file    | Field    | What it tells you              |
|---------------|----------------|----------|--------------------------------|
| Bytes read    | `io.stat`      | `rbytes` | Total data read from disk      |
| Bytes written | `io.stat`      | `wbytes` | Total data written to disk     |
| Read ops      | `io.stat`      | `rios`   | Number of read operations      |
| Write ops     | `io.stat`      | `wios`   | Number of write operations     |
| I/O pressure  | `io.pressure`  | `some avg10` | % time tasks waited for I/O |

### Network Metrics

Network is NOT available via cgroups. Options:

1. **Parse dnf/yum logs** -- during dep installation, dnf logs download sizes
   and speeds. Parse these from mock's build log.
2. **iptables/nftables accounting** -- count bytes per mock chroot network
   namespace. Requires network namespace isolation (mock already supports this).
3. **`/proc/net/dev` delta** -- read network counters before and after build.
   Simple but coarse (includes all traffic, not just build-related).
4. **Proxy/squid logs** -- if builds go through a proxy, log download volumes
   there. Already exists in some Copr deployments.

**Recommendation:** Start with option 3 (`/proc/net/dev` delta). Simple, no
dependencies, gives total bytes downloaded. Sufficient to distinguish
network-heavy builds (Rust, Go, Node) from network-light builds (C/C++).

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

**Per-phase timing is NOT feasible from build.log.** While rpmbuild outputs
phase markers like `Executing(%build): /bin/sh -e /var/tmp/rpm-tmp.XXX`,
these lines have **no absolute timestamps**. The log is raw rpmbuild stdout
with no datetime prefixes. mock's own messages (`Start:`, `Finish:`,
`INFO: Done(...)`) provide only aggregate timing for the whole build, not
per-phase breakdowns.

**Instead, rely on periodic sampling** (see Implementation below). By
correlating time-series resource snapshots with the build timeline, we
can indirectly observe phase transitions -- e.g. a sudden CPU spike after
a network-heavy period marks the transition from dep_install to %build.
This is far more useful than log parsing and works without any changes to
rpmbuild or mock.

**Per-phase hooks are a future possibility** if mock adds plugin hooks for
individual rpmbuild sections (currently it only has `prebuild`/`postbuild`
for the whole invocation).

## Implementation

### Approach: Periodic Sampling (Time-Series)

Instead of reading cgroup metrics once before and once after the build,
use **periodic sampling** -- a background thread reads all metrics every
N seconds throughout the build. This produces a time-series that is far more
informative than start/end deltas.

**Why periodic sampling is better than start/end snapshots:**

1. **Distinguishes spikes from sustained usage.** Two packages may both show
   `memory.peak = 30 GB`, but one uses 30 GB for 3 hours (needs a big builder)
   while the other spikes to 30 GB for 10 seconds during linking (can survive
   on a medium builder with swap). Only the time-series reveals this.

2. **Identifies bottleneck transitions.** Builds are not homogeneous -- they go
   through network-heavy (dep install), CPU-heavy (compile), memory-heavy
   (linking), and I/O-heavy (packaging) phases. The time-series shows when each
   resource is the bottleneck without needing per-phase hooks in mock.

3. **Richer features for ML.** From a time-series we can compute summary
   statistics (peak, mean, p95, variance, sustained-high duration) that are
   far more predictive than just peak values.

4. **Negligible overhead.** Reading 5 cgroup files every 10 seconds costs
   <0.5 ms per sample. A 4-hour build = 1440 samples x ~100 bytes = ~144 KB.
   Effectively zero impact on build performance.

### Where to Collect

The sampler runs in **copr-rpmbuild**, wrapping `MockBuilder.run()`. It runs
as a daemon thread that terminates when the build finishes.

### What to Sample (Per Tick)

| Metric             | Source file              | Notes                          |
|--------------------|--------------------------|--------------------------------|
| RSS (current)      | `memory.current`         | Single integer                 |
| Swap (current)     | `memory.swap.current`    | Single integer                 |
| CPU cumulative     | `cpu.stat` `user_usec`   | Delta from previous sample     |
| CPU cumulative     | `cpu.stat` `system_usec` | Delta from previous sample     |
| I/O cumulative     | `io.stat` `rbytes`       | Delta from previous sample     |
| I/O cumulative     | `io.stat` `wbytes`       | Delta from previous sample     |
| CPU pressure        | `cpu.pressure` `some total` | Cumulative microseconds     |
| Memory pressure    | `memory.pressure` `some total` | Cumulative microseconds |
| I/O pressure       | `io.pressure` `some total` | Cumulative microseconds      |

Plus read once at the end (kernel's own high-water mark, more precise than
our sampled maximum):
- `memory.peak` -- absolute peak RSS since cgroup creation

### Sampling Implementation

```python
import threading
import time
from pathlib import Path

SAMPLE_INTERVAL = 10  # seconds

class CgroupSampler:
    """Background thread that samples cgroup metrics periodically."""

    def __init__(self, cgroup_path: str, interval: int = SAMPLE_INTERVAL):
        self.cgroup_path = Path(cgroup_path)
        self.interval = interval
        self.samples: list[dict] = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _run(self):
        while not self._stop_event.is_set():
            self.samples.append(self._take_sample())
            self._stop_event.wait(self.interval)

    def _take_sample(self) -> dict:
        return {
            "t": time.time(),
            "rss_bytes": self._read_int("memory.current"),
            "swap_bytes": self._read_int("memory.swap.current"),
            "cpu_user_usec": self._read_cpu_field("user_usec"),
            "cpu_system_usec": self._read_cpu_field("system_usec"),
            "io_rbytes": self._read_io_field("rbytes"),
            "io_wbytes": self._read_io_field("wbytes"),
            "cpu_pressure_total": self._read_pressure("cpu.pressure"),
            "mem_pressure_total": self._read_pressure("memory.pressure"),
            "io_pressure_total": self._read_pressure("io.pressure"),
        }

    def _read_int(self, filename: str) -> int:
        try:
            return int((self.cgroup_path / filename).read_text().strip())
        except (FileNotFoundError, ValueError):
            return 0

    # ... similar helpers for cpu.stat, io.stat, *.pressure parsing ...

    def compute_summary(self, wall_clock_seconds: float,
                        cores: int) -> dict:
        """Compute summary statistics from collected time-series."""
        if not self.samples:
            return {}

        rss_values = [s["rss_bytes"] for s in self.samples]
        memory_peak = self._read_int("memory.peak")  # kernel HWM
        GiB = 1024 ** 3

        cpu_total = (
            (self.samples[-1]["cpu_user_usec"] - self.samples[0]["cpu_user_usec"])
            + (self.samples[-1]["cpu_system_usec"] - self.samples[0]["cpu_system_usec"])
        )

        return {
            # Peak values (hard constraints -- must not exceed)
            "peak_rss_gb": memory_peak / GiB,
            "peak_sampled_rss_gb": max(rss_values) / GiB,

            # Sustained usage (spike vs sustained distinction)
            "mean_rss_gb": (sum(rss_values) / len(rss_values)) / GiB,
            "p95_rss_gb": sorted(rss_values)[int(len(rss_values) * 0.95)] / GiB,
            "sustained_high_rss_pct": sum(
                1 for v in rss_values if v > 0.8 * memory_peak
            ) / len(rss_values),

            # CPU totals and utilization
            "cpu_user_seconds": (self.samples[-1]["cpu_user_usec"]
                                 - self.samples[0]["cpu_user_usec"]) / 1e6,
            "cpu_system_seconds": (self.samples[-1]["cpu_system_usec"]
                                   - self.samples[0]["cpu_system_usec"]) / 1e6,
            "cpu_utilization_ratio": cpu_total / (wall_clock_seconds * 1e6 * cores),

            # I/O totals
            "io_read_gb": (self.samples[-1]["io_rbytes"]
                           - self.samples[0]["io_rbytes"]) / GiB,
            "io_write_gb": (self.samples[-1]["io_wbytes"]
                            - self.samples[0]["io_wbytes"]) / GiB,

            # Build metadata
            "wall_clock_seconds": wall_clock_seconds,
            "builder_cores": cores,
            "sample_count": len(self.samples),
            "sample_interval_seconds": self.interval,
        }
```

### Usage in copr-rpmbuild

```python
# In MockBuilder.run() or its caller:

sampler = CgroupSampler(discover_cgroup_path(mock_pid))
pre_net = read_proc_net_dev()
sampler.start()

try:
    mock.run_build(spec, sources, ...)  # blocking
finally:
    sampler.stop()
    post_net = read_proc_net_dev()

    summary = sampler.compute_summary(wall_clock_seconds, builder_cores)
    summary["net_download_gb"] = (post_net.rx_bytes - pre_net.rx_bytes) / GiB

    # Write summary stats (for /report endpoint and fetcher)
    write_json(results_dir / "resource_stats.json", summary)

    # Write raw time-series (for detailed analysis and future model improvement)
    write_jsonl(results_dir / "resource_samples.jsonl", sampler.samples)
```

### Output Files

Two files are written to the build results directory:

#### `resource_stats.json` -- Summary statistics (primary)

Used by the `/report` endpoint and the fetcher for model training:

```json
{
  "peak_rss_gb": 28.4,
  "peak_sampled_rss_gb": 27.9,
  "mean_rss_gb": 12.3,
  "p95_rss_gb": 26.1,
  "sustained_high_rss_pct": 0.35,
  "cpu_user_seconds": 11200,
  "cpu_system_seconds": 340,
  "cpu_utilization_ratio": 0.81,
  "io_read_gb": 5.2,
  "io_write_gb": 12.8,
  "net_download_gb": 1.3,
  "wall_clock_seconds": 14400,
  "builder_cores": 4,
  "sample_count": 1440,
  "sample_interval_seconds": 10
}
```

Key fields for spike vs sustained distinction:
- `peak_rss_gb = 28.4, sustained_high_rss_pct = 0.95` -> sustained, needs 32 GB
- `peak_rss_gb = 28.4, sustained_high_rss_pct = 0.02` -> spike, 16 GB + swap OK

#### `resource_samples.jsonl` -- Raw time-series (secondary)

One JSON object per line, one line per sample. Used for detailed post-hoc
analysis, debugging slow builds, and computing additional ML features:

```json
{"t":1719300000.0,"rss_bytes":2147483648,"swap_bytes":0,"cpu_user_usec":1000000,"cpu_system_usec":50000,"io_rbytes":104857600,"io_wbytes":0}
{"t":1719300010.0,"rss_bytes":4294967296,"swap_bytes":0,"cpu_user_usec":8500000,"cpu_system_usec":120000,"io_rbytes":209715200,"io_wbytes":52428800}
```

This file can be large for long builds (~144 KB for 4 hours at 10s interval)
but is still negligible compared to build logs and RPM artifacts.

### Reporting to rpmeta

After the build completes, Copr backend sends the summary stats from
`resource_stats.json` to rpmeta via a new `POST /report` endpoint
(see 02-history-db.md). Only the summary is sent -- the raw time-series
stays in the results directory for offline analysis and fetcher access.

### cgroup Path Discovery

Mock runs rpmbuild inside a chroot using systemd-nspawn. The build process
lives in a cgroup hierarchy. The exact path depends on the environment:

```
# Under a systemd service (typical Copr builder):
/sys/fs/cgroup/system.slice/copr-backend-build-<id>.service/

# Under a user session:
/sys/fs/cgroup/user.slice/user-<uid>.slice/session-<sid>.scope/
```

The path is discovered from `/proc/<pid>/cgroup` of the mock process:

```python
def discover_cgroup_path(pid: int) -> str:
    with open(f"/proc/{pid}/cgroup") as f:
        for line in f:
            # cgroups v2: single line "0::/path"
            if line.startswith("0::"):
                return "/sys/fs/cgroup" + line.strip().split("::")[1]
    raise RuntimeError(f"Cannot find cgroup for pid {pid}")
```

### Overhead Considerations

- Reading 5 cgroup files per sample: **<0.5 ms** (kernel-maintained counters)
- `/proc/net/dev` read (start + end only): **negligible**
- Sampling thread CPU usage: **<0.004%** (0.5 ms every 10 seconds)
- `resource_stats.json` size: **<1 KB**
- `resource_samples.jsonl` size: **~36 bytes/sample x 6/min = ~144 KB for 4 hours**
- In-process memory: **<1 MB** (list of sample dicts)

Total overhead: effectively zero. These are kernel-maintained counters, not
application-level instrumentation.
