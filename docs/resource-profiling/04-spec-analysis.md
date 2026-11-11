# Spec Analysis -- Layer 3 (Cold-Start Fallback)

## When This Is Used

When both Layer 1 (history) and Layer 2 (ML model) have no data for a package.
This happens for completely new packages not yet in the training data. The spec
file is the only information available before the first build.

## Approach: Two Strategies, Tried in Order

1. **Nearest-neighbor lookup** against packages that already have a real
   History DB profile, matched by BuildRequires similarity -- "this new
   package looks like these 5 known ones, whose actual measured resource
   profile was X." Tried first, because a real measured profile from a
   similar package is a much stronger signal than a language-level average.
2. **Static language/build-system table** ("is it Rust, is it Python") as a
   fallback -- used only when no sufficiently similar package exists yet
   (e.g. during early bootstrapping, or a genuinely novel BuildRequires
   combination).

Neither strategy is ML -- both are **heuristics**: the first is a similarity
search over real data, the second is a set of fixed rules based on domain
knowledge about RPM build patterns. The point of trying nearest-neighbor
first is specifically to fix the weakness of the language table alone: "it's
Python" doesn't distinguish a 5-second pure-Python script from a 60-minute
NumPy-style build with heavy C extensions, but "it shares 8 of 10
BuildRequires with `scipy`, which historically needed 12 GB RAM and 25
minutes of CPU work" does.

## Nearest-Neighbor Lookup (Primary Strategy)

### Similarity Metric

Deliberately simple and mechanical -- no embeddings, no NLP model, no extra
infrastructure -- consistent with the rest of this pipeline preferring
mechanical corrections over black-box ones wherever a mechanical one is
good enough (see 02-history-db.md's "Hardware Dependency" section for the
same principle applied elsewhere): **Jaccard similarity over BuildRequires
sets.**

```python
def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
```

### Finding Candidates

```python
SIMILARITY_THRESHOLD = 0.3  # below this, don't trust the match at all

def find_similar_packages(
    session: Session,
    build_requires: list[str],
    chroot: str,
    k: int = 5,
) -> list[tuple[BuildResourceHistory, float]]:
    """
    Search build_resource_history for packages with a similar
    BuildRequires set that have a real, valid resource profile.
    Returns up to k (row, similarity) pairs above SIMILARITY_THRESHOLD,
    most similar first.
    """
    target = set(build_requires)

    # Same quality filter as get_resource_profile() -- an OOM-killed row's
    # numbers are a ceiling it died against, not a resource need. Restrict
    # to the same chroot so cross-arch/cross-OS differences don't leak in.
    candidates = (
        session.query(BuildResourceHistory)
        .filter(
            BuildResourceHistory.chroot == chroot,
            BuildResourceHistory.oom_kill_count == 0,
            BuildResourceHistory.build_requires.isnot(None),
        )
        .all()
    )

    scored = [
        (row, _jaccard_similarity(target, set(row.build_requires)))
        for row in candidates
    ]
    scored = [(row, sim) for row, sim in scored if sim >= SIMILARITY_THRESHOLD]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:k]
```

### Turning Neighbors into a `ResourceProfile`

```python
def estimate_from_similar_packages(
    neighbors: list[tuple[BuildResourceHistory, float]],
) -> Optional[ResourceProfile]:
    """
    Similarity-weighted estimate from the neighbors found above.
    Returns None if there are no neighbors -- caller falls back to the
    static language table.
    """
    if not neighbors:
        return None

    best_similarity = neighbors[0][1]
    weights = [sim for _, sim in neighbors]
    total_weight = sum(weights)

    def weighted(field: str) -> float:
        return sum(getattr(row, field) * w for (row, _), w in zip(neighbors, weights)) / total_weight

    # Capped below History DB's own single-real-build confidence (0.2, see
    # 02-history-db.md) -- this is a similar package, not the same one, so
    # even a near-perfect BuildRequires match shouldn't outrank one real
    # measurement of the exact package being asked about.
    confidence = min(0.18, best_similarity * 0.2)

    return ResourceProfile(
        peak_anon_gb=weighted("peak_anon_gb"),
        cpu_core_seconds=weighted("cpu_user_seconds") + weighted("cpu_system_seconds"),
        wall_clock_seconds=None,  # mixing neighbors' wall clocks isn't meaningful
        io_total_gb=weighted("io_read_gb") + weighted("io_write_gb"),
        ram_confidence=confidence,
        cpu_confidence=confidence,
        memory_tightness_flag=False,
        bottleneck_hint="none",  # no single build's phase timeline applies here
        build_count=0,
        source="spec_heuristic",
    )
```

