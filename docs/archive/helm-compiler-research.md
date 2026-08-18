# Helm Values Compiler - Python Ecosystem Research

Research into Python projects that layer over Helm and Argo CD for config compilation,
values generation, and GitOps workflows.

Date: 2026-03-01

---

## Executive Summary

There is **no single dominant Python project** that does what our HelmValuesCompiler does:
read service configs + deployment configs + environment details and produce `values.yaml`.

The closest tool is **Kapitan** (1,904 stars), but it brings substantial complexity
(Jsonnet, reclass, its own CLI). Our approach -- Pydantic models + deep merge + YAML dump
-- is validated by the ecosystem as the right KISS pattern.

---

## Config Compilation / Management

### Kapitan (Closest Match)

- **URL:** <https://github.com/kapicorp/kapitan>
- **Stars:** 1,904 | **Active:** Yes (v0.34.7, 3.3k weekly PyPI downloads)
- **What:** Generic templated config management. "Inventory" of YAML classes/targets
  compiled via `kapitan compile` into output artifacts (K8s manifests, Helm values).
  Supports Jinja2, Jsonnet, Kadet (Python), and Helm input types. Hierarchical class
  inheritance via reclass merges YAML fragments.
- **Key patterns:**
  - **Inventory = classes + targets**: Classes define defaults; targets compose classes
    and override parameters. Structurally similar to our
    `base config + environment overlay + service-specific override` pattern.
  - **`kapitan compile`**: Single command compiles entire inventory into output.
    Our `HelmValuesCompiler.compile_all()` is the same concept.
  - **Kadet (Python input type)**: Python classes that produce YAML. Exactly what
    Pydantic models do for us.
  - **Helm values pass-through**: Inventory YAML is the SSoT, Helm charts are consumers.
- **Why not adopt:** Heavy framework (reclass dependency, Jsonnet learning curve, its own
  CLI). Overkill for a library. No CNCF backing. Our Pydantic approach is simpler.

### Adobe HIML (Hierarchical YAML Config)

- **URL:** <https://github.com/adobe/himl>
- **Stars:** 133 | **Active:** Yes (v0.20.0)
- **What:** Deep-merges hierarchical YAML files along a path hierarchy
  (e.g., `env=prod/region=us-east-1/cluster=main/app=myservice`). Jinja2 interpolation,
  secrets from Vault/S3/SSM. Inspired by Puppet's Hiera.
- **Key patterns:**
  - **`ConfigProcessor`**: Reads YAML root-to-leaf, deep-merging along the way.
  - **Custom `type_strategies`**: Pluggable merge functions per type.
  - **Path-based hierarchy**: Directory structure IS the configuration hierarchy.
- **Reusable ideas:** Path-based hierarchy for environment/region/cluster/service
  overlays. Pluggable merge strategies if our merge logic grows.

### HiYaPyCo

- **URL:** <https://github.com/zerwes/hiyapyco>
- **Stars:** 116 | **Active:** Low
- **What:** Merge multiple YAML files with Jinja2 interpolation.
- **Key patterns:**
  - `hiyapyco.load('base.yaml', 'env.yaml', 'service.yaml', method=METHOD_MERGE)`
  - `NONE_BEHAVIOR_OVERRIDE` vs `NONE_BEHAVIOR_IGNORE` for controlling null semantics.
