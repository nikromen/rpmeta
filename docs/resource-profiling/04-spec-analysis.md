# Spec Analysis -- Layer 3 (Cold-Start Fallback)

## When This Is Used

When both Layer 1 (history) and Layer 2 (ML model) have no data for a package.
This happens for completely new packages not yet in the training data. The spec
file is the only information available before the first build.

## Approach

Parse the RPM spec file (or SRPM metadata) to extract structural hints about
resource requirements. This is a **heuristic**, not ML -- a set of rules
based on domain knowledge about RPM build patterns.

## Language/Build System Detection

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

```python
LANGUAGE_PROFILES = {
    "rust": ResourceEstimate(
        peak_rss_gb=lambda srpm_mb: max(4, srpm_mb * 8),
        cpu_ratio=0.90,
        io_gb=lambda srpm_mb: srpm_mb * 3,
        net_gb=2.0,  # crate downloads
        confidence=0.3,
        notes="CPU-heavy compilation, heavy network for crate downloads",
    ),
    "go": ResourceEstimate(
        peak_rss_gb=lambda srpm_mb: max(2, srpm_mb * 4),
        cpu_ratio=0.85,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        net_gb=1.0,  # module downloads
        confidence=0.3,
        notes="CPU-heavy, moderate network for module downloads",
    ),
    "c_cpp": ResourceEstimate(
        peak_rss_gb=lambda srpm_mb: max(2, srpm_mb * 4),
        cpu_ratio=0.85,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        net_gb=0.1,
        confidence=0.25,
        notes="CPU-heavy compilation, linking can be memory-intensive",
    ),
    "python": ResourceEstimate(
        peak_rss_gb=lambda _: 2,
        cpu_ratio=0.3,
        io_gb=lambda srpm_mb: srpm_mb * 1.5,
        net_gb=0.5,
        confidence=0.4,  # Python builds are more predictable
        notes="Mostly I/O, low CPU unless C extensions",
    ),
    "java": ResourceEstimate(
        peak_rss_gb=lambda _: 4,
        cpu_ratio=0.6,
        io_gb=lambda srpm_mb: srpm_mb * 2,
        net_gb=0.5,
        confidence=0.3,
        notes="JVM overhead, moderate CPU",
    ),
    "nodejs": ResourceEstimate(
        peak_rss_gb=lambda _: 4,
        cpu_ratio=0.4,
        io_gb=lambda _: 5,  # node_modules is huge
        net_gb=3.0,  # npm install downloads a lot
        confidence=0.25,
        notes="Network and I/O heavy from npm, moderate CPU",
    ),
}

DEFAULT_PROFILE = ResourceEstimate(
    peak_rss_gb=lambda _: 4,
    cpu_ratio=0.5,
    io_gb=lambda srpm_mb: srpm_mb * 2,
    net_gb=0.5,
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
    spec_path: Optional[Path] = None,
    build_requires: Optional[list[str]] = None,
    srpm_size_mb: float = 1.0,
    has_check: bool = False,
) -> ResourceProfile:
    """
    Estimate resource requirements from spec file metadata.
    Returns a ResourceProfile with low confidence (heuristic).
    """
    if build_requires is None and spec_path:
        build_requires = parse_build_requires(spec_path)

    language = detect_language(build_requires or [])
    build_system = detect_build_system(build_requires or [])

    profile = LANGUAGE_PROFILES.get(language, DEFAULT_PROFILE)

    peak_rss = profile.peak_rss_gb(srpm_size_mb)
    io_total = profile.io_gb(srpm_size_mb)
    net_total = profile.net_gb

    if has_check:
        peak_rss *= 1.3
        io_total *= 1.2

    return ResourceProfile(
        peak_rss_gb=peak_rss,
        cpu_utilization_ratio=profile.cpu_ratio,
        io_total_gb=io_total,
        net_download_gb=net_total,
        confidence=profile.confidence,
        source="spec_heuristic",
    )
```

## Accuracy Expectations

This layer is intentionally imprecise. It's a safety net, not a precision tool:

- **Within 2x of actual:** Good outcome. "Estimate 8 GB, actual 5 GB" → fine,
  slightly over-provisioned.
- **Within 5x of actual:** Acceptable. "Estimate 4 GB, actual 1 GB" → wasted
  resources but build succeeds.
- **Off by 10x+:** Bad but survivable. Cost optimizer adds safety margins for
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
