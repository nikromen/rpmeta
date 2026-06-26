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

Resource profiles (peak RSS, I/O volume, CPU utilization ratio) do NOT have
these problems because they are properties of the package, not the hardware.

## Schema

```sql
CREATE TABLE build_resource_history (
    id              SERIAL PRIMARY KEY,
    package_name    TEXT NOT NULL,
    chroot          TEXT NOT NULL,
    build_id        INTEGER,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Resource metrics (hardware-independent)
    peak_rss_gb         REAL NOT NULL,
    cpu_user_seconds    REAL NOT NULL,
    cpu_system_seconds  REAL NOT NULL,
    io_read_gb          REAL NOT NULL,
    io_write_gb         REAL NOT NULL,
    net_download_gb     REAL NOT NULL,

    -- Context (for analysis, not prediction input)
    wall_clock_seconds  INTEGER NOT NULL,
    builder_cores       INTEGER NOT NULL,
    cpu_utilization_ratio REAL NOT NULL,  -- derived: cpu_total / (wall * cores)
    builder_flavor      TEXT,  -- which flavor was used

    -- Indexing
    CONSTRAINT idx_pkg_chroot UNIQUE (package_name, chroot, build_id)
);

CREATE INDEX idx_history_lookup
    ON build_resource_history (package_name, chroot, recorded_at DESC);
```

Keep the **last N records** per `(package_name, chroot)`. N=20 is reasonable --
enough for stable percentiles, small enough to not bloat the DB.

Old records (beyond N) can be archived or deleted by a periodic cleanup job.

## Lookup Logic

```python
def get_resource_profile(
    session: Session,
    package_name: str,
    chroot: str,
    n: int = 20,
) -> Optional[ResourceProfile]:
    """
    Compute p95 resource profile from the last N builds.
    Returns None if no history exists.
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

    return ResourceProfile(
        peak_rss_gb=percentile([r.peak_rss_gb for r in rows], 95),
        cpu_utilization_ratio=percentile([r.cpu_utilization_ratio for r in rows], 95),
        io_total_gb=percentile([r.io_read_gb + r.io_write_gb for r in rows], 95),
        net_download_gb=percentile([r.net_download_gb for r in rows], 95),
        build_count=len(rows),
        confidence=min(1.0, len(rows) / 5),  # full confidence after 5 builds
    )
```

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

`confidence = min(1.0, build_count / 5)` -- ramps from 0.2 (one build) to 1.0
(five or more builds). Used by the cost optimizer to decide safety margins:

- Low confidence → higher safety margin (1.3x) → more conservative flavor
- High confidence → lower safety margin (1.15x) → more cost-efficient

Even a single build gives some confidence (0.2) because one real measurement
is better than an ML prediction for a known package.

## Report Endpoint (`POST /report`)

The Copr backend calls this after each build:

```json
POST /v1/report
{
    "package_name": "rocblas",
    "chroot": "fedora-42-x86_64",
    "build_id": 12345678,
    "peak_rss_gb": 31.2,
    "cpu_user_seconds": 11200,
    "cpu_system_seconds": 340,
    "io_read_gb": 5.2,
    "io_write_gb": 12.8,
    "net_download_gb": 0.1,
    "wall_clock_seconds": 14400,
    "builder_cores": 4,
    "builder_flavor": "mem-heavy"
}
```

Protected by HMAC-SHA256 authentication (reuse `auth.py` from the
`receive-data` branch). See the existing implementation for details on
headers (`X-RpmEta-Source`, `X-RpmEta-Signature-256`, `X-RpmEta-Timestamp`).

## Data Retention

- Keep last 20 records per `(package_name, chroot)`.
- Periodic cleanup job (daily cron or DB trigger) deletes older records.
- Estimated DB size: ~100K unique (pkg, chroot) × 20 records × ~200 bytes
  = ~400 MB. Trivial for PostgreSQL.
- Optionally archive old records to a separate table for trend analysis
  and model retraining.
