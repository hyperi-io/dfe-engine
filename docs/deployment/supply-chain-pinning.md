# Supply-chain pinning

How dfe-engine and the DFE repos it ships beside pin dependencies. Choose the
latest version that passes the supply-chain rules, then pin it by SHA or digest.

1. Pick the version by the /deps supply-chain rules:
   - A 7-day release-age cooldown: never adopt an external release less than 7
     days old. When the newest is younger, step back to the most recent one at
     least 7 days old.
   - Prefer the LTS line where one exists (runtimes, base images, database and
     runtime majors with a support window). The cooldown picks how fresh, the
     LTS rule picks which line.
   - A security or CVE fix skips the cooldown.
   - Internal HyperI dependencies (`hyperi-io/*`, `scalo`, `dfe-*`,
     `ghcr.io/hyperi-io/*`) have no cooldown.
   - Read release dates from `gh api` or the PyPI JSON API, not the web UI,
     which serves cached release pages.
2. Pin that version by SHA or digest, so a moved tag cannot swap it after review.

| Artefact | Pin |
|---|---|
| PyPI package | The committed `uv.lock` holds a `hash = "sha256:..."` per package, and `pyproject.toml` sets `>=` floors at the pinned version. The lock is the pin |
| Container image | `repo/name:tag@sha256:<digest>` |
| GitHub Action | The full 40-character commit SHA, never `@vN`. hyperi-ci's reusable-workflow caller stays on `@main` |
| Helm chart or operator | A digest where the registry has one, else the version string the same rules chose |

## Where each repo stands

- dfe-engine: `uv.lock` is fully hash-pinned, and both Dockerfile base images
  are `tag@sha256` (`BUILDER_IMAGE` and the runtime `FROM` in `Dockerfile`).
- dfe-docker: every image, the third-party services (ClickHouse, Kafka,
  Redpanda, Kafbat) included, takes the `tag@sha256` that
  `make stack VERSION=X.Y.Z` writes from the DFE stack's version file, and
  `make check-hardfail` stops on any image that can resolve without a digest
  (dfe-docker `scripts/check_hardfail.py`). Its Renovate config <!-- doc-paths: ignore -->
  (`docker:pinDigests`, a 7-day `minimumReleaseAge`) keeps the rest pinned. <!-- doc-paths: ignore -->

The Renovate preset `github>hyperi-io/renovate-config` and hyperi-ci's
quality-stage CVE audit enforce this across the org.
