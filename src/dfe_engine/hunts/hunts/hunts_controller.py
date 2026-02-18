import asyncio
import os
import sys
from pathlib import Path
import signal
import pandas as pd
from tabulate import tabulate
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union
from hyperi_pylib.logger import logger
from .hunts_scheduler import HuntScheduler
from ..runner.cron_runner import CronRunner
from ...config.config_loader import DFEConfigLoader
from ...settings import get_settings
import multiprocessing
import time


class HuntController:
    @staticmethod
    def process_hunt(
        arg_dfe_config_path: str,
        arg_hunt_dir: Optional[str],
        arg_hunt_rule_repo_dir: Optional[str],
        arg_hunt_timeout: int,
        arg_hunt_num_threads: int,
        arg_checkpoint_destination: str,
        arg_checkpoint_timestamp_field: str,
        arg_dfe_log_path: str,
        arg_hunt_log_path: Optional[str],
        arg_target: Optional[str],
        arg_target_file_path: Optional[str],
        test_mode: bool = False,
        verbose: bool = False,
    ) -> None:
        dfe_config = HuntController._load_dfe_config(arg_dfe_config_path, arg_target_file_path)
        if dfe_config is None:
            return

        config_values = HuntController._resolve_config_values(
            dfe_config,
            arg_target_file_path,
            arg_hunt_timeout,
            arg_hunt_num_threads,
            arg_checkpoint_destination,
            arg_hunt_log_path,
            arg_hunt_dir,
            arg_hunt_rule_repo_dir,
            arg_checkpoint_timestamp_field,
        )

        target_config_data = HuntController._load_target_config(
            arg_target, config_values["targets_file_path"]
        )
        if target_config_data is None:
            return

        paths = HuntController._resolve_hunt_paths(config_values, target_config_data)
        if paths is None:
            return

        hunt_dirs, rule_dirs = HuntController._validate_directories(
            paths["hunt_config_path"], paths["hunt_rules_path"]
        )
        if hunt_dirs is None:
            return

        hunt_log_path = HuntController._resolve_hunt_log_path(config_values["hunt_log_path"])

        HuntController._start_schedulers(
            hunt_dirs,
            rule_dirs,
            config_values,
            hunt_log_path,
            target_config_data,
            test_mode,
        )

    @staticmethod
    def _load_dfe_config(config_path: str, target_file_path: Optional[str]) -> Optional[dict]:
        """Load DFE config and perform initial validation."""
        try:
            dfe_config = DFEConfigLoader.load_dfe_package(
                config_path, require_config=False, logger=logger
            )
        except FileNotFoundError as error:
            logger.error(f"Error loading the dfe config package: {error}")
            return None

        if target_file_path is None:
            logger.warning(
                f" the arg_target_file_path is None. Please review the parameters.  Value [{target_file_path}]"
            )

        if dfe_config.get("hunt_scheduler") is None:
            logger.warning(
                " the dfe_package.yaml has no hunt scheduler configration files is None. You will need to ensure that you have passed in all settings via the CLI."
            )

        return dfe_config

    @staticmethod
    def _resolve_config_values(
        dfe_config: dict,
        arg_target_file_path: Optional[str],
        arg_hunt_timeout: int,
        arg_hunt_num_threads: int,
        arg_checkpoint_destination: str,
        arg_hunt_log_path: Optional[str],
        arg_hunt_dir: Optional[str],
        arg_hunt_rule_repo_dir: Optional[str],
        arg_checkpoint_timestamp_field: str,
    ) -> dict:
        """Resolve all configuration values from args and config."""
        return {
            "targets_file_path": HuntController._get_config_value(
                arg_target_file_path, dfe_config, "global_settings", "target_path"
            ),
            "hunt_checkpoint_path": dfe_config.get("global_settings", {}).get(
                "hunt_checkpoint_path", None
            ),
            "hunt_cron_task_timeout": HuntController._get_config_value(
                arg_hunt_timeout, dfe_config, "hunt_scheduler", "timeout", default=-1
            ),
            "hunt_num_threads": HuntController._get_config_value(
                arg_hunt_num_threads, dfe_config, "hunt_scheduler", "num_threads", default=1
            ),
            "checkpoint_destination": HuntController._get_config_value(
                arg_checkpoint_destination,
                dfe_config,
                "hunt_scheduler",
                "checkpoint_destination",
                "clickhouse",
            ),
            "hunt_log_path": HuntController._get_config_value(
                arg_hunt_log_path,
                dfe_config,
                "hunt_scheduler",
                "hunt_log_path",
                default=os.path.join(os.getcwd(), CronRunner.DEFAULT_HUNT_LOG_FILE_PATH),
            ),
            "hunt_config_path": HuntController._get_config_value(
                arg_hunt_dir, dfe_config, "hunt_scheduler", "hunt_dir"
            ),
            "hunt_rules_path": HuntController._get_config_value(
                arg_hunt_rule_repo_dir, dfe_config, "hunt_scheduler", "rule_repo_dir"
            ),
            "checkpoint_timestamp_field": HuntController._get_config_value(
                arg_checkpoint_timestamp_field,
                dfe_config,
                "hunt_scheduler",
                "checkpoint_timestamp_field",
                "timestamp",
            ),
        }

    @staticmethod
    def _load_target_config(
        target_name: Optional[str], targets_file_path: Optional[str]
    ) -> Optional[dict]:
        """Load target configuration."""
        if targets_file_path is None:
            logger.warning(
                f" the targets_file_path is still set to None. Please review the parameters and the dfe_package file. Value [{targets_file_path}]"
            )

        try:
            logger.info(
                f" Reading Target Config of {target_name} from this file {targets_file_path}"
            )
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=target_name, targets_file_path=targets_file_path
            )

            DFEConfigLoader.print_target(
                logger=logger,
                targets_file_path=targets_file_path,
                target_name=target_name,
            )
            return target_config_data
        except FileNotFoundError as error:
            logger.error(
                f"Unable to load target or retrieve the hunt config paths: {error}",
                exc_info=True,
            )
            raise

    @staticmethod
    def _resolve_hunt_paths(config_values: dict, target_config_data: dict) -> Optional[dict]:
        """Resolve hunt and rules paths, validating required values."""
        hunt_config_path = config_values["hunt_config_path"] or target_config_data.get(
            "hunt_config_path", None
        )
        hunt_rules_path = config_values["hunt_rules_path"] or target_config_data.get(
            "hunt_rules_path", None
        )

        if not config_values["targets_file_path"]:
            logger.error("Target file path is missing.")
            return None
        if not hunt_config_path:
            logger.error("Hunt config path is missing.")
            return None
        if not hunt_rules_path:
            logger.error("Hunt rules path is missing.")
            return None

        return {
            "hunt_config_path": hunt_config_path,
            "hunt_rules_path": hunt_rules_path,
        }

    @staticmethod
    def _validate_directories(hunt_config_path: str, hunt_rules_path: str) -> Optional[tuple]:
        """Parse and validate hunt and rule directories."""
        hunt_dirs = (
            [dir.strip() for dir in hunt_config_path.split(",")]
            if "," in hunt_config_path
            else [hunt_config_path]
        )
        rule_dirs = (
            [dir.strip() for dir in hunt_rules_path.split(",")]
            if "," in hunt_rules_path
            else [hunt_rules_path]
        )

        logger.info(
            f"Processing {len(hunt_dirs)} hunt directories and {len(rule_dirs)} rule directories"
        )

        if len(hunt_dirs) != len(rule_dirs):
            error_msg = (
                f"Number of hunt directories ({len(hunt_dirs)}) does not match "
                f"number of rule directories ({len(rule_dirs)}). Please ensure they are paired correctly."
            )
            logger.error(error_msg)
            raise ValueError(error_msg)

        for hunt_dir in hunt_dirs:
            if not os.path.isdir(hunt_dir):
                raise Exception(f"Hunt directory [{hunt_dir}] not found.")

        for rule_dir in rule_dirs:
            if not os.path.isdir(rule_dir):
                raise Exception(f"Rules repository directory [{rule_dir}] not found.")

        return hunt_dirs, rule_dirs

    @staticmethod
    def _resolve_hunt_log_path(hunt_log_path: Optional[str]) -> str:
        """Resolve the hunt log path from config or settings."""
        if not hunt_log_path:
            settings = get_settings()
            return settings.hunts.log_path or os.path.join(
                os.getcwd(), CronRunner.DEFAULT_HUNT_LOG_FILE_PATH
            )
        return hunt_log_path

    @staticmethod
    def _start_schedulers(
        hunt_dirs: List[str],
        rule_dirs: List[str],
        config_values: dict,
        hunt_log_path: str,
        target_config_data: dict,
        test_mode: bool,
    ) -> None:
        """Start hunt schedulers for each directory pair."""
        processes = []

        for hunt_dir, rule_dir in zip(hunt_dirs, rule_dirs, strict=False):
            logger.info(
                f"Starting scheduler for paired hunt directory: {hunt_dir} and rule directory: {rule_dir}"
            )

            if test_mode:
                HuntController._run_scheduler(
                    hunt_dir,
                    rule_dir,
                    config_values["hunt_checkpoint_path"],
                    config_values["hunt_cron_task_timeout"],
                    config_values["hunt_num_threads"],
                    config_values["checkpoint_destination"],
                    hunt_log_path,
                    logger,
                    config_values["checkpoint_timestamp_field"],
                    target_config_data,
                )
            else:
                process = multiprocessing.Process(
                    target=HuntController._run_scheduler,
                    args=(
                        hunt_dir,
                        rule_dir,
                        config_values["hunt_checkpoint_path"],
                        config_values["hunt_cron_task_timeout"],
                        config_values["hunt_num_threads"],
                        config_values["checkpoint_destination"],
                        hunt_log_path,
                        logger,
                        config_values["checkpoint_timestamp_field"],
                        target_config_data,
                    ),
                )
                process.start()
                processes.append(process)
                time.sleep(1)

        logger.info("Hunt scheduler processes started.")

        if not test_mode:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("Exiting main process. Hunt scheduler daemons will continue running.")

    @staticmethod
    def _run_scheduler(
        hunt_dir,
        rule_dir,
        hunt_checkpoint_path,
        hunt_cron_task_timeout,
        hunt_num_threads,
        checkpoint_destination,
        hunt_log_path,
        logger,
        checkpoint_timestamp_field,
        target_config_data,
    ):
        try:
            scheduler = HuntScheduler(
                hunt_dir=hunt_dir,
                rule_repo_dir=rule_dir,
                hunt_checkpoint_path=hunt_checkpoint_path,
                hunt_cron_task_timeout=hunt_cron_task_timeout,
                num_threads=hunt_num_threads,
                checkpoint_destination=checkpoint_destination,
                hunt_log_path=hunt_log_path,
                logger=logger,
                checkpoint_timestamp_field=checkpoint_timestamp_field,
                target_config_data=target_config_data,
            )

            scheduler.print_hunt_parameters()
            asyncio.run(scheduler.schedule_hunts_async())
        except Exception as e:
            logger.error(
                f"Error running scheduler for hunt directory {hunt_dir} and rule directory {rule_dir}: {e}",
                exc_info=True,
            )

    @staticmethod
    def _get_config_value(
        arg_value: Optional[Any],
        config: dict,
        section: str,
        key: str,
        default: Optional[Any] = None,
    ) -> Any:
        """
        Get config value from the provided arguments or fallback to config file.

        Params:
        - arg_value (Any, optional): Argument value.
        - config (dict): Configuration dictionary.
        - section (str): Configuration section.
        - key (str): Configuration key.
        - default (Any, optional): Default value if both arg_value and config are missing. Defaults to None.

        Returns:
        - Any: Resolved configuration value.
        """
        return arg_value if arg_value is not None else config.get(section, {}).get(key, default)

    @staticmethod
    def list_hunts(
        args_look_back_hours: int,
        args_log_path: Optional[str],
        args_hunt_log_path: Optional[str],
    ) -> None:
        """
        List hunts within a specified time range.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_look_back_hours (int): Hours to go back and fetch the hunts.
            args_log_path (Optional[str]): Path to the common directory for log files.
            args_hunt_log_path (Optional[str]): Path to the hunt files directory for log files.
        """

        if not args_hunt_log_path:
            settings = get_settings()
            args_hunt_log_path = settings.hunts.log_path or os.path.join(
                os.getcwd(), CronRunner.DEFAULT_HUNT_LOG_FILE_PATH
            )

        try:
            config = DFEConfigLoader.load_dfe_package(require_config=False, logger=logger)
        except FileNotFoundError as error:
            logger.error(f"Error loading DFE package: {error}")
            return

        args_log_path or config.get("global_settings", {}).get(
            "tmp/logs/", os.path.join(os.getcwd(), "tmp/logs/")
        )
        hunt_log_file_path = os.path.join(args_hunt_log_path, CronRunner.THREAD_TRACKING_LOG)

        start_time = datetime.now(timezone.utc) - timedelta(hours=args_look_back_hours)
        end_time = datetime.now(timezone.utc)
        log_entries = HuntController.parse_logs(hunt_log_file_path, start_time, end_time)

        if log_entries:
            df = pd.DataFrame(log_entries)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values(by="timestamp", ascending=False).drop_duplicates(
                subset=["pid", "thread_id"], keep="first"
            )

            df["process_info"] = (
                df["pid"].astype(int).apply(lambda pid: HuntController.get_process_info(pid) or {})
            )
            df["status"] = df["process_info"].apply(
                lambda info: info.get("status", "stopped") if isinstance(info, dict) else "stopped"
            )
            df["status"] = df["status"].map(
                lambda x: "running" if x in ["sleeping", "running", "started"] else x
            )
            df["num_threads"] = df["process_info"].apply(
                lambda info: info.get("num_threads", "0") if isinstance(info, dict) else "0"
            )

            display_df = df[
                [
                    "hunt_name",
                    "timestamp",
                    "pid",
                    "thread_id",
                    "hunt_source",
                    "status",
                    "num_threads",
                ]
            ]
            table_str = tabulate(display_df, headers="keys", tablefmt="grid", showindex=False)
            formatted_templates = []
            headers = []
            logger.info(
                f"\n\n{table_str}" + tabulate(formatted_templates, headers=headers, tablefmt="grid")
            )
        else:
            logger.warning("PID not found")

    @staticmethod
    def view_hunts(
        args_pid: Union[int, List[int]],
        args_log_path: Optional[str],
        args_hunt_log_path: Optional[str],
    ) -> None:
        """
        View hunts associated with one or more process IDs (PIDs).

        Parameters:
            args_pid (Union[int, List[int]]): Process ID (PID) or list of PIDs to fetch hunts for.
            args_log_path (Optional[str]): Path to the common directory for log files.
            args_hunt_log_path (Optional[str]): Path to the hunt files directory for log files.
        """

        if not args_hunt_log_path:
            settings = get_settings()
            args_hunt_log_path = settings.hunts.log_path or os.path.join(
                os.getcwd(), CronRunner.DEFAULT_HUNT_LOG_FILE_PATH
            )

        try:
            config = DFEConfigLoader.load_dfe_package(require_config=False, logger=logger)
        except FileNotFoundError as error:
            logger.error(f"Error loading DFE package: {error}")
            return

        args_log_path or config.get("global_settings", {}).get(
            "tmp/logs/", os.path.join(os.getcwd(), "tmp/logs/")
        )
        pids = [args_pid] if not isinstance(args_pid, list) else args_pid

        hunt_log_file_path = os.path.join(args_hunt_log_path, CronRunner.THREAD_TRACKING_LOG)
        data = []

        try:
            with open(hunt_log_file_path, "r") as file:
                for line in file:
                    log_entry = HuntController.parse_log_entry(line)
                    if int(log_entry["pid"]) in pids:
                        data.append(log_entry)
        except FileNotFoundError:
            logger.warning(f"Log file not found: {hunt_log_file_path}")
            return

        if data:
            df = pd.DataFrame(data)
            df["pid"] = df["pid"].astype(int)
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df.sort_values(by="timestamp", ascending=False, inplace=True)
            df.drop_duplicates(subset=["timestamp", "pid", "hunt_file"], keep="first", inplace=True)

            process_info_series = df["pid"].apply(HuntController.get_process_info).apply(pd.Series)
            if (
                "status" in process_info_series.columns
                and "num_threads" in process_info_series.columns
            ):
                df = pd.concat([df, process_info_series], axis=1)
                df["status"] = df["status"].fillna("stopped")
                df["status"] = df["status"].map(
                    lambda x: "running" if x in ["sleeping", "running", "started"] else x
                )
                df["num_threads"] = df["num_threads"].fillna("unknown").astype("Int64")

                if df.empty:
                    logger.warning("PID not found")
                else:
                    data_list = df[
                        [
                            "hunt_name",
                            "timestamp",
                            "pid",
                            "hunt_file",
                            "hunt_source",
                            "status",
                            "num_threads",
                        ]
                    ].to_dict(orient="records")
                    tabulated_output = tabulate(data_list, headers="keys", tablefmt="grid")
                    logger.info(f"\n\n{tabulated_output}")
            else:
                logger.warn("PID not found")
        else:
            logger.warn("PID not found")

    @staticmethod
    def print_hunt_parameters(
        args_dfe_package_file_path: str,
        args_log_path: str,
        args_hunt_log_path: Optional[str],
        args_target: Optional[str],
        args_target_file_path: Optional[str],
    ) -> None:
        """
        Logs configuration settings including hunt directory, rule repository directory, timeout, number of threads, log paths, and credentials file path.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_log_path (str): Path to the DFE log directory.
            args_hunt_log_path (Optional[str]): Path to the hunt logs directory.
            args_target (Optional[str]): The name of the target environment.
            args_target_file_path (Optional[str]): The location of the dfe_target file.
        """

        try:
            dfe_config = DFEConfigLoader.load_dfe_package(
                config_file_path=args_dfe_package_file_path,
                require_config=False,
            )
            dfe_config_target_path = dfe_config["global_settings"].get("target_path", None)
        except FileNotFoundError as error:
            logger.error(f"Error: Was not able to load the dfe_package.yaml:\n{error}")
            return

        try:
            target_path = args_target_file_path if args_target_file_path else dfe_config_target_path
            target_config_data = DFEConfigLoader.read_target_config(
                target_name=args_target, targets_file_path=target_path
            )
            hunt_config_path = target_config_data["hunt_config_path"]
            hunt_rules_path = target_config_data["hunt_rules_path"]
        except FileNotFoundError as error:
            logger.error(f"Unable to load target: {error}")
            sys.exit(1)

        num_threads = dfe_config["hunt_scheduler"]["num_threads"]
        credentials_file_path = dfe_config["global_settings"]["target_path"]
        dfe_package_hunt_dir = dfe_config["hunt_scheduler"]["hunt_dir"]
        dfe_package_rule_repo_dir = dfe_config["hunt_scheduler"]["rule_repo_dir"]

        if not args_hunt_log_path:
            settings = get_settings()
            args_hunt_log_path = settings.hunts.log_path or os.path.join(
                os.getcwd(), "default_hunt_log_path"
            )

        hunt_configuration_settings = {
            "Configuration Data:": dfe_config,
            "Root target file Hunt directory:": hunt_config_path,
            "Root target file Rule repository directory:": hunt_rules_path,
            "DFE package hunt directory:": dfe_package_hunt_dir,
            "DFE package rule repository directory:": dfe_package_rule_repo_dir,
            "Number of threads:": num_threads,
            "Common log path:": args_log_path,
            "Target Hunt logs directory:": args_hunt_log_path,
            "Configuration root path:": credentials_file_path,
        }

        logger.info("\n--- Hunt Configuration Settings ---\n")
        for key, value in hunt_configuration_settings.items():
            logger.info(f"{key:75} {value}")
        logger.info("-----------------------------------\n")

        try:
            rules = os.listdir(hunt_rules_path)
            for entry in rules:
                entry_path = os.path.join(hunt_rules_path, entry)
                if os.path.isfile(entry_path):
                    logger.info(f"Rule Template Loading: {entry}")
        except FileNotFoundError as e:
            logger.error(f"Error accessing directory '{hunt_rules_path}': {e}")

    @staticmethod
    def kill_all_hunts(args_log_path: str, args_hunt_log_path: Optional[str]) -> None:
        """
        Terminate all activities based on PIDs in the thread_tracking.log and delete the file.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_log_path (str): Path to the DFE log directory.
            args_hunt_log_path (Optional[str]): Path to the hunt logs directory.
        """

        if not args_hunt_log_path:
            settings = get_settings()
            args_hunt_log_path = settings.hunts.log_path or os.path.join(
                os.getcwd(), "default_hunt_log_path"
            )

        thread_tracking_file_path = Path(args_hunt_log_path) / "thread_tracking.log"
        if thread_tracking_file_path.exists():
            try:
                with thread_tracking_file_path.open("r") as file:
                    for line in file:
                        try:
                            pid_part = int(line.split("pid=")[1].split(",")[0].strip())
                            os.kill(pid_part, signal.SIGTERM)
                            logger.info(f"Terminated process with PID: {pid_part}")
                        except ValueError:
                            logger.error(f"Invalid PID format in line: {line.strip()}")
                        except ProcessLookupError:
                            logger.error(f"No process found with PID: {pid_part}")

                thread_tracking_file_path.unlink()
                logger.info(f"Deleted tracking file: {thread_tracking_file_path}")
            except Exception as e:
                logger.error(f"Error handling thread tracking file: {e}")
        else:
            logger.info("No tracking file found.")

    @staticmethod
    def kill_hunt(
        args_dfe_package_file_path: str,
        args_kill_pid: Optional[int],
        args_log_path: Optional[str],
    ) -> None:
        """
        Kill Hunt

        Kills a hunt associated with a specified PID.

        Parameters:
            args_dfe_package_file_path (str): Path to the DFE package configuration file.
            args_kill_pid (Optional[int]): PID of the process to stop.
            args_log_path (Optional[str]): Path to the common directory for log files.
        """

        try:
            dfe_package_config = DFEConfigLoader.load_dfe_package(
                args_dfe_package_file_path, require_config=False, logger=logger
            )
        except FileNotFoundError as error:
            logger.error(f"Error loading DFE package: {error}")
            return

        args_log_path = args_log_path or dfe_package_config.get("global_settings", {}).get(
            "tmp/logs/", os.path.join(os.getcwd(), "tmp/logs/")
        )

        DFEConfigLoader.print_default_target(logger=logger)

        try:
            os.kill(args_kill_pid, signal.SIGTERM)
            logger.info(f"Killed process with PID: {args_kill_pid}")
        except ProcessLookupError:
            logger.warning(f"Process with PID {args_kill_pid} not found.")

    @staticmethod
    def parse_log_entry(line: str) -> Dict[str, Any]:
        """
        Parse a single log entry into a dictionary.

        Parameters:
            line (str): A single line from the log file.

        Returns:
            Dict[str, Any]: A dictionary containing log entry data.
        """
        log_entry = {}
        for part in line.strip().split(","):
            key, value = part.split("=")
            log_entry[key] = value
        return log_entry

    @staticmethod
    def parse_logs(
        log_file_path: str, start_time: datetime, end_time: datetime
    ) -> List[Dict[str, Any]]:
        """
        Parse the logs from the log file within a specified time frame.

        Parameters:
            log_file_path (str): The path to the log file.
            start_time (datetime): The start datetime to filter logs.
            end_time (datetime): The end datetime to filter logs.

        Returns:
            List[Dict[str, Any]]: A list of log entry dictionaries.
        """
        log_entries = []
        with open(log_file_path, "r") as log_file:
            for line in log_file:
                log_entry = HuntController.parse_log_entry(line)
                log_date = datetime.strptime(log_entry["timestamp"], "%Y-%m-%d %H:%M:%S")
                log_date = log_date.replace(tzinfo=timezone.utc)
                if start_time <= log_date <= end_time:
                    log_entries.append(log_entry)
        return log_entries

    @staticmethod
    def get_process_info(pid: int) -> Dict[str, Any]:
        """
        Retrieve process information for a given PID.

        Parameters:
            pid (int): Process ID to retrieve information for.

        Returns:
            Dict[str, Any]: A dictionary containing process information.
        """
        # Example implementation to get process info
        return {
            "status": "running" if pid % 2 == 0 else "sleeping",
            "num_threads": "4" if pid % 2 == 0 else "2",
        }
