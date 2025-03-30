#!/usr/bin/env python3
"""
BitaxePID Auto-Tuner Module

This module provides an auto-tuning system for Bitaxe miners. It manages stratum pool configurations,
initializes hardware settings, and continuously tunes the miner's voltage and frequency based on
performance metrics using a PID control strategy.
"""
import argparse
import logging
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from threading import Thread
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse

# Third-party imports
from rich.console import Console
import json
import os

# Local application imports
from interfaces import (
    IBitaxeAPIClient,
    ILogger,
    IConfigLoader,
    ITerminalUI,
    TuningStrategy,
)
from implementations import (
    BitaxeAPIClient,
    Logger,
    YamlConfigLoader,
    RichTerminalUI,
    NullTerminalUI,
    PIDTuningStrategy,
)
from pools import get_fastest_pools

# Global variables
console = Console()
__version__ = "1.0.3"
latest_metrics: List[Dict[str, Any]] = []


class MetricsHandler(BaseHTTPRequestHandler):
    """
    HTTP request handler for serving metrics data via GET requests.

    Handles requests to the "/metrics" endpoint by returning the latest metrics in JSON format.
    """

    def do_GET(self) -> None:
        """Handle GET requests and serve metrics data."""
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            metrics_json = json.dumps({"endpoints": latest_metrics}).encode("utf-8")
            self.wfile.write(metrics_json)
        else:
            self.send_response(404)
            self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """
    A threaded HTTP server to handle multiple metrics requests concurrently.

    Combines ThreadingMixIn and HTTPServer to process requests in separate threads.
    """


