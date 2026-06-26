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

Mock/rpmbuild executes builds in well-defined phases. Capturing per-phase
timing and resource snapshots gives much richer data:

| Phase        | rpmbuild section | Resource characteristics                    |
|--------------|------------------|---------------------------------------------|
| dep_install  | (mock setup)     | Network-heavy (downloading packages)        |
| %prep        | `%prep`          | I/O (unpacking sources), usually fast       |
| %build       | `%build`         | CPU-heavy (compilation), often RAM-heavy    |
| %check       | `%check`         | CPU + RAM (running tests), sometimes slow   |
| %install     | `%install`       | I/O (copying files), usually fast           |
| packaging    | (rpmbuild)       | I/O + CPU (compression of RPMs)             |

**How to capture phases:** rpmbuild outputs phase markers to build.log:
```
Executing(%prep): ...
Executing(%build): ...
Executing(%check): ...
Executing(%install): ...
```

Parse these timestamps from the build log post-build, correlate with cgroup
metric snapshots taken during the build (periodic sampling or per-phase
start/end readings).

**Phase-level data is a stretch goal.** Start with aggregate (whole-build)
metrics. Add per-phase breakdown later when the infrastructure is mature.

## Implementation

### Where to Collect

The metrics should be collected in **copr-rpmbuild** or
**copr-backend** (the `BuildBackgroundWorker`), as close to the mock
invocation as possible.

```python
# Pseudocode for the collection wrapper

class CgroupMetrics:
    def __init__(self, cgroup_path: str):
        self.cgroup_path = Path(cgroup_path)

    def read_cpu_stat(self) -> dict:
        content = (self.cgroup_path / "cpu.stat").read_text()
        stats = {}
        for line in content.splitlines():
            key, value = line.split()
            stats[key] = int(value)
        return stats

    def read_memory_peak(self) -> int:
        return int((self.cgroup_path / "memory.peak").read_text().strip())

    def read_io_stat(self) -> dict:
        # io.stat format: "MAJ:MIN rbytes=X wbytes=Y rios=Z wios=W"
        ...

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu=self.read_cpu_stat(),
            memory_peak=self.read_memory_peak(),
            memory_swap=self.read_memory_swap(),
            io=self.read_io_stat(),
        )


# Before build:
pre_snapshot = metrics.snapshot()
pre_net = read_proc_net_dev()

# ... mock runs the build ...

# After build:
post_snapshot = metrics.snapshot()
post_net = read_proc_net_dev()

# Compute deltas:
resource_stats = ResourceStats(
    peak_rss_gb=post_snapshot.memory_peak / GiB,
    cpu_user_seconds=(post_snapshot.cpu["user_usec"] - pre_snapshot.cpu["user_usec"]) / 1e6,
    cpu_system_seconds=(post_snapshot.cpu["system_usec"] - pre_snapshot.cpu["system_usec"]) / 1e6,
    io_read_gb=(post_io.rbytes - pre_io.rbytes) / GiB,
    io_write_gb=(post_io.wbytes - pre_io.wbytes) / GiB,
    net_download_gb=(post_net.rx_bytes - pre_net.rx_bytes) / GiB,
    wall_clock_seconds=ended_on - started_on,
    cores=builder_cores,
)

# Write to results alongside results.json:
write_json(results_dir / "resource_stats.json", resource_stats)
```

### Output Format (`resource_stats.json`)

```json
{
  "peak_rss_gb": 28.4,
  "cpu_user_seconds": 11200,
  "cpu_system_seconds": 340,
  "io_read_gb": 5.2,
  "io_write_gb": 12.8,
  "net_download_gb": 1.3,
  "wall_clock_seconds": 14400,
  "builder_cores": 4,
  "cpu_utilization_ratio": 0.81,
  "memory_pressure_avg": 0.02,
  "io_pressure_avg": 0.15
}
```

### Reporting to rpmeta

After the build completes, Copr backend sends the resource stats to rpmeta
via a new `POST /report` endpoint (see 02-history-db.md). This is the same
endpoint discussed in the EMA plan, but with richer data.

### cgroup Path Discovery

Mock runs rpmbuild in a chroot. On a systemd-based system, the build process
is in a cgroup like:

```
/sys/fs/cgroup/user.slice/user-<uid>.slice/session-<sid>.scope/
```

Or if running under a systemd service (as in Copr):
```
/sys/fs/cgroup/system.slice/copr-backend-build-<id>.service/
```

The exact path can be discovered from `/proc/<pid>/cgroup` of the mock process.

### Overhead Considerations

- Reading cgroup files: **negligible** (<1 ms per read, kernel-maintained)
- `/proc/net/dev`: **negligible** (single file read)
- Periodic sampling (if desired): 1 read/10s = 0.0001% overhead
- Writing `resource_stats.json`: **negligible** (<1 KB file)

Total overhead: effectively zero. These are kernel-maintained counters, not
application-level instrumentation.
