#!/usr/bin/env python3
"""
BitaxePID Auto-Tuner Module

This module provides an auto-tuning system for Bitaxe miners, managing stratum
pools, initializing hardware, and tuning voltage/frequency using a dual PID strategy.
"""

import argparse
import logging
import signal
import statistics
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from threading import Thread
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse

from rich.console import Console
import json
import os

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

console = Console()
__version__ = "1.0.3"
latest_metrics: List[Dict[str, Any]] = []


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
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
    pass


def start_metrics_server() -> None:
    server = ThreadedHTTPServer(("0.0.0.0", 8093), MetricsHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    logging.info("Metrics server started on http://0.0.0.0:8093/metrics")


def parse_stratum_url(url: str) -> Dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "stratum+tcp":
        raise ValueError(f"Invalid scheme: {parsed.scheme}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("Stratum URL must include hostname and port")
    return {"hostname": parsed.hostname, "port": parsed.port}


class TuningManager:
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
        Initialize the TuningManager with components and settings.

        Args:
            tuning_strategy (TuningStrategy): Strategy for tuning voltage/frequency.
            api_client (IBitaxeAPIClient): Client for miner API interaction.
            logger (ILogger): Logger for metrics and events.
            config_loader (IConfigLoader): Loader for config files.
            terminal_ui (ITerminalUI): UI for displaying status.
            sample_interval (float): Sampling interval (seconds).
            initial_voltage (float): Initial voltage (mV).
            initial_frequency (float): Initial frequency (MHz).
            pools_file (str): Path to pools YAML file.
            config (Dict[str, Any]): Configuration dictionary.
            user_file (Optional[str]): Path to user YAML file.
            primary_stratum (Optional[Dict]): Primary stratum pool info.
            backup_stratum (Optional[Dict]): Backup stratum pool info.
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
        self.monitoring_samples = int(60 / sample_interval)  # 12 samples at 5s interval
        self.sample_count = 0
        self.temp_buffer: List[float] = []
        self.power_buffer: List[float] = []

        system_info = self.api_client.get_system_info()
        if system_info is None:
            logging.error("Failed to get system info from miner API")
            sys.exit(1)
        self.mac_address = system_info.get("macAddr", "unknown")
        current_stratum_user = system_info.get("stratumUser", "")
        current_fallback_user = system_info.get("fallbackStratumUser", "")

        self.stratum_users = {}
        if not current_stratum_user and self.user_file:
            self.stratum_users = self._load_stratum_users()

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

        primary, backup = self._standardize_pools(stratum_info)
        self._apply_stratum_settings(
            primary, backup, current_stratum_user, current_fallback_user
        )
        self._initialize_hardware()

    def _get_backup_pool(self) -> Dict[str, Any]:
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
        logging.info(
            f"Initializing hardware: Voltage={self.target_voltage}mV, Frequency={self.target_frequency}MHz"
        )
        self.api_client.set_settings(self.target_voltage, self.target_frequency)

    def _load_stratum_users(self) -> Dict[str, str]:
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
        self.running = False
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.stop()
        print("\nTuning stopped gracefully")

    def start_tuning(self) -> None:
        """
        Start the tuning loop: collect data every 5s, adjust settings every 60s.

        Collects temperature and power every 5 seconds, calculates medians every
        60 seconds (12 samples), and applies PID adjustments based on medians.
        """
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

                # Collect instantaneous data every 5 seconds
                temp = system_info.get("temp", 0)
                power = system_info.get("power", 0)
                self.temp_buffer.append(temp)
                self.power_buffer.append(power)
                if len(self.temp_buffer) > 12:
                    self.temp_buffer.pop(0)
                    self.power_buffer.pop(0)

                self.terminal_ui.update(
                    system_info, self.target_voltage, self.target_frequency
                )

                # Base metrics for logging every 5 seconds
                metrics = {
                    "mac_address": self.mac_address,
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "target_frequency": self.target_frequency,
                    "target_voltage": self.target_voltage,
                    "hashrate": system_info.get("hashRate", 0),
                    "temp": temp,
                    "pid_settings": self.config,
                    "power": power,
                    "board_voltage": system_info.get("voltage", 0),
                    "current": system_info.get("current", 0),
                    "core_voltage_actual": system_info.get("coreVoltageActual", 0),
                    "frequency": system_info.get("frequency", 0),
                    "fanrpm": system_info.get("fanrpm", 0),
                }

                # Every 60 seconds (12 samples), calculate medians and adjust
                if self.sample_count % 12 == 0 and len(self.temp_buffer) == 12:
                    median_temp = statistics.median(self.temp_buffer)
                    median_power = statistics.median(self.power_buffer)
                    new_voltage, new_frequency, pid_freq_terms, pid_volt_terms = (
                        self.tuning_strategy.apply_strategy(
                            self.target_voltage,
                            self.target_frequency,
                            median_temp,
                            median_power,
                        )
                    )
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
                    self.logger.log_to_csv(
                        **metrics,
                        median_temp=median_temp,
                        median_power=median_power,
                        recommended_voltage=new_voltage,
                        recommended_frequency=new_frequency,
                        pid_freq_terms=pid_freq_terms,
                        pid_volt_terms=pid_volt_terms,
                    )
                else:
                    self.logger.log_to_csv(**metrics)

                if self.config.get("METRICS_SERVE", False):
                    latest_metrics = [
                        m
                        for m in latest_metrics
                        if m["mac_address"] != self.mac_address
                    ]
                    latest_metrics.append(metrics)

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
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--ip", required=True, type=str, help="IP address of the Bitaxe miner"
    )
    parser.add_argument("--config", type=str, help="Path to optional user YAML config")
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
        "--serve-metrics", action="store_true", help="Serve metrics on port 8093"
    )
    return parser.parse_args()


def load_config(
    config_loader: IConfigLoader, asic_yaml: str, user_config_path: Optional[str] = None
) -> Dict[str, Any]:
    if not os.path.exists(asic_yaml):
        logging.error(f"ASIC model YAML file {asic_yaml} not found")
        sys.exit(1)
    config = config_loader.load_config(asic_yaml)
    if user_config_path and os.path.exists(user_config_path):
        user_config = config_loader.load_config(user_config_path)
        config.update(user_config)
    return config


def validate_config(config: Dict[str, Any]) -> None:
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
        "TARGET_TEMP",
        "POWER_LIMIT",
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
        handlers=handlers,
    )

    api_client = BitaxeAPIClient(ip=args.ip)
    system_info = api_client.get_system_info()
    if system_info is None:
        logging.error("Failed to fetch system info from API")
        api_client.close()
        sys.exit(1)

    asic_model = system_info.get("ASICModel", "default")
    asic_yaml = f"{asic_model}.yaml"
    config_loader = YamlConfigLoader()
    config = load_config(config_loader, asic_yaml, args.config)

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
        target_temp=config["TARGET_TEMP"],
        power_limit=config["POWER_LIMIT"],
    )

    terminal_ui = NullTerminalUI() if args.log_to_console else RichTerminalUI()
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

    def signal_handler(sig: int, frame: Any) -> None:
        logging.info("Shutting down gracefully...")
        tuning_manager.stop_tuning()
        api_client.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if serve_metrics:
        start_metrics_server()

    logging.info("Starting BitaxePID tuner...")
    tuning_manager.start_tuning()


if __name__ == "__main__":
    main()
