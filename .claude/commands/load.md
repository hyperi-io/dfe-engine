# Load Session

You are loading project context for a new work session or refreshing my memory.

## Session Initialization

**Step 1: Read project state** (in this order):

- [STATE.md](../../STATE.md) - Current project state and session history
- [TODO.md](../../TODO.md) - Current tasks and priorities

**Step 2: Load standards:**

- [STANDARDS-QUICKSTART.md](../../standards/STANDARDS-QUICKSTART.md) - Core coding standards (includes language/infra file mapping)

**Step 3: Load language and infrastructure standards:**

Scan project root for config files and load matching standards (self-documenting naming):

| If you find... | Load from `standards/languages/` |
|----------------|----------------------------------|
| `pyproject.toml`, `setup.py`, `*.py` | `PYTHON.md` |
| `go.mod` | `GOLANG.md` |
| `package.json`, `tsconfig.json` | `TYPESCRIPT.md` |
| `Cargo.toml` | `RUST.md` |
| `*.sh`, `ci/` directory | `BASH.md` |

| If you find... | Load from `standards/infrastructure/` |
|----------------|---------------------------------------|
| `Dockerfile`, `docker-compose.yaml` | `DOCKER.md` |
| `Chart.yaml`, `helmfile.yaml` | `K8S.md` |
| `*.tf` | `TERRAFORM.md` |
| `ansible.cfg`, `playbooks/` | `ANSIBLE.md` |

---

## Ready to Work

After loading documentation:

1. Check git status and recent commits
2. Be ready - no greetings, wait for the user's first task
