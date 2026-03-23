# WASM Transform Module Versioning — Work Breakdown Structure

**Date:** 2026-03-19
**Status:** Backlog (long-term)
**Owner:** dfe-engine (not dfe-transform-wasm)
**Origin:** Moved from dfe-transform-wasm Phase 7.4 — versioning is a deployment/orchestration concern, not a runtime concern.

---

## Goal

Track versions of compiled WASM transform modules, support rollback to
previous versions, and provide a registry for storing and retrieving
module artefacts.

## Why dfe-engine, Not dfe-transform-wasm

- dfe-engine already manages the transform lifecycle: compile, store, deploy
- dfe-engine's config registry already stores transform metadata
- The WASM host binary (dfe-transform-wasm) loads a module from a file path
  at startup — it has no concept of versions, only a path
- Rollback is a deployment operation (ArgoCD/Helm rollback), not a runtime operation
- WASM modules require pod restart to take effect — no hot-swap

## Scope

### Module Registry

Store compiled WASM artefacts with version metadata:

| Field | Type | Description |
|-------|------|-------------|
| `transform_name` | string | Unique name (matches pipeline config) |
| `version` | semver | Module version (auto-incremented or explicit) |
| `language` | enum | rust / go / assemblyscript |
| `source_hash` | SHA-256 | Hash of source code that produced this module |
| `wasm_hash` | SHA-256 | Hash of compiled .wasm binary |
| `wasm_size_bytes` | u64 | Compiled module size |
| `compiled_at` | RFC 3339 | Compilation timestamp |
| `compiled_by` | string | User or CI job that triggered compilation |
| `storage_url` | string | S3/MinIO URL where artefact is stored |
| `status` | enum | active / superseded / deprecated / failed |

### Storage Backend

- **Primary:** S3-compatible object storage (MinIO in DevEx, S3 in prod)
- **Key pattern:** `transforms/{name}/v{version}/transform.wasm`
- **Metadata:** Stored in dfe-engine's config registry (PostgreSQL or directory-based)

### API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/transforms/{name}/versions` | List all versions of a transform |
| `GET` | `/api/transforms/{name}/versions/{version}` | Get metadata for a specific version |
| `GET` | `/api/transforms/{name}/versions/latest` | Get latest active version |
| `POST` | `/api/transforms/{name}/versions` | Register a new version (after compilation) |
| `POST` | `/api/transforms/{name}/deploy/{version}` | Deploy a specific version (triggers Helm update) |
| `POST` | `/api/transforms/{name}/rollback` | Rollback to previous active version |
| `DELETE` | `/api/transforms/{name}/versions/{version}` | Mark a version as deprecated |

### Deploy Flow

1. User compiles transform via dfe-engine compile endpoint (already exists)
2. Compile service stores artefact in S3 and registers version in registry
3. User triggers deploy via API or UI
4. dfe-engine updates Helm values (`wasmModule.httpUrl` points to new version's S3 URL)
5. ArgoCD syncs, pod restarts with new module

### Rollback Flow

1. User calls rollback API or uses UI
2. dfe-engine looks up the previous active version in the registry
3. dfe-engine updates Helm values to point at the previous version's S3 URL
4. ArgoCD syncs, pod restarts with previous module

### Integration with Existing Systems

- **Compile service** (`dfe-transform-wasm/crates/compiler/`) — already compiles modules. Needs to upload artefact to S3 and call the version registration API after successful compilation.
- **Helm chart** — already uses `wasmModule.httpUrl`. No chart changes needed — dfe-engine just sets the URL to the versioned S3 path.
- **dfe-ui** — Phase 6.6 (deploy flow UI) would surface version selection and rollback in the UI.
- **ArgoCD** — handles the actual pod rollout. Helm rollback (`argocd app rollback`) also works as a manual fallback.

## Work Breakdown

### Phase 1: Registry + Storage

1. [ ] Design version metadata schema (Pydantic model in dfe-engine)
2. [ ] Implement S3 artefact upload in compile endpoint
3. [ ] Implement version registration endpoint (`POST /api/transforms/{name}/versions`)
4. [ ] Implement version listing endpoint (`GET /api/transforms/{name}/versions`)
5. [ ] Implement latest version endpoint (`GET /api/transforms/{name}/versions/latest`)

### Phase 2: Deploy + Rollback

1. [ ] Implement deploy endpoint (`POST /api/transforms/{name}/deploy/{version}`)
2. [ ] Integrate with HelmValuesCompiler to update `wasmModule.httpUrl`
3. [ ] Implement rollback endpoint (`POST /api/transforms/{name}/rollback`)
4. [ ] Add version status tracking (active/superseded/deprecated)

### Phase 3: UI Integration

1. [ ] Version list in transform editor (dfe-ui via dfe-engine API)
2. [ ] Deploy version selector
3. [ ] Rollback button with confirmation
4. [ ] Version diff view (source code changes between versions)

## Dependencies

- S3-compatible storage (MinIO in DevEx, S3 in prod)
- dfe-engine config registry (for metadata)
- dfe-transform-wasm compile service (for artefact production)
- ArgoCD (for deployment rollout)

## Not In Scope

- WASM module hot-swap (requires pod restart — decided in dfe-transform-wasm)
- Runtime version detection (host binary doesn't know about versions)
- Multi-version concurrent execution (one module per pod)
