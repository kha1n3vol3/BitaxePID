#!/usr/bin/env python3
"""
BitaxePID Auto-Tuner Module

This module provides an automated tuning system for Bitaxe ASIC miners. It interfaces with the miner via an API,
adjusts voltage and frequency settings using a PID strategy, and optimizes stratum pool selection based on latency.
Configuration is loaded from YAML files, with command-line overrides for flexibility. The module supports both
console logging and a rich terminal UI for real-time monitoring, and optionally exposes metrics via an HTTP server
on port 8093 for Prometheus and Grafana dashboards when enabled via --serve-metrics or METRICS_SERVE config.

Usage:
    python bitaxepid.py --ip <miner_ip> [--pools-file pools2.yaml] [--logging-level debug] [--serve-metrics]

Dependencies:
    - requests, rich, pyyaml, typing, http.server, socketserver, threading
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
from interfaces import IBitaxeAPIClient, ILogger, IConfigLoader, ITerminalUI, TuningStrategy
from implementations import BitaxeAPIClient, Logger, YamlConfigLoader, RichTerminalUI, NullTerminalUI, PIDTuningStrategy
from pools import get_fastest_pools
from rich.console import Console
import json
import os

console = Console()
__version__ = "1.0.3"  # add connection pool for reuse to bitaxe.

# Global variable to store the latest metrics for the HTTP server (now a list of dicts)
latest_metrics: List[Dict[str, Any]] = []

class MetricsHandler(BaseHTTPRequestHandler):
    """HTTP handler to serve JSON metrics for Prometheus and Grafana."""

    def do_GET(self) -> None:
        """
        Handle GET requests to the /metrics endpoint.

        Serves the latest metrics as a JSON object with a list of endpoints, otherwise returns a 404.
        """
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            metrics_json = json.dumps({"endpoints": latest_metrics}).encode('utf-8')
            self.wfile.write(metrics_json)
        else:
            self.send_response(404)
            self.end_headers()

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server to handle multiple requests concurrently."""
    pass