`confidence` tops out at 0.18 even for a perfect (similarity=1.0) match --
deliberately still below Layer 1's minimum of 0.2 (one real build of the
*exact* package). A great BuildRequires match is a good guess, not a
measurement.

## Language/Build System Detection (Fallback Strategy)

The strongest signal comes from BuildRequires, which reveals the primary
language and build system:

```python
LANGUAGE_INDICATORS = {
    "rust": ["rust-packaging", "cargo", "rust-srpm-macros"],
    "go": ["go-rpm-macros", "golang", "go-compilers-golang-compiler"],
    "c_cpp": ["gcc", "gcc-c++", "cmake", "meson", "autoconf", "automake"],
    "python": ["python3-devel", "python3dist(setuptools)", "pyproject-rpm-macros"],
    "java": ["maven-local", "java-devel", "ant"],
    "nodejs": ["nodejs-packaging", "npm", "nodejs-devel"],
    "perl": ["perl-generators", "perl-devel"],
    "ruby": ["ruby-devel", "rubygems-devel"],
    "haskell": ["ghc-rpm-macros", "ghc-Cabal-devel"],
}

BUILD_SYSTEM_INDICATORS = {
    "cmake": ["cmake"],
    "meson": ["meson"],
    "autotools": ["autoconf", "automake", "libtool"],
    "cargo": ["cargo", "rust-packaging"],
    "setuptools": ["python3dist(setuptools)"],
    "go": ["go-rpm-macros"],
    "maven": ["maven-local"],
}
```

## Resource Estimation Rules

Each language/build-system has typical resource characteristics:

`cpu_core_seconds` is estimated directly as an absolute value (core-seconds
of CPU work), not as a ratio -- consistent with the rest of the pipeline
never storing a precomputed utilization ratio (see 01-data-collection.md and
02-history-db.md for why). There's no `net_gb`/network estimate either:
network isn't collected anywhere in this pipeline (Copr builders have no
network access during `%build`/`%check`, see 01-data-collection.md).

```python
LANGUAGE_PROFILES = {
    "rust": ResourceEstimate(
        peak_anon_gb=lambda srpm_mb: max(4, srpm_mb * 8),
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 40,  # heavy compilation
        io_gb=lambda srpm_mb: srpm_mb * 3,
        confidence=0.3,
        notes="CPU-heavy compilation, monomorphization-driven codegen",
    ),
    "go": ResourceEstimate(
        peak_anon_gb=lambda srpm_mb: max(2, srpm_mb * 4),
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 25,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        confidence=0.3,
        notes="CPU-heavy but fast compiler, less RAM than Rust/C++",
    ),
    "c_cpp": ResourceEstimate(
        peak_anon_gb=lambda srpm_mb: max(2, srpm_mb * 4),
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 25,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        confidence=0.25,
        notes="CPU-heavy compilation, linking can be memory-intensive",
    ),
    "python": ResourceEstimate(
        peak_anon_gb=lambda _: 2,
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 3,
        io_gb=lambda srpm_mb: srpm_mb * 1.5,
        confidence=0.4,  # Python builds are more predictable
        notes="Mostly I/O, low CPU unless C extensions",
    ),
    "java": ResourceEstimate(
        peak_anon_gb=lambda _: 4,
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 12,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        confidence=0.3,
        notes="JVM overhead, moderate CPU",
    ),
    "nodejs": ResourceEstimate(
        peak_anon_gb=lambda _: 4,
        cpu_core_seconds=lambda srpm_mb: srpm_mb * 8,
        io_gb=lambda _: 5,  # node_modules is huge
        confidence=0.25,
        notes="I/O heavy from npm install, moderate CPU",
    ),
}

DEFAULT_PROFILE = ResourceEstimate(
    peak_anon_gb=lambda _: 4,
    cpu_core_seconds=lambda srpm_mb: srpm_mb * 15,
    io_gb=lambda srpm_mb: srpm_mb * 2,
    confidence=0.1,
    notes="Unknown language, conservative default",
)
```

## Additional Signals from Spec

Beyond language detection:

### SRPM Size

Larger source → more to compile → more CPU and I/O. The `srpm_size_mb` is a
useful scaling factor for resource estimates. A 500 MB SRPM is going to need
more resources than a 50 KB one regardless of language.

### Number of BuildRequires

More dependencies → longer dep installation phase (network + I/O). Also
correlates with project complexity.

### %check Section

If the spec has `%check` (tests are run), add 20-50% to CPU and time estimates.
Test suites can be memory-heavy (especially integration tests).

### Parallelism Hints

If `%build` uses `%make_build` or `%{?_smp_mflags}`, the build can parallelize.
More cores will help. If it uses plain `make` without `-j`, it's sequential
and more cores won't help.