def start_metrics_server() -> None:
    """
    Start a metrics server on port 8093 to serve the latest metrics data.

    The server runs in a separate daemon thread to avoid blocking the main tuning process.
    """
    server = ThreadedHTTPServer(("0.0.0.0", 8093), MetricsHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logging.info("Metrics server started on http://0.0.0.0:8093/metrics")


def parse_stratum_url(url: str) -> Dict[str, Any]:
    """
    Parse a stratum URL into a dictionary containing hostname and port.

    Args:
        url (str): The stratum URL to parse (e.g., "stratum+tcp://pool.example.com:3333").

    Returns:
        Dict[str, Any]: A dictionary with 'hostname' and 'port' keys.

    Raises:
        ValueError: If the URL scheme is not 'stratum+tcp' or if hostname/port is missing.
    """
    parsed = urlparse(url)
    if parsed.scheme != "stratum+tcp":
        raise ValueError(f"Invalid scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Stratum URL must include hostname and port")
    return {"hostname": parsed.hostname, "port": parsed.port}


class TuningManager:
    """
    Manages the auto-tuning process for a Bitaxe miner.

    Responsibilities include initializing hardware, setting up stratum pools, and running a
    continuous tuning loop to optimize performance based on a provided tuning strategy.
    """

    def __init__(
        self,
        tuning_strategy: TuningStrategy,
        api_client: IBitaxeAPIClient,
        logger: ILogger,
        config_loader: IConfigLoader,
        terminal_ui: ITerminalUI,
        sample_interval: float,
        initial_voltage: float,
        initial_frequency: float,
        pools_file: str,
        config: Dict[str, Any],
        user_file: Optional[str] = None,
        primary_stratum: Optional[Dict[str, Any]] = None,
        backup_stratum: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Initialize the TuningManager with necessary components and settings.

        Args:
            tuning_strategy (TuningStrategy): Strategy for adjusting voltage and frequency.
            api_client (IBitaxeAPIClient): Client for interacting with the miner API.
            logger (ILogger): Logger instance for recording metrics and events.
            config_loader (IConfigLoader): Loader for configuration files.
            terminal_ui (ITerminalUI): UI for displaying tuning status.
            sample_interval (float): Time interval between samples in seconds.
            initial_voltage (float): Initial voltage setting in millivolts.
            initial_frequency (float): Initial frequency setting in MHz.
            pools_file (str): Path to the pools YAML file.
            config (Dict[str, Any]): Configuration dictionary.
            user_file (Optional[str]): Path to the user YAML file, if provided.
            primary_stratum (Optional[Dict[str, Any]]): Primary stratum pool info.
            backup_stratum (Optional[Dict[str, Any]]): Backup stratum pool info.
        """
        # Set up core instance variables
        self.tuning_strategy = tuning_strategy
        self.api_client = api_client
        self.logger = logger
        self.config_loader = config_loader
        self.terminal_ui = terminal_ui
        self.sample_interval = sample_interval
        self.running = True
        self.target_voltage = initial_voltage
        self.target_frequency = initial_frequency
        self.pools_file = pools_file
        self.config = config
        self.user_file = user_file
        self.monitoring_samples = int(60 / sample_interval)
        self.sample_count = 0

        # Fetch system information from the miner
        system_info = self.api_client.get_system_info()
        if system_info is None:
            logging.error("Failed to get system info from miner API")
            sys.exit(1)
        self.mac_address = system_info.get("macAddr", "unknown")
        current_stratum_user = system_info.get("stratumUser", "")
        current_fallback_user = system_info.get("fallbackStratumUser", "")

        # Load stratum users if not present in system info and a user file is provided
        self.stratum_users = {}
        if not current_stratum_user and self.user_file:
            self.stratum_users = self._load_stratum_users()

        # Determine stratum pools based on input or configuration
        if primary_stratum:
            stratum_info = [
                primary_stratum,
                backup_stratum if backup_stratum else self._get_backup_pool(),
            ]
        elif "PRIMARY_STRATUM" in self.config and "BACKUP_STRATUM" in self.config:
            stratum_info = self._parse_config_stratums()
        else:
            stratum_info = get_fastest_pools(
                yaml_file=self.pools_file,
                stratum_user=self.stratum_users.get("stratumUser", ""),
                fallback_stratum_user=self.stratum_users.get("fallbackStratumUser", ""),
                user_yaml=self.user_file,
                force_measure=True,
                latency_expiry_minutes=15,
            )
            if len(stratum_info) < 2:
                logging.error("Failed to get at least two valid pools")
                sys.exit(1)

        # Standardize and apply stratum settings
        primary, backup = self._standardize_pools(stratum_info)
        self._apply_stratum_settings(
            primary, backup, current_stratum_user, current_fallback_user
        )

        # Initialize hardware with initial settings
        self._initialize_hardware()

    def _get_backup_pool(self) -> Dict[str, Any]:
        """
        Fetch a backup pool by measuring pool latencies and selecting the fastest.

        Returns:
            Dict[str, Any]: Backup pool information with 'hostname' and 'port'.

        Raises:
            SystemExit: If no valid backup pool is found.
        """
        logging.info("Measuring backup pool latencies...")
        backup_pools = get_fastest_pools(
            yaml_file=self.pools_file,
            stratum_user=self.stratum_users.get("stratumUser", ""),
            fallback_stratum_user=self.stratum_users.get("fallbackStratumUser", ""),
            user_yaml=self.user_file,
            force_measure=True,
            latency_expiry_minutes=15,
        )
        if not backup_pools:
            logging.error("Failed to get a valid backup pool")
            sys.exit(1)
        return backup_pools[0]

    def _parse_config_stratums(self) -> List[Dict[str, Any]]:
        """
        Parse primary and backup stratum URLs from the configuration.

        Returns:
            List[Dict[str, Any]]: List containing primary and backup pool info.

        Raises:
            SystemExit: If stratum URLs are invalid.
        """
        try:
            primary = parse_stratum_url(self.config["PRIMARY_STRATUM"])
            backup = parse_stratum_url(self.config["BACKUP_STRATUM"])
            return [primary, backup]
        except ValueError as e:
            logging.error(f"Invalid stratum URL in config: {e}")
            sys.exit(1)

    def _standardize_pools(
        self, stratum_info: List[Dict[str, Any]]
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Standardize pool information to ensure 'hostname' and 'port' keys are present.

        Args:
            stratum_info (List[Dict[str, Any]]): List of pool info dictionaries.

        Returns:
            tuple[Dict[str, Any], Dict[str, Any]]: Standardized primary and backup pool info.
        """
        for pool in stratum_info:
            if "endpoint" in pool and "hostname" not in pool:
                parsed = parse_stratum_url(pool["endpoint"])
                pool["hostname"] = parsed["hostname"]
                pool["port"] = parsed["port"]
            pool.pop("endpoint", None)
        return stratum_info[0], stratum_info[1]

    def _apply_stratum_settings(
        self,
        primary: Dict[str, Any],
        backup: Dict[str, Any],
        current_stratum_user: str,
        current_fallback_user: str,
    ) -> None:
        """
        Apply stratum settings to the miner, including pool users.

        Args:
            primary (Dict[str, Any]): Primary pool information.
            backup (Dict[str, Any]): Backup pool information.
            current_stratum_user (str): Current stratum user from system info.
            current_fallback_user (str): Current fallback stratum user from system info.

        Raises:
            SystemExit: If stratum users are missing or settings cannot be applied.
        """
        primary["user"] = current_stratum_user or self.stratum_users.get(
            "stratumUser", ""
        )
        backup["user"] = current_fallback_user or self.stratum_users.get(
            "fallbackStratumUser", primary["user"]
        )
        if not primary["user"] or not backup["user"]:
            logging.error(
                f"Stratum users missing: Primary='{primary['user']}', Backup='{backup['user']}'"
            )
            sys.exit(1)
        logging.info(
            f"Setting primary stratum: {primary['hostname']}:{primary['port']} (user: {primary['user']})"
        )
        logging.info(
            f"Setting backup stratum: {backup['hostname']}:{backup['port']} (user: {backup['user']})"
        )
        if not self.api_client.set_stratum(primary, backup):
            logging.error("Failed to set stratum endpoints")
            sys.exit(1)
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.show_banner()
        time.sleep(1)
        self.api_client.restart()

    def _initialize_hardware(self) -> None:
        """Initialize the miner hardware with initial voltage and frequency settings."""
        logging.info(
            f"Initializing hardware: Voltage={self.target_voltage}mV, Frequency={self.target_frequency}MHz"
        )
        self.api_client.set_settings(self.target_voltage, self.target_frequency)

    def _load_stratum_users(self) -> Dict[str, str]:
        """
        Load stratum users from the user YAML file.

        Returns:
            Dict[str, str]: Dictionary with 'stratumUser' and 'fallbackStratumUser' keys.
        """
        try:
            users = self.config_loader.load_config(self.user_file)
            return {
                "stratumUser": users.get("stratumUser", ""),
                "fallbackStratumUser": users.get("fallbackStratumUser", ""),
            }
        except Exception as e:
            logging.warning(f"Failed to load user.yaml: {e}")
            return {}

    def stop_tuning(self) -> None:
        """Stop the tuning process and clean up resources."""
        self.running = False
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.stop()
        print("\nTuning stopped gracefully")

    def start_tuning(self) -> None:
        """
        Start the main tuning loop to continuously monitor and adjust miner settings.

        The loop fetches system info, updates the UI, logs metrics, and applies the tuning strategy.
        """
        global latest_metrics
        try:
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.start()
            logging.info("Starting BitaxePID tuner...")
            while self.running:
                # Fetch current system information
                system_info = self.api_client.get_system_info()
                if not system_info:
                    time.sleep(1)
                    continue

                # Update UI with current metrics
                self.terminal_ui.update(
                    system_info, self.target_voltage, self.target_frequency
                )

                # Compile metrics data
                metrics = {
                    "mac_address": self.mac_address,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "target_frequency": self.target_frequency,
                    "target_voltage": self.target_voltage,
                    "hashrate": system_info.get("hashRate", 0),
                    "temp": system_info.get("temp", 0),
                    "pid_settings": self.config,
                    "power": system_info.get("power", 0),
                    "board_voltage": system_info.get("voltage", 0),
                    "current": system_info.get("current", 0),
                    "core_voltage_actual": system_info.get("coreVoltageActual", 0),
                    "frequency": system_info.get("frequency", 0),
                    "fanrpm": system_info.get("fanrpm", 0),
                }

                # Log metrics to CSV
                self.logger.log_to_csv(**metrics)

                # Update metrics for serving if enabled
                if self.config.get("METRICS_SERVE", False):
                    latest_metrics = [
                        m
                        for m in latest_metrics
                        if m["mac_address"] != self.mac_address
                    ]
                    latest_metrics.append(metrics)

                # Apply tuning strategy to adjust settings
                new_voltage, new_frequency = self.tuning_strategy.apply_strategy(
                    current_voltage=self.target_voltage,
                    current_frequency=self.target_frequency,
                    temp=system_info.get("temp", 0),
                    hashrate=system_info.get("hashRate", 0),
                    power=system_info.get("power", 0),
                )

                # Apply new settings after sufficient samples
                if self.sample_count >= self.monitoring_samples:
                    if (
                        new_voltage != self.target_voltage
                        or new_frequency != self.target_frequency
                    ):
                        self.target_voltage = new_voltage
                        self.target_frequency = new_frequency
                        self.api_client.set_settings(
                            self.target_voltage, self.target_frequency
                        )
                        self.logger.save_snapshot(
                            self.target_voltage, self.target_frequency
                        )
                self.sample_count += 1
                time.sleep(self.sample_interval)
        except KeyboardInterrupt:
            self.stop_tuning()
        except Exception as e:
            logging.error(f"Error in tuning loop: {e}")
            time.sleep(1)
        finally:
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.stop()


def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments for the BitaxePID Auto-Tuner.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--ip", required=True, type=str, help="IP address of the Bitaxe miner"
    )
    parser.add_argument(
        "--config", type=str, help="Path to optional user YAML configuration file"
    )
    parser.add_argument(
        "--user-file", type=str, default=None, help="Path to user YAML file"
    )
    parser.add_argument(
        "--pools-file", type=str, default=None, help="Path to pools YAML file"
    )
    parser.add_argument("--primary-stratum", type=str, help="Primary stratum URL")
    parser.add_argument("--backup-stratum", type=str, help="Backup stratum URL")
    parser.add_argument(
        "--stratum-user", type=str, help="Stratum user for primary pool"
    )
    parser.add_argument(
        "--fallback-stratum-user", type=str, help="Stratum user for backup pool"
    )
    parser.add_argument("--voltage", type=float, help="Initial voltage override (mV)")
    parser.add_argument(
        "--frequency", type=float, help="Initial frequency override (MHz)"
    )
    parser.add_argument(
        "--sample-interval", type=float, help="Sample interval override (seconds)"
    )
    parser.add_argument(
        "--log-to-console", action="store_true", help="Log to console instead of UI"
    )
    parser.add_argument(
        "--logging-level",
        type=str,
        choices=["info", "debug"],
        default="info",
        help="Logging level",
    )
    parser.add_argument(
        "--serve-metrics",
        action="store_true",
        help="Serve metrics via HTTP on port 8093",
    )
    return parser.parse_args()


def load_config(
    config_loader: IConfigLoader, asic_yaml: str, user_config_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Load configuration from ASIC model YAML and optionally merge with user configuration.

    Args:
        config_loader (IConfigLoader): Instance to load configuration files.
        asic_yaml (str): Path to the ASIC model YAML file.
        user_config_path (Optional[str]): Path to the user configuration YAML file.

    Returns:
        Dict[str, Any]: Merged configuration dictionary.

    Raises:
        SystemExit: If the ASIC YAML file does not exist.
    """
    if not os.path.exists(asic_yaml):
        logging.error(f"ASIC model YAML file {asic_yaml} not found")
        sys.exit(1)
    config = config_loader.load_config(asic_yaml)
    if user_config_path and os.path.exists(user_config_path):
        user_config = config_loader.load_config(user_config_path)
        config.update(user_config)
    return config


def validate_config(config: Dict[str, Any]) -> None:
    """
    Validate that the configuration contains all required keys.

    Args:
        config (Dict[str, Any]): Configuration dictionary to validate.

    Raises:
        SystemExit: If any required keys are missing.
    """
    required_keys = [
        "INITIAL_VOLTAGE",
        "INITIAL_FREQUENCY",
        "SAMPLE_INTERVAL",
        "LOG_FILE",
        "SNAPSHOT_FILE",
        "POOLS_FILE",
        "PID_FREQ_KP",
        "PID_FREQ_KI",
        "PID_FREQ_KD",
        "PID_VOLT_KP",
        "PID_VOLT_KI",
        "PID_VOLT_KD",
        "MIN_VOLTAGE",
        "MAX_VOLTAGE",
        "MIN_FREQUENCY",
        "MAX_FREQUENCY",
        "VOLTAGE_STEP",
        "FREQUENCY_STEP",
        "HASHRATE_SETPOINT",
        "TARGET_TEMP",
        "POWER_LIMIT",
    ]
    missing_keys = [key for key in required_keys if key not in config]
    if missing_keys:
        logging.error(f"Missing required config keys: {', '.join(missing_keys)}")
        sys.exit(1)


def main() -> None:
    """
    Main entry point for the BitaxePID Auto-Tuner.

    Sets up the environment, initializes components, and starts the tuning process.
    """
    # Parse command-line arguments
    args = parse_arguments()

    # Configure logging
    handlers = [logging.FileHandler("bitaxepid_monitor.log")]
    if args.log_to_console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.logging_level == "debug" else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )

    # Initialize API client and fetch system info
    api_client = BitaxeAPIClient(ip=args.ip)
    system_info = api_client.get_system_info()
    if system_info is None:
        logging.error("Failed to fetch system info from API")
        api_client.close()
        sys.exit(1)

    # Load configuration based on ASIC model
    asic_model = system_info.get("ASICModel", "default")
    asic_yaml = f"{asic_model}.yaml"
    config_loader = YamlConfigLoader()
    config = load_config(config_loader, asic_yaml, args.config)

    # Override config with command-line arguments if provided
    if args.voltage is not None:
        config["INITIAL_VOLTAGE"] = args.voltage
    if args.frequency is not None:
        config["INITIAL_FREQUENCY"] = args.frequency
    if args.sample_interval is not None:
        config["SAMPLE_INTERVAL"] = args.sample_interval

    # Validate configuration
    validate_config(config)

    # Determine metrics serving status
    serve_metrics = args.serve_metrics or config.get("METRICS_SERVE", False)
    config["METRICS_SERVE"] = serve_metrics

    # Initialize logger and tuning strategy
    logger_instance = Logger(config["LOG_FILE"], config["SNAPSHOT_FILE"])
    tuning_strategy = PIDTuningStrategy(
        kp_freq=config["PID_FREQ_KP"],
        ki_freq=config["PID_FREQ_KI"],
        kd_freq=config["PID_FREQ_KD"],
        kp_volt=config["PID_VOLT_KP"],
        ki_volt=config["PID_VOLT_KI"],
        kd_volt=config["PID_VOLT_KD"],
        min_voltage=config["MIN_VOLTAGE"],
        max_voltage=config["MAX_VOLTAGE"],
        min_frequency=config["MIN_FREQUENCY"],
        max_frequency=config["MAX_FREQUENCY"],
        voltage_step=config["VOLTAGE_STEP"],
        frequency_step=config["FREQUENCY_STEP"],
        setpoint=config["HASHRATE_SETPOINT"],
        sample_interval=config["SAMPLE_INTERVAL"],
        target_temp=config["TARGET_TEMP"],
        power_limit=config["POWER_LIMIT"],
    )

    # Set up terminal UI based on logging preference
    terminal_ui = NullTerminalUI() if args.log_to_console else RichTerminalUI()

    # Parse stratum URLs if provided
    primary_stratum = (
        parse_stratum_url(args.primary_stratum) if args.primary_stratum else None
    )
    if primary_stratum and args.stratum_user:
        primary_stratum["user"] = args.stratum_user
    backup_stratum = (
        parse_stratum_url(args.backup_stratum) if args.backup_stratum else None
    )
    if backup_stratum and args.fallback_stratum_user:
        backup_stratum["user"] = args.fallback_stratum_user

    # Initialize tuning manager
    tuning_manager = TuningManager(
        tuning_strategy=tuning_strategy,
        api_client=api_client,
        logger=logger_instance,
        config_loader=config_loader,
        terminal_ui=terminal_ui,
        sample_interval=config["SAMPLE_INTERVAL"],
        initial_voltage=config["INITIAL_VOLTAGE"],
        initial_frequency=config["INITIAL_FREQUENCY"],
        pools_file=args.pools_file if args.pools_file else config["POOLS_FILE"],
        config=config,
        user_file=args.user_file if args.user_file else config.get("USER_FILE", None),
        primary_stratum=primary_stratum,
        backup_stratum=backup_stratum,
    )

    # Set up signal handlers for graceful shutdown
    def signal_handler(sig: int, frame: Any) -> None:
        """Handle shutdown signals to stop tuning gracefully."""
        logging.info("Shutting down gracefully...")
        tuning_manager.stop_tuning()
        api_client.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start metrics server if enabled
    if serve_metrics:
        start_metrics_server()

    # Begin tuning process
    logging.info("Starting BitaxePID tuner...")
    tuning_manager.start_tuning()


if __name__ == "__main__":
    main()
