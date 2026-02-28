## DFE Hunt Engine

The DFE Hunt engine provides hunt scheduling and execution for the DFE system. It supports listing, running, viewing, and terminating hunts.

### Configuration

Hunt configuration is managed through the DFE settings cascade:

- `DFE_HUNT_LOG_PATH` — Path to hunt log directory
- Hunt definitions are stored as YAML in the config directory

### API

Hunts are managed programmatically via `dfe_engine.hunts`:

```python
from dfe_engine.hunts import HuntsController

controller = HuntsController(settings)

# List hunts
hunts = controller.list_hunts(look_back_hours=24)

# Run a hunt
controller.run_hunt(hunt_dir="/path/to/hunts", rule_repo_dir="/path/to/rules")

# Kill a specific hunt
controller.kill_hunt(pid=12345)

# Kill all hunts
controller.kill_all_hunts()
```

### Hunt Directory Structure

```
hunts/
  stable/
    hunt/          # Hunt configurations
    rules/         # Hunt rule definitions
```

### Parameters

| Parameter | Description |
|-----------|-------------|
| `hunt_dir` | Directory containing hunt configuration |
| `rule_repo_dir` | Directory containing hunt rule YAML files |
| `hunt_timeout` | Timeout for individual cron tasks (default: 300s) |
| `hunt_num_threads` | Number of threads (default: 4) |
| `log_path` | Path to log directory |
| `hunt_log_path` | Path to hunt logs directory |