### ExclusiveArch

If the spec limits to specific architectures, this is useful metadata for
understanding the build (e.g., x86_64-only often means low-level code).

## Implementation

```python
def estimate_from_spec(
    session: Session,
    chroot: str,
    spec_path: Optional[Path] = None,
    build_requires: Optional[list[str]] = None,
    srpm_size_mb: float = 1.0,
    has_check: bool = False,
) -> ResourceProfile:
    """
    Estimate resource requirements from spec file metadata.
    Tries nearest-neighbor lookup against real History DB data first;
    falls back to the static language table only when no similar package
    exists yet. Always returns a ResourceProfile with low confidence
    (heuristic either way).
    """
    if build_requires is None and spec_path:
        build_requires = parse_build_requires(spec_path)

    if build_requires:
        neighbors = find_similar_packages(session, build_requires, chroot)
        nn_profile = estimate_from_similar_packages(neighbors)
        if nn_profile is not None:
            return nn_profile

    # Fallback: no similar package found (or no build_requires at all --
    # e.g. a caller that hasn't been updated to send it yet)
    language = detect_language(build_requires or [])
    build_system = detect_build_system(build_requires or [])

    profile = LANGUAGE_PROFILES.get(language, DEFAULT_PROFILE)

    peak_anon = profile.peak_anon_gb(srpm_size_mb)
    cpu_core_seconds = profile.cpu_core_seconds(srpm_size_mb)
    io_total = profile.io_gb(srpm_size_mb)

    if has_check:
        peak_anon *= 1.3
        cpu_core_seconds *= 1.3
        io_total *= 1.2

    return ResourceProfile(
        peak_anon_gb=peak_anon,
        cpu_core_seconds=cpu_core_seconds,
        wall_clock_seconds=None,  # no real measurement exists yet
        io_total_gb=io_total,
        ram_confidence=profile.confidence,
        cpu_confidence=profile.confidence,
        memory_tightness_flag=False,  # nothing to detect this from
        bottleneck_hint="none",  # no real build timeline to classify
        build_count=0,
        source="spec_heuristic",
    )
```

`wall_clock_seconds=None` means `_avg_concurrent_cores()` in
05-flavor-optimizer.md can't be computed for a spec-heuristic profile, so
`_compute_efficiency_index()` falls back to its neutral `cpu_fit=0.5` and
`_estimate_duration_hours()` returns `None` -- for a completely unknown
package, that's an honest reflection of "we have no idea how well this
parallelizes," not a bug to work around.

## Accuracy Expectations

This layer is intentionally imprecise. It's a safety net, not a precision
tool -- true for both strategies, but not equally imprecise:

- **Nearest-neighbor match found:** confidence up to 0.18 (see above),
  scaling with `best_similarity`. Expected to be noticeably closer to
  reality than the language table, since it borrows a real measured profile
  from an actually-similar package instead of a language-wide average.
- **Language table fallback (no match found):** confidence 0.1-0.4, as
  before. This is the weaker case the rest of this section describes.

In both cases:

- **Within 2x of actual:** Good outcome. "Estimate 8 GB, actual 5 GB" → fine,
  slightly over-provisioned.
- **Within 5x of actual:** Acceptable. "Estimate 4 GB, actual 1 GB" → wasted
  resources but build succeeds.
- **Off by 10x+:** Bad but survivable. Flavor optimizer adds safety margins for
  low-confidence estimates. First build collects real data for history DB,
  so subsequent builds are precise.

The key property: **it should almost never UNDER-estimate** enough to cause
OOM or timeout. Conservative estimates are acceptable; failed builds are not.

## Data Sources for Spec Analysis

The spec file metadata can come from:

1. **SRPM inspection:** `rpm -qp --requires *.src.rpm` gives BuildRequires
2. **dist-git:** Spec file is in the source repository
3. **Copr API:** Build submission includes source reference
4. **rpmeta API extension:** Accept optional `build_requires` and `srpm_size_mb`
   fields in the prediction request

Option 4 is cleanest -- the Copr backend already has this information when
it calls rpmeta, it just doesn't send it today. Adding optional fields to
`InputRecord` is backward-compatible.

**`build_requires` needs to flow both ways to make the nearest-neighbor
strategy work over time:** into `/predict` requests (so a brand-new package
can be matched against neighbors), and into `/report` payloads for *every*
build regardless of which layer resolved its prediction (so the corpus that
future nearest-neighbor lookups search against keeps growing) -- see the
`build_requires` column and `/report` payload in 02-history-db.md. A package
resolved entirely by Layer 1 or Layer 2 today still becomes a useful
neighbor for some unrelated brand-new package tomorrow, as long as its
BuildRequires got reported.