def start_metrics_server() -> None:
    """Start the HTTP server on port 8093 in a separate thread."""
    server = ThreadedHTTPServer(('localhost', 8093), MetricsHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logging.info("Metrics server started on http://localhost:8093/metrics")

def parse_stratum_url(url: str) -> Dict[str, Any]:
    """
    Parse a stratum URL into hostname and port components.

    Args:
        url (str): The stratum URL (e.g., "stratum+tcp://solo.ckpool.org:3333").

    Returns:
        Dict[str, Any]: Dictionary with 'hostname' and 'port' keys.

    Raises:
        ValueError: If the URL scheme is invalid or lacks hostname/port.

    Example:
        >>> parse_stratum_url("stratum+tcp://solo.ckpool.org:3333")
        {'hostname': 'solo.ckpool.org', 'port': 3333}
    """
    parsed = urlparse(url)
    if parsed.scheme != "stratum+tcp":
        raise ValueError(f"Invalid scheme: {parsed.scheme}. Expected 'stratum+tcp'")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Stratum URL must include both hostname and port")
    return {"hostname": parsed.hostname, "port": parsed.port}

class TuningManager:
    """Manages the tuning process for a Bitaxe miner, adjusting settings and stratum pools."""

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
        backup_stratum: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Initialize the TuningManager with tuning parameters and miner settings.

        Args:
            tuning_strategy (TuningStrategy): Strategy for adjusting voltage/frequency.
            api_client (IBitaxeAPIClient): API client for miner communication.
            logger (ILogger): Logger for recording tuning data.
            config_loader (IConfigLoader): Loader for YAML configuration files.
            terminal_ui (ITerminalUI): UI for displaying tuning status.
            sample_interval (float): Interval between tuning adjustments (seconds).
            initial_voltage (float): Starting voltage in millivolts.
            initial_frequency (float): Starting frequency in MHz.
            pools_file (str): Path to the pools YAML file.
            config (Dict[str, Any]): Configuration dictionary from YAML.
            user_file (Optional[str]): Path to user YAML file, if provided.
            primary_stratum (Optional[Dict[str, Any]]): Primary stratum settings.
            backup_stratum (Optional[Dict[str, Any]]): Backup stratum settings.
        """
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
        logging.debug(f"User file set to: {self.user_file}")

        system_info = self.api_client.get_system_info()
        if system_info is None:
            logging.error("Failed to get system info from miner API")
            sys.exit(1)
        self.mac_address = system_info.get("macAddr", "unknown")  # Store MAC address

        current_stratum_user = system_info.get("stratumUser", "")
        current_fallback_user = system_info.get("fallbackStratumUser", "")
        logging.debug(f"Current stratum users from API: primary='{current_stratum_user}', backup='{current_fallback_user}'")

        self.stratum_users = {}
        if not current_stratum_user:
            self.stratum_users = self._load_stratum_users()
            logging.debug(f"Loaded stratum users from file: {self.stratum_users}")
        else:
            logging.debug("API system stratum user assumed correct once set; skipping user file load")

        if primary_stratum:
            stratum_info = [primary_stratum, backup_stratum] if backup_stratum else [
                primary_stratum, self._get_backup_pool()]
        elif 'PRIMARY_STRATUM' in self.config and 'BACKUP_STRATUM' in self.config:
            stratum_info = self._parse_config_stratums()
        else:
            logging.debug(f"Measuring pools from {self.pools_file}")  # Fixed typo: self.pools_file
            stratum_info = get_fastest_pools(
                yaml_file=self.pools_file,
                stratum_user=self.stratum_users.get("stratumUser", ""),
                fallback_stratum_user=self.stratum_users.get("fallbackStratumUser", ""),
                user_yaml=self.user_file,
                force_measure=True,
                latency_expiry_minutes=15
            )
            if len(stratum_info) < 2:
                logging.error("Failed to get at least two valid pools")
                sys.exit(1)

        primary, backup = self._standardize_pools(stratum_info)
        self._apply_stratum_settings(primary, backup, current_stratum_user, current_fallback_user)
        self._initialize_hardware()

    def _get_backup_pool(self) -> Dict[str, Any]:
        """Fetch a backup pool via latency testing if not provided."""
        logging.info("Measuring backup pool latencies...")
        backup_pools = get_fastest_pools(
            yaml_file=self.pools_file,
            stratum_user=self.stratum_users.get("stratumUser", ""),
            fallback_stratum_user=self.stratum_users.get("fallbackStratumUser", ""),
            user_yaml=self.user_file,
            force_measure=True,
            latency_expiry_minutes=15
        )
        if not backup_pools:
            logging.error("Failed to get a valid backup pool")
            sys.exit(1)
        return backup_pools[0]

    def _parse_config_stratums(self) -> List[Dict[str, Any]]:
        """Parse stratum URLs from config."""
        try:
            primary = parse_stratum_url(self.config['PRIMARY_STRATUM'])
            backup = parse_stratum_url(self.config['BACKUP_STRATUM'])
            return [primary, backup]
        except ValueError as e:
            logging.error(f"Invalid stratum URL in config: {e}")
            sys.exit(1)

    def _standardize_pools(self, stratum_info: List[Dict[str, Any]]) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Standardize pool dictionaries and return primary/backup."""
        for pool in stratum_info:
            if 'endpoint' in pool and 'hostname' not in pool:
                parsed = parse_stratum_url(pool['endpoint'])
                pool['hostname'] = parsed['hostname']
                pool['port'] = parsed['port']
            if 'hostname' not in pool or 'port' not in pool:
                logging.error("Pool missing 'hostname' or 'port'")
                sys.exit(1)
            pool.pop('endpoint', None)
        return stratum_info[0], stratum_info[1]

    def _apply_stratum_settings(
        self,
        primary: Dict[str, Any],
        backup: Dict[str, Any],
        current_stratum_user: str,
        current_fallback_user: str
    ) -> None:
        """Apply stratum settings to the miner."""
        primary["user"] = current_stratum_user or self.stratum_users.get("stratumUser", "")
        backup["user"] = current_fallback_user or self.stratum_users.get("fallbackStratumUser", primary["user"])
        if not primary["user"] or not backup["user"]:
            logging.error(f"Stratum users missing: Primary='{primary['user']}', Backup='{backup['user']}'")
            sys.exit(1)

        logging.info(f"Setting primary stratum: {primary['hostname']}:{primary['port']} (user: {primary['user']})")
        logging.info(f"Setting backup stratum: {backup['hostname']}:{backup['port']} (user: {backup['user']})")
        if not self.api_client.set_stratum(primary, backup):
            logging.error("Failed to set stratum endpoints")
            sys.exit(1)
        logging.info("Stratum set, restarting miner...")
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.show_banner()
        time.sleep(1)
        self.api_client.restart()

    def _initialize_hardware(self) -> None:
        """Initialize miner hardware settings."""
        logging.info(f"Initializing hardware: Voltage={self.target_voltage}mV, Frequency={self.target_frequency}MHz")
        self.api_client.set_settings(self.target_voltage, self.target_frequency)

    def _load_stratum_users(self) -> Dict[str, str]:
        """
        Load stratum users from user.yaml if available.

        Returns:
            Dict[str, str]: Dictionary with 'stratumUser' and 'fallbackStratumUser'.
        """
        if not self.user_file:
            return {}
        try:
            users = self.config_loader.load_config(self.user_file)
            return {
                "stratumUser": users.get("stratumUser", ""),
                "fallbackStratumUser": users.get("fallbackStratumUser", "")
            }
        except Exception as e:
            logging.warning(f"Failed to load user.yaml: {e}")
            return {}

    def stop_tuning(self) -> None:
        """Stop the tuning process gracefully."""
        self.running = False
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.stop()
        print("\nTuning stopped gracefully")

    def start_tuning(self) -> None:
        """Start the tuning process, adjusting settings based on system info and exposing metrics if enabled."""
        global latest_metrics
        try:
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.start()
            logging.info("Starting BitaxePID tuner...")
            while self.running:
                system_info = self.api_client.get_system_info()
                if not system_info:
                    time.sleep(1)
                    continue

                self.terminal_ui.update(system_info, self.target_voltage, self.target_frequency)
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
                    "fanrpm": system_info.get("fanrpm", 0)
                }
                self.logger.log_to_csv(**metrics)
                if self.config.get("METRICS_SERVE", False):
                    # Replace existing entry for this MAC or append if new
                    latest_metrics = [m for m in latest_metrics if m["mac_address"] != self.mac_address]
                    latest_metrics.append(metrics)

                new_voltage, new_frequency = self.tuning_strategy.apply_strategy(
                    current_voltage=self.target_voltage,
                    current_frequency=self.target_frequency,
                    temp=system_info.get("temp", 0),
                    hashrate=system_info.get("hashRate", 0),
                    power=system_info.get("power", 0)
                )

                if new_voltage != self.target_voltage or new_frequency != self.target_frequency:
                    self.target_voltage = new_voltage
                    self.target_frequency = new_frequency
                    self.api_client.set_settings(self.target_voltage, self.target_frequency)
                    self.logger.save_snapshot(self.target_voltage, self.target_frequency)

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
    Parse command-line arguments for the BitaxePID tuner.

    Returns:
        argparse.Namespace: Parsed arguments with command-line options.

    Example:
        >>> args = parse_arguments()  # Run with: python bitaxepid.py --ip 192.168.1.1 --serve-metrics
        >>> args.ip
        '192.168.1.1'
        >>> args.serve_metrics
        True
    """
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument("--ip", required=True, type=str, help="IP address of the Bitaxe miner")
    parser.add_argument("--config", type=str, help="Path to optional user YAML configuration file")
    parser.add_argument("--user-file", type=str, default=None, help="Path to user YAML file (default: from config)")
    parser.add_argument("--pools-file", type=str, default=None, help="Path to pools YAML file (default: from config)")
    parser.add_argument("--primary-stratum", type=str, help="Primary stratum URL (e.g., stratum+tcp://host:port)")
    parser.add_argument("--backup-stratum", type=str, help="Backup stratum URL (e.g., stratum+tcp://host:port)")
    parser.add_argument("--stratum-user", type=str, help="Stratum user for primary pool")
    parser.add_argument("--fallback-stratum-user", type=str, help="Stratum user for backup pool")
    parser.add_argument("--voltage", type=float, help="Initial voltage override (mV)")
    parser.add_argument("--frequency", type=float, help="Initial frequency override (MHz)")
    parser.add_argument("--sample-interval", type=float, help="Sample interval override (seconds)")
    parser.add_argument("--log-to-console", action="store_true", help="Log to console instead of UI")
    parser.add_argument("--logging-level", type=str, choices=["info", "debug"], default="info", help="Logging level")
    parser.add_argument("--serve-metrics", action="store_true", help="Serve metrics via HTTP on port 8093 (default: False)")
    return parser.parse_args()

def load_config(config_loader: IConfigLoader, asic_yaml: str, user_config_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load and merge configurations from ASIC model YAML and optional user config.

    Args:
        config_loader (IConfigLoader): Loader for YAML files.
        asic_yaml (str): Path to ASIC model YAML file.
        user_config_path (Optional[str]): Path to optional user config YAML.

    Returns:
        Dict[str, Any]: Merged configuration dictionary.
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
    Validate that required configuration keys are present.

    Args:
        config (Dict[str, Any]): Configuration dictionary to validate.

    Raises:
        SystemExit: If required keys are missing.
    """
    required_keys = [
        "INITIAL_VOLTAGE", "INITIAL_FREQUENCY", "SAMPLE_INTERVAL", "LOG_FILE", "SNAPSHOT_FILE",
        "POOLS_FILE", "PID_FREQ_KP", "PID_FREQ_KI", "PID_FREQ_KD", "PID_VOLT_KP", "PID_VOLT_KI",
        "PID_VOLT_KD", "MIN_VOLTAGE", "MAX_VOLTAGE", "MIN_FREQUENCY", "MAX_FREQUENCY",
        "VOLTAGE_STEP", "FREQUENCY_STEP", "HASHRATE_SETPOINT", "TARGET_TEMP", "POWER_LIMIT"
    ]
    missing_keys = [key for key in required_keys if key not in config]
    if missing_keys:
        logging.error(f"Missing required config keys: {', '.join(missing_keys)}")
        sys.exit(1)

