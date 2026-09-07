# Release Process — sera-browser

This document describes how `sera-browser` is packaged, distributed, and
installed. The package is **public on PyPI**; the source repository is
**private on GitHub**.

## 1. Package metadata

| Field | Value |
|---|---|
| **Package name** | `sera-browser` |
| **Import name** | `sera_browser` |
| **Current version** | `3.1.0` |
| **Python** | `>=3.12` |
| **License** | Apache-2.0 |
| **Registry** | PyPI (public) |
| **Source repository** | `mir-ashiq/sera-browser` (private) |

## 2. Distribution: PyPI (public) + GitHub Release (artifacts)

The package is published to **public PyPI** via **Trusted Publishing** (OIDC,
no stored tokens). The source repository stays **private** on GitHub — only
the published wheel is publicly installable.

### PyPI Trusted Publishing configuration

| Field | Value |
|---|---|
| **PyPI project name** | `sera-browser` |
| **Owner** | `mir-ashiq` |
| **Repository** | `sera-browser` |
| **Workflow** | `release.yml` |
| **Environment** | `pypi` |

This is configured in PyPI's web UI under `Settings → Publishing` →
`Add a new publisher → GitHub`. The workflow uses `pypa/gh-action-pypi-publish`
with `id-token: write` permission — no API tokens are stored anywhere.

### Installation

```bash
# From PyPI (public, no auth needed):
pip install sera-browser

# Or upgrade:
pip install --upgrade sera-browser
```

## 3. Semantic versioning

We follow [Semantic Versioning 2.0.0](https://semver.org/):

- **MAJOR** (4.x.x): breaking changes to the public API
- **MINOR** (3.1.x): new features, backward-compatible
- **PATCH** (3.x.1): bug fixes, backward-compatible

The version is defined in:
- `pyproject.toml` → `version = "3.1.0"`
- `src/sera_browser/__init__.py` → `__version__ = "3.1.0"`

Both must match. The CI release workflow validates this alignment.

### Creating a release

```bash
# 1. Update the version in both files (must match).
# 2. Commit the version bump.
git commit -am "Bump version to 3.1.0"

# 3. Tag the commit (must match the version with a 'v' prefix).
git tag v3.1.0

# 4. Push the tag.
git push origin v3.1.0

# 5. The Release workflow runs automatically:
#    - runs tests
#    - validates version matches tag
#    - builds wheel + sdist + checksums
#    - publishes to PyPI (Trusted Publishing, no tokens)
#    - creates GitHub Release with artifacts
```

## 4. GitHub Actions workflows

### CI workflow (`.github/workflows/ci.yml`)

Runs on every push/PR to `main`:
- Tests on Python 3.12 + 3.13
- Builds wheel + sdist
- Validates package metadata
- Clean-env wheel installation test
- Uploads coverage + build artifacts

### Release workflow (`.github/workflows/release.yml`)

Runs when a `v*.*.*` tag is pushed:
1. **Tests** — runs the full test suite (fail-safe: if tests fail, no release)
2. **Build** — builds wheel + sdist + checksums, validates metadata + version alignment
3. **Publish to PyPI** — uses Trusted Publishing (OIDC, `id-token: write`)
4. **GitHub Release** — creates a release with wheel/sdist/checksums attached

No stored tokens. No PAT. No `GITHUB_TOKEN` for publishing. Pure OIDC.

## 5. What goes in PyPI vs GitHub

| Where | What |
|---|---|
| **PyPI** | Project name (`sera-browser`), trusted publisher config (repo + workflow + environment) |
| **GitHub** | The workflow file (`release.yml`), the `pypi` environment, the tag trigger, the private source code |
| **NOT in either** | Long-lived API tokens, passwords, secrets |

## 6. Build artifacts

Each release produces:
- `sera_browser-{version}-py3-none-any.whl` — the wheel (binary distribution)
- `sera_browser-{version}.tar.gz` — the source distribution (sdist)
- `checksums.txt` — SHA-256 hashes of both

Artifacts are NOT committed to the repository — they're published to PyPI and
attached to the GitHub Release as downloadable assets.

## 7. Dependencies

### Runtime (required)

| Dependency | Min version | Purpose |
|---|---|---|
| playwright | >=1.57 | Browser automation engine |
| fastapi | >=0.110 | HTTP API |
| uvicorn[standard] | >=0.29 | ASGI server |
| pydantic | >=2.6 | Data models |
| pydantic-settings | >=2.2 | Env-var configuration |
| httpx | >=0.27 | OpenAI-compatible LLM client |
| anyio | >=4.3 | Async primitives |
| tenacity | >=8.2 | Retry logic |

### Optional

| Extra | Dependency | Purpose |
|---|---|---|
| `[fallback]` | browser-use>=0.1.30 | Optional browser-use fallback driver |
| `[vision]` | Pillow>=10.0 | Set-of-Mark screenshot annotation |
| `[dev]` / `[test]` | pytest, pytest-asyncio, etc. | Development + testing |

Install with extras:
```bash
pip install "sera-browser[fallback,vision,dev]"
```
