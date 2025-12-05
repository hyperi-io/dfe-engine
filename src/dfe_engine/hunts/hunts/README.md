## DFE Hunt CLI

The DFE Hunt CLI provides a set of commands to interact with hunts in the DFE system. It allows users to list hunts, run hunts, view hunt details, and kill hunts based on process IDs (PIDs).

### Setup

Configure the necessary YAML files (`dfe_package.yaml` and `dfe_targets.yaml`) according to your environment and requirements.


#### dfe_package.yaml

The `dfe_package.yaml` file serves as the primary configuration file for the DFE Schema Builder. It contains global settings and parameters used by the tool during schema generation and application.

Example dfe_package,yaml format : 

```yaml
global_settings:
  default_target: localhost
  target_path: ~/.dfe/dfe_targets.yaml

hunt_scheduler:
  hunt_dir: ../.dfe_hunt_config/windows_smd_hunts/hunts/
  rule_repo_dir: ../.dfe_hunt_config/windows_smd_hunts/rules/
  num_threads: 4

```

#### dfe_targets.yaml

The `dfe_targets.yaml` file defines different target environments, such as development, staging, and production. Each target environment specifies configurations specific to that environment, allowing for seamless deployment across different setups.


Example dfe_targets.yaml format: 

#### `dfe_targets.yaml`

```yaml
default_target: dev
targets:
  dev:
    # Development environment configurations
    ch_host: localhost
    ch_port: 8123
    hunt_config_path: ../.dfe_hunt_config/first_ten_windows_audit_hunt/hunt
    hunt_rules_path: ../.dfe_hunt_config/first_ten_windows_audit_hunt/rules
    # Other environment configurations....
```




# Hunt CLI Commands

## 1. Print Hunt Parameters

Print DFE hunt parameters.

### Command:

```bash
dfecli print-hunt-parameters --dfe_package_file_path /path/to/dfe_package.yaml --log_path /path/to/logs --hunt_log_path /path/to/hunt_logs --target production --target_file_path /pathto/dfe_targets.yaml
```

### Options/Parameters:

- `--dfe_package_file_path`: Path to configuration file
- `--log_path`: Path to log path directory
- `--hunt_log_path`: Path to hunt logs directory
- `--target`: Target name for specific environment
- `--target_file_path`: Path to the targets configuration file

## 2. Run Hunt

Run DFE hunt.

### Command:

```bash
dfecli run-hunt --dfe_package_file_path /path/to/dfe_package.yaml --hunt_dir /path/to/hunt_dir --hunt_rule_repo_dir /path/to/hunt_rules --hunt_timeout 60 --hunt_num_threads 4 --log_path /path/to/logs --hunt_log_path /path/to/hunt_logs --target production --target_file_path /path/to/dfe_targets.yaml
```

### Options/Parameters:

- `--dfe_package_file_path`: Path to the configuration file
- `--hunt_dir`: Directory containing hunt configuration
- `--hunt_rule_repo_dir`: Directory containing target YAML files
- `--hunt_timeout`: Timeout setting for individual Cron tasks
- `--hunt_num_threads`: Number of threads
- `--log_path`: Path to log path directory
- `--hunt_log_path`: Path to hunt logs directory
- `--target`: Target name for specific environment
- `--target_file_path`: Path to the targets configuration file

## 3. Kill All Hunts

Terminate all activities based on PIDs in the thread_tracking.log and delete the file.

### Command:

```bash
dfecli kill-all-hunts --log_path /path/to/logs --hunt_log_path /path/to/hunt_logs
```

### Options/Parameters:

- `--log_path`: Path to log path directory
- `--hunt_log_path`: Path to hunt logs directory

## 4. List Hunts

List hunts within a specified time range.

### Command:

```bash
dfecli list-hunts --look_back_hours 24 --log_path /path/to/logs --hunt_log_path /path/to/hunt_logs
```

### Options/Parameters:

- `--look_back_hours`: Number of hours to look back for hunts
- `--log_path`: Path to log path directory
- `--hunt_log_path`: Path to hunt logs directory

## 5. View Hunts

Retrieve and view hunts associated with one or more process IDs (PIDs).

### Command:

```bash
dfecli view-hunts --view_pid 12345 --log_path /path/to/logs --hunt_log_path /path/to/hunt_logs
```

### Options/Parameters:

- `--view_pid`: Process ID (PID) or list of PIDs to fetch hunts for
- `--log_path`: Path to log path directory
- `--hunt_log_path`: Path to hunt logs directory

## 6. Kill Hunt

Terminate a specific hunt process based on the provided PID.

### Command:

```bash
dfecli kill-hunt --dfe_package_file_path /path/to/dfe_package.yaml --kill_pid 12345 --log_path /path/to/logs
```

### Options/Parameters:

- `--dfe_package_file_path`: Path to the DFE package configuration file
- `--kill_pid`: PID of the process to stop
- `--log_path`: Path to log path directory 