def main() -> None:
    args = parse_arguments()
    handlers = [logging.FileHandler("bitaxepid_monitor.log")]
    if args.log_to_console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if args.logging_level == "debug" else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers
    )

    # Initialize the API client with enhanced settings
    api_client = BitaxeAPIClient(
        ip=args.ip,
        timeout=10,  # Longer timeout to avoid ConnectTimeoutError
        retries=5,   # More retries for resilience
        pool_maxsize=10  # Connection pooling
    )
    
    system_info = api_client.get_system_info()
    if system_info is None:
        logging.error("Failed to fetch system info from API")
        api_client.close()
        sys.exit(1)

    asic_model = system_info.get("ASICModel", "default")
    asic_yaml = f"{asic_model}.yaml"
    config_loader = YamlConfigLoader()
    config = load_config(config_loader, asic_yaml, args.config)

    # Apply overrides (unchanged)
    if args.voltage is not None:
        config["INITIAL_VOLTAGE"] = args.voltage
    if args.frequency is not None:
        config["INITIAL_FREQUENCY"] = args.frequency
    if args.sample_interval is not None:
        config["SAMPLE_INTERVAL"] = args.sample_interval
    validate_config(config)

    serve_metrics = args.serve_metrics or config.get("METRICS_SERVE", False)
    config["METRICS_SERVE"] = serve_metrics

    logger_instance = Logger(config["LOG_FILE"], config["SNAPSHOT_FILE"])
    tuning_strategy = PIDTuningStrategy(
        kp_freq=config["PID_FREQ_KP"], ki_freq=config["PID_FREQ_KI"], kd_freq=config["PID_FREQ_KD"],
        kp_volt=config["PID_VOLT_KP"], ki_volt=config["PID_VOLT_KI"], kd_volt=config["PID_VOLT_KD"],
        min_voltage=config["MIN_VOLTAGE"], max_voltage=config["MAX_VOLTAGE"],
        min_frequency=config["MIN_FREQUENCY"], max_frequency=config["MAX_FREQUENCY"],
        voltage_step=config["VOLTAGE_STEP"], frequency_step=config["FREQUENCY_STEP"],
        setpoint=config["HASHRATE_SETPOINT"], sample_interval=config["SAMPLE_INTERVAL"],
        target_temp=config["TARGET_TEMP"], power_limit=config["POWER_LIMIT"]
    )
    terminal_ui = NullTerminalUI() if args.log_to_console else RichTerminalUI()

    primary_stratum = parse_stratum_url(args.primary_stratum) if args.primary_stratum else None
    if primary_stratum and args.stratum_user:
        primary_stratum["user"] = args.stratum_user
    backup_stratum = parse_stratum_url(args.backup_stratum) if args.backup_stratum else None
    if backup_stratum and args.fallback_stratum_user:
        backup_stratum["user"] = args.fallback_stratum_user

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
        backup_stratum=backup_stratum
    )

    def signal_handler(sig: int, frame: Any) -> None:
        logging.info("Shutting down gracefully...")
        tuning_manager.stop_tuning()
        api_client.close()  # Clean up the connection pool
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if serve_metrics:
        start_metrics_server()
    logging.info("Starting BitaxePID tuner...")
    tuning_manager.start_tuning()

if __name__ == "__main__":
    main()