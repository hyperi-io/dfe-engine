# Supply-chain pinning

How dfe-engine (and the DFE repos it ships alongside) pin dependencies. The one
rule: **choose the latest version that passes the supply-chain rules, then pin
it by SHA/digest.**

## The two steps

1. **Pick the version** by the /deps supply-chain rules:
   - 7-day release-age cooldown - never adopt an external release published less
     than 7 days ago. If the newest release is too fresh, step back to the most
     recent one that is at least 7 days old.
   - Prefer the LTS line where one exists (runtimes, base images, DB/runtime
     majors with a support window). Cooldown picks how fresh; LTS picks which
     line.
   - Security/CVE fixes bypass the cooldown - adopt the patch now.
   - Internal HyperI deps (`hyperi-io/*`, `scalo`, `dfe-*`, `ghcr.io/hyperi-io/*`)
     have no cooldown - adopt immediately.
   - Verify release dates via `gh api` / the PyPI JSON API, NOT the cached web
     UI (it serves stale release pages).

2. **Pin that version by SHA/digest** so a moved tag cannot swap it after review:
   - PyPI: the committed `uv.lock` carries per-package `hash = "sha256:..."`.
     `pyproject.toml` uses `>=` floors at the pinned version; the lock is the
     hash-authoritative pin.
   - Container images: `repo/name:tag@sha256:<digest>`.
   - GitHub Actions: the full 40-char commit SHA (not `@vN`). Exception:
     hyperi-ci's own `@main` reusable-workflow caller is left on `@main`.
   - Helm charts / operators: digest/SHA where supported, else the version
     string chosen by the same rule.

## Where each repo stands

- **dfe-engine**: `uv.lock` is fully hash-pinned. Dockerfile base images and the
  Helm chart image ref still use floating tags - digest-pin those at build/deploy
  time.
- **dfe-docker**: third-party services (ClickHouse, Kafka, Redpanda, Kafbat) are
  pinned `tag@sha256` in `.env.example`, with Renovate (`docker:pinDigests` + a
  custom `.env.example` manager) keeping them current under a 7-day cooldown.
  The DFE-owned service images and the engine image float to `:latest` and must
  be brought onto the same `tag@sha256` pattern, pinned per dfe-docker release.

Automation enforcing this org-wide: the Renovate preset
`github>hyperi-io/renovate-config` and hyperi-ci's quality-stage CVE audit.