- **Reusable ideas:** None handling strategies (does `null` mean "remove key" or "keep
  default"?).

---

## Helm Client Libraries

### pyhelm3

- **URL:** <https://github.com/azimuth-cloud/pyhelm3>
- **Stars:** 79 | **Active:** Moderate (v0.4.0)
- **What:** Async Python client for Helm 3 (wraps `helm` CLI). Lists releases,
  installs/upgrades charts.
- **Relevance:** Only useful if we need to run `helm upgrade` from Python. We generate
  values files for Argo CD, so not needed.

### Avionix

- **URL:** <https://github.com/zbrookle/avionix>
- **Stars:** 76 | **Active:** No (abandoned 2024)
- **What:** OOP Python API for building Helm charts. Python classes for K8s resources.
- **Relevance:** Wrong abstraction level. Builds entire charts, not values files.

---

## Kubernetes Manifest Generation

### cdk8s (CDK for Kubernetes)

- **URL:** <https://github.com/cdk8s-team/cdk8s>
- **Stars:** 4,776 | **Active:** Yes (AWS-backed, 23k weekly PyPI downloads)
- **What:** Define K8s apps in Python/TS/Go/Java. Synthesizes to standard K8s YAML.
  Can import/wrap Helm charts. `cdk8s synth` = compile-to-YAML.
- **Key patterns:**
  - **Constructs tree**: App -> Chart -> Constructs. Composable, reusable abstractions.
  - **`Helm` construct**: Import chart, pass values as Python dict.
  - **`cdk8s import`**: Auto-generate typed Python constructs from chart schemas.
- **Why not adopt:** Replaces Helm entirely (generates manifests, not values.yaml). Depends
  on jsii (JS bridge). Heavier than Pydantic models.

### Pulumi Kubernetes

- **URL:** <https://github.com/pulumi/pulumi-kubernetes>
- **Stars:** 24,850 (main) | **Active:** Very (VC-funded, 12.5k weekly PyPI)
- **What:** Full IaC platform. Python SDK deploys Helm charts, K8s resources.
- **Key patterns:**
  - `values` dict passed to Chart/Release resources.
  - `transformations` array: post-render resource modifications.
- **Why not adopt:** Full IaC platform with its own state backend. Massive overkill.

### k8s-handle (2GIS)

- **URL:** <https://github.com/2gis/k8s-handle>
- **Stars:** 175 | **Active:** Yes
- **What:** CI/CD tool for K8s using Jinja2 templates. Reads `config.yaml` with
  environment sections, renders manifests, applies via K8s API.
- **Key patterns:**
  - **`config.yaml` with environment sections**: Variables per env, templates render
    against them. Similar to our environment-specific config.
  - **Two-phase: template then provision**: Same as our compile-then-commit-to-git.

---

## Deep Merge Libraries

### deepmerge

- **URL:** <https://github.com/toumorokoshi/deepmerge>
- **Stars:** 208 | **Active:** Moderate | **License:** MIT | **Deps:** None
- **What:** Flexible deep merge for Python dicts, lists, sets with configurable strategies.
- **Key patterns:**
  - `always_merger.merge(base, override)` -- single call, last value wins.
  - `conservative_merger` -- keep existing values.
  - Custom `Merger` with per-type strategies: `(list, ["append"]), (dict, ["merge"])`.
  - Fallback and type-conflict strategies.
- **Recommendation:** **Consider adopting** if merge logic needs to handle list strategies
  (Helm replaces lists by default, which can be surprising). Small, MIT, no deps.

### mergedeep

- **URL:** <https://github.com/clarketm/mergedeep>
- **Stars:** 161 | **Active:** Low
- **What:** Simple deep merge: `REPLACE`, `ADDITIVE`, `TYPESAFE_REPLACE`, `TYPESAFE_ADDITIVE`.
- **Key patterns:** Type-safe variant raises on type conflicts. Additive extends
  lists/sets/tuples.

---

## Pydantic + YAML + K8s Patterns

### pydantic-yaml

- **URL:** <https://github.com/NowanIlfideme/pydantic-yaml>
- **Stars:** 190 | **Active:** Yes (Pydantic v2 support)
- **What:** Adds `to_yaml_str()` / `to_yaml_file()` to Pydantic models. Uses ruamel.yaml.
- **Relevance:** We already use ruamel.yaml via `yaml_utils.py`. Could consider if we want
  cleaner model-to-YAML serialization.

---

## Non-Python Tools (Patterns Only)

### Grafana Tanka

- **URL:** <https://github.com/grafana/tanka>
- **Stars:** 2,646 | **Active:** Yes (Grafana Labs production use)
- **What:** K8s config management using Jsonnet. Environments as first-class citizens.
  Go + Jsonnet, not Python. Conceptual pattern (environment overlays -> manifests) identical
  to ours.

### chartpress (JupyterHub)

- **URL:** <https://github.com/jupyterhub/chartpress>
- **Stars:** 58 | **Active:** Yes
- **What:** Automates building Docker images for Helm charts and updating `values.yaml`
  with correct image tags. Git-tag-based versioning.
- **Reusable pattern:** Programmatically updating values.yaml image tags/versions based
  on build context.

### Helmfile

- Go, not Python. Relevant for its layering pattern:
  - Base values -> environment overlay -> release-specific values
  - Deep merge, later files win
  - Same layering pattern our HelmValuesCompiler follows.

---

## Proven Patterns (Adopted)

Based on this research, our approach is validated by the ecosystem:

### 1. Layered Deep Merge

All major tools (Kapitan, HIML, Helmfile) use this pattern:

```text
base_values           (chart defaults)
  + environment.yaml  (prod/staging overrides)
  + service.yaml      (per-service from ServiceConfigRegistry)
  + deployment.yaml   (per-deployment from DeploymentConfigRegistry)
  = final values.yaml
```

### 2. Pydantic Models as Typed Values Schema

Validated by K8S-Pydantic, pydantic-yaml, cdk8s patterns:

```python
class HelmServiceValues(BaseModel):
    image: str
    resources: dict[str, Any] = {}
    keda: HelmKedaConfig = HelmKedaConfig()
    config: dict[str, Any] = {}


values = HelmServiceValues(**merged_config)
yaml_output = yaml.dump(values.model_dump(exclude_none=True))
```

### 3. Single `compile()` Verb

Universal pattern (Kapitan, cdk8s, k8s-handle):

```python
compiler = HelmValuesCompiler(registries, environment)
result = compiler.compile_all()
compiler.write_all(result, output_dir)
```

### 4. Git-Commit-Then-Sync (GitOps)

Universal: compile -> write values.yaml -> git commit + push -> Argo CD auto-syncs.
We have `DirectoryConfigStore` with dulwich git integration.

### 5. KEDA Config Generation

No Python library exists for KEDA config generation. All users define ScaledObject YAML
by hand or through Helm chart values. Our approach of generating KEDA config as part of
values.yaml compilation is correct.

---

## Conclusion

**Do not adopt any framework wholesale.** Our existing approach is correct:

1. **Pydantic models** for typed values schemas
2. **Deep merge** for layered configuration
3. **ruamel.yaml** for YAML output
4. **DirectoryConfigStore + dulwich** for git-backed writes
5. **Single `compile()` method** producing deterministic output

The one library worth considering is **deepmerge** (MIT, no deps) for its per-type merge
strategies, particularly for list handling in Helm values.
