#!/usr/bin/env python3
"""
BitaxePID Auto-Tuner (Refactored)

An automated tuning system for the Bitaxe 601 Gamma Bitcoin and related miners, refactored to follow clean architecture principles.
Optimizes hashrate while respecting temperature and power constraints, with PID-based or temp-watch tuning modes.
Features a TUI, CSV logging, and JSON snapshots.

Usage:
    python bitaxepid.py 192.168.68.111 [options]
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from typing import Dict, Optional, Any, Tuple, Protocol
import pyfiglet
import requests
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box
from simple_pid import PID
import yaml

# Color Constants for Cyberdeck Theme
BACKGROUND = "#121212"          # Dark background
TEXT_COLOR = "#E0E0E0"          # Light text
PRIMARY_ACCENT = "#39FF14"      # Neon green for primary highlights
SECONDARY_ACCENT = "#00BFFF"    # Bright blue for secondary highlights
WARNING_COLOR = "#FF9933"       # Orange for warnings
ERROR_COLOR = "#FF0000"         # Red for errors
DECORATIVE_COLOR = "#FF0099"    # Pink for decorative elements
TABLE_HEADER = DECORATIVE_COLOR # Header color for tables
TABLE_ROW_EVEN = "#222222"      # Dark gray for even table rows
TABLE_ROW_ODD = "#444444"       # Mid gray for odd table rows
PROGRESS_BAR_BG = "#333333"     # Gray for progress bar background

# Default values (fallback if no config file is provided)
DEFAULTS = {
    "INITIAL_VOLTAGE": 1200,
    "INITIAL_FREQUENCY": 500,
    "MIN_VOLTAGE": 1100,
    "MAX_VOLTAGE": 1300,
    "MIN_FREQUENCY": 400,
    "MAX_FREQUENCY": 550,
    "FREQUENCY_STEP": 25,
    "VOLTAGE_STEP": 10,
    "TARGET_TEMP": 45.0,
    "SAMPLE_INTERVAL": 5,
    "POWER_LIMIT": 15.0,
    "HASHRATE_SETPOINT": 500,
    # PID constants tuned to reduce integral windup and improve stability
    "PID_FREQ_KP": 0.2,    # Increased proportional gain for frequency to enhance responsiveness to current hashrate errors
    "PID_FREQ_KI": 0.01,   # Reduced integral gain to slow integral accumulation and prevent windup
    "PID_FREQ_KD": 0.02,   # Derivative gain unchanged, suitable for damping with typical noise levels
    "PID_VOLT_KP": 0.1,    # Proportional gain for voltage, unchanged as voltage adjustments are secondary
    "PID_VOLT_KI": 0.01,   # Reduced integral gain to minimize windup in voltage control
    "PID_VOLT_KD": 0.02,   # Derivative gain unchanged, maintains stability
    "LOG_FILE": "bitaxepid_tuning_log.csv",
    "SNAPSHOT_FILE": "bitaxpide_snapshot.json"
}

SNAPSHOT_FILE = "bitaxepid_snapshot.json"
LOG_FILE = "bitaxepid_tuning_log.csv"

console = Console()
logger = logging.getLogger(__name__)

class ArgumentParser:
    def __init__(self):
        self.parser = argparse.ArgumentParser(description="My Application")

    def setup_basic_arguments(self):
        """Define essential command-line arguments."""
        self.parser.add_argument("input", type=str, help="Input file path")
        self.parser.add_argument("--verbose", action="store_true", help="Enable verbose output")

    def setup_advanced_arguments(self):
        """Define optional or advanced arguments."""
        advanced = self.parser.add_argument_group("Advanced Options")
        advanced.add_argument("--timeout", type=int, default=30, help="Timeout in seconds")

    def parse(self):
        """Set up all arguments and parse them."""
        self.setup_basic_arguments()
        self.setup_advanced_arguments()
        self.setup_logging_arguments()
        return self.parser.parse_args()



# Domain Layer Protocols and Implementations
class TuningStrategy(Protocol):
    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        """Adjust voltage and frequency based on current metrics."""
        pass

class PIDTuningStrategy:
    def __init__(self, kp_freq: float, ki_freq: float, kd_freq: float, kp_volt: float, ki_volt: float, kd_volt: float,
                 min_voltage: float, max_voltage: float, min_frequency: float, max_frequency: float,
                 voltage_step: float, frequency_step: float, setpoint: float, sample_interval: float,
                 target_temp: float, power_limit: float):
        self.pid_freq = PID(kp_freq, ki_freq, kd_freq, setpoint=setpoint, sample_time=sample_interval)
        self.pid_freq.output_limits = (min_frequency, max_frequency)
        self.pid_volt = PID(kp_volt, ki_volt, kd_volt, setpoint=setpoint, sample_time=sample_interval)
        self.pid_volt.output_limits = (min_voltage, max_voltage)
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.voltage_step = voltage_step
        self.frequency_step = frequency_step
        self.target_temp = target_temp
        self.power_limit = power_limit
        self.last_hashrate = None
        self.stagnation_count = 0
        self.drop_count = 0

    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        # Calculate PID outputs and constrain them
        freq_output = self.pid_freq(hashrate)
        volt_output = self.pid_volt(hashrate)
        proposed_frequency = round(freq_output / self.frequency_step) * self.frequency_step
        proposed_frequency = max(self.min_frequency, min(self.max_frequency, proposed_frequency))
        proposed_voltage = round(volt_output / self.voltage_step) * self.voltage_step
        proposed_voltage = max(self.min_voltage, min(self.max_voltage, proposed_voltage))

        # Track hashrate trends
        hashrate_dropped = self.last_hashrate is not None and hashrate < self.last_hashrate
        stagnated = self.last_hashrate == hashrate
        if hashrate_dropped:
            self.drop_count += 1
        else:
            self.drop_count = 0
        if stagnated:
            self.stagnation_count += 1
        else:
            self.stagnation_count = 0

        new_voltage = current_voltage
        new_frequency = current_frequency
        asic_model = "BM1366"

        # Diagnostic logging for temp check
        logger.debug(f"Checking temp: {temp}°C vs target {self.target_temp}°C, current_freq={current_frequency}MHz")

        # Strictly enforce temperature and power limits
        if temp > self.target_temp:
            if current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                logger.info(f"Reducing frequency to {new_frequency}MHz due to temperature exceeding target ({temp}°C > {self.target_temp}°C) | ASIC Model={asic_model}, Power={power}W, Hashrate={hashrate}GH/s")
                self.last_hashrate = hashrate
                return new_voltage, new_frequency
            elif current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                logger.info(f"Reducing voltage to {new_voltage}mV due to temperature exceeding target and frequency at minimum | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W, Hashrate={hashrate}GH/s")
                self.last_hashrate = hashrate
                return new_voltage, new_frequency
            else:
                logger.info(f"Cannot reduce further due to temperature exceeding target ({temp}°C > {self.target_temp}°C) - at min frequency and voltage | ASIC Model={asic_model}, Power={power}W, Hashrate={hashrate}GH/s")
                self.last_hashrate = hashrate
                return new_voltage, new_frequency
        elif power > self.power_limit * 1.075:
            if current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                logger.info(f"Reducing voltage to {new_voltage}mV due to power exceeding limit ({power}W > {self.power_limit * 1.075}W) | ASIC Model={asic_model}, Temp={temp}°C, Hashrate={hashrate}GH/s")
                self.last_hashrate = hashrate
                return new_voltage, new_frequency
            else:
                logger.info(f"Cannot reduce further due to power exceeding limit ({power}W > {self.power_limit * 1.075}W) - at min voltage | ASIC Model={asic_model}, Temp={temp}°C, Hashrate={hashrate}GH/s")
                self.last_hashrate = hashrate
                return new_voltage, new_frequency

        # Proceed to hashrate optimization only if temp and power are within limits
        if hashrate < self.pid_freq.setpoint:
            if self.drop_count >= 30 and current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                logger.info(f"Reducing frequency to {new_frequency}MHz due to multiple hashrate drops (drop_count={self.drop_count}) | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W, Hashrate={hashrate}GH/s")
            else:
                if hashrate < 0.85 * self.pid_freq.setpoint and current_voltage < self.max_voltage:
                    new_voltage = min(proposed_voltage, current_voltage + self.voltage_step)
                    logger.info(f"Increasing voltage to {new_voltage}mV due to severe hashrate drop ({hashrate} < {0.85 * self.pid_freq.setpoint}) | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W")
                new_frequency = proposed_frequency
                logger.info(f"Adjusting frequency to {new_frequency}MHz based on PID output | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W, Hashrate={hashrate}GH/s")
                if current_frequency >= self.max_frequency and current_voltage < self.max_voltage:
                    new_voltage = current_voltage + self.voltage_step
                    logger.info(f"Increasing voltage to {new_voltage}mV as frequency is at maximum | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W, Hashrate={hashrate}GH/s")
        else:
            logger.info(f"System stable, maintaining settings: Voltage={current_voltage}mV, Frequency={new_frequency}MHz | ASIC Model={asic_model}, Temp={temp}°C, Power={power}W, Hashrate={hashrate}GH/s")

        self.last_hashrate = hashrate
        return new_voltage, new_frequency


class TempWatchTuningStrategy:
    def __init__(self, min_voltage: float, min_frequency: float, voltage_step: float, frequency_step: float, target_temp: float):
        self.min_voltage = min_voltage
        self.min_frequency = min_frequency
        self.voltage_step = voltage_step
        self.frequency_step = frequency_step
        self.target_temp = target_temp

    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        if temp > self.target_temp:
            if current_frequency > self.min_frequency:
                return current_voltage, current_frequency - self.frequency_step
            elif current_voltage > self.min_voltage:
                return current_voltage - self.voltage_step, current_frequency
        return current_voltage, current_frequency

# Infrastructure Layer Protocols and Implementations
class HardwareGateway(Protocol):
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        pass

    def set_settings(self, voltage: float, frequency: float) -> float:
        pass

class BitaxeAPI:
    def __init__(self, bitaxepid_ip: str, min_voltage: float, max_voltage: float, min_frequency: float, max_frequency: float, frequency_step: float):
        self.bitaxepid_url = f"http://{bitaxepid_ip}"
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.frequency_step = frequency_step

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(f"{self.bitaxepid_url}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching system info: {e}")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
        frequency = round(frequency / self.frequency_step) * self.frequency_step
        frequency = max(self.min_frequency, min(self.max_frequency, frequency))
        voltage = max(self.min_voltage, min(self.max_voltage, voltage))
        settings = {"coreVoltage": voltage, "frequency": frequency}
        try:
            response = requests.patch(f"{self.bitaxepid_url}/api/system", json=settings, timeout=10)
            response.raise_for_status()
            logger.info(f"Applied settings: Voltage = {voltage}mV, Frequency = {frequency}MHz")
            time.sleep(2)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting system settings: {e}")
        return frequency

class Logger(Protocol):
    def log(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float) -> None:
        pass

class CSVLogger:
    def __init__(self, log_file: str):
        self.log_file = log_file

    def log(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float) -> None:
        file_exists = os.path.isfile(self.log_file)
        with open(self.log_file, 'a', newline='') as csvfile:
            fieldnames = ["timestamp", "frequency_mhz", "voltage_mv", "hashrate_ghs", "temperature_c"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": timestamp,
                "frequency_mhz": frequency,
                "voltage_mv": voltage,
                "hashrate_ghs": hashrate,
                "temperature_c": temp
            })

class SnapshotManager(Protocol):
    def load(self) -> Tuple[float, float]:
        pass

    def save(self, voltage: float, frequency: float) -> None:
        pass

class JsonSnapshotManager:
    def __init__(self, snapshot_file: str, default_voltage: float, default_frequency: float):
        self.snapshot_file = snapshot_file
        self.default_voltage = default_voltage
        self.default_frequency = default_frequency

    def load(self) -> Tuple[float, float]:
        if os.path.exists(self.snapshot_file):
            try:
                with open(self.snapshot_file, 'r') as f:
                    snapshot = json.load(f)
                    voltage = float(snapshot.get("voltage", self.default_voltage))
                    frequency = float(snapshot.get("frequency", self.default_frequency))
                    return voltage, frequency
            except Exception as e:
                logger.error(f"Failed to load snapshot: {e}")
        return self.default_voltage, self.default_frequency

    def save(self, voltage: float, frequency: float) -> None:
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

# Presentation Layer Protocols and Implementations
class Presenter(Protocol):
    def update(self, info: Dict[str, Any], hashrate: float) -> None:
        pass

class TUIPresenter:
    def __init__(self):
        self.log_messages = []
        self.layout = self.create_layout()
        self.live = Live(self.layout, console=console, refresh_per_second=1)
        self.live.start()

    def create_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(Layout(name="top", size=15), Layout(name="bottom", ratio=1))
        layout["top"].split_row(Layout(name="hashrate", ratio=3), Layout(name="header", ratio=2))
        layout["bottom"].split_column(Layout(name="main", ratio=1), Layout(name="log", size=10))
        layout["main"].split_row(Layout(name="stats", ratio=1), Layout(name="progress", ratio=2))
        return layout

    def update(self, info: Dict[str, Any], hashrate: float) -> None:
        temp = info.get("temp", "N/A")
        power = info.get("power", 0)
        hostname = info.get("hostname", "Unknown")
        frequency = info.get("frequency", 0)
        core_voltage = info.get("coreVoltageActual", 0)
        voltage = info.get("voltage", 0)

        # Hashrate Panel
        hashrate_str = f"{hashrate:.0f} GH/s"
        ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
        hashrate_text = Text(ascii_art, style=f"{PRIMARY_ACCENT} on {BACKGROUND}")
        self.layout["hashrate"].update(Panel(hashrate_text, title="Hashrate", border_style=PRIMARY_ACCENT, style=f"on {BACKGROUND}"))

        # Header with Power Info as a Table
        header_table = Table(box=box.SIMPLE, style=DECORATIVE_COLOR, title=f"BitaxePID Auto-Tuner (Host: {hostname})")
        header_table.add_column("Parameter", style=f"bold {DECORATIVE_COLOR}", justify="right")
        header_table.add_column("Value", style=f"bold {TEXT_COLOR}")
        power_str = f"{power:.2f}W" + (f" [{WARNING_COLOR}](OVER LIMIT)[/]" if power > DEFAULTS["POWER_LIMIT"] else "")
        header_table.add_row("Power", power_str)
        header_table.add_row("Current", f"{info.get('current', 0):.2f}mA")
        header_table.add_row("Core Voltage", f"{core_voltage:.2f}mV")
        header_table.add_row("Voltage", f"{voltage:.0f}mV")
        self.layout["header"].update(Panel(header_table, style=f"on {BACKGROUND}", border_style=PRIMARY_ACCENT))

        # Stats Table
        stats_table = Table(box=box.SIMPLE, style=TEXT_COLOR)
        stats_table.add_column("Parameter", style=f"bold {TABLE_HEADER}")
        stats_table.add_column("Value", style=f"bold {TEXT_COLOR}")
        stats = {
            "ASIC Model": info.get("ASICModel", "N/A"),
            "Best Diff": info.get("bestDiff", "N/A"),
            "Fan RPM": f"{info.get('fanrpm', 0):.0f}",
            "Frequency": f"{frequency:.2f}MHz",
            "Hashrate": f"{hashrate:.2f} GH/s",
            "Temperature": f"{temp if temp == 'N/A' else float(temp):.2f}°C"
        }
        for i, (param, value) in enumerate(sorted(stats.items())):
            row_style = TABLE_ROW_EVEN if i % 2 == 0 else TABLE_ROW_ODD
            stats_table.add_row(param, str(value), style=f"on {row_style}")
        self.layout["stats"].update(Panel(stats_table, title="System Stats", border_style=PRIMARY_ACCENT, style=f"on {BACKGROUND}"))

        # Progress Bars
        progress = Progress(TextColumn("{task.description}", style=TEXT_COLOR),
                            BarColumn(bar_width=40, complete_style=PRIMARY_ACCENT, style=PROGRESS_BAR_BG),
                            TextColumn("{task.percentage:>3.0f}%", style=TEXT_COLOR))
        progress.add_task(f"Hashrate: {hashrate:.2f}", total=DEFAULTS["HASHRATE_SETPOINT"], completed=hashrate)
        progress.add_task(f"Voltage: {voltage:.2f}", total=DEFAULTS["MAX_VOLTAGE"], completed=voltage)
        progress.add_task(f"Frequency: {frequency:.2f}", total=DEFAULTS["MAX_FREQUENCY"], completed=frequency)
        self.layout["progress"].update(Panel(progress, title="Performance", border_style=PRIMARY_ACCENT, style=f"on {BACKGROUND}"))

        # Log Panel
        status = f"Temp: {temp}°C | Hashrate: {hashrate:.2f} GH/s | Power: {power}W"
        self.log_messages.append(status)
        if len(self.log_messages) > 8:
            self.log_messages.pop(0)
        log_text = Text("\n".join(self.log_messages), style=f"{TEXT_COLOR} on {BACKGROUND}")
        self.layout["log"].update(Panel(log_text, title="Log", border_style=PRIMARY_ACCENT, style=f"on {BACKGROUND}"))


class NullPresenter:
    def update(self, info: Dict[str, Any], hashrate: float) -> None:
        pass

# Application Layer
class TuneBitaxeUseCase:
    def __init__(self, tuning_strategy: TuningStrategy, hardware_gateway: HardwareGateway, logger: Logger,
                 snapshot_manager: SnapshotManager, presenter: Presenter, sample_interval: float,
                 initial_voltage: float, initial_frequency: float):
        self.tuning_strategy = tuning_strategy
        self.hardware_gateway = hardware_gateway
        self.logger = logger
        self.snapshot_manager = snapshot_manager
        self.presenter = presenter
        self.sample_interval = sample_interval
        self.running = True
        self.current_voltage = initial_voltage
        self.current_frequency = initial_frequency
        self.hardware_gateway.set_settings(self.current_voltage, self.current_frequency)

    def start(self) -> None:
        while self.running:
            info = self.hardware_gateway.get_system_info()
            if info is None:
                time.sleep(self.sample_interval)
                continue
            temp = float(info.get("temp", "N/A")) if info.get("temp", "N/A") != "N/A" else DEFAULTS["TARGET_TEMP"] + 1
            hashrate = info.get("hashRate", 0)
            power = info.get("power", 0)
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.logger.log(timestamp, self.current_frequency, self.current_voltage, hashrate, temp)
            self.presenter.update(info, hashrate)
            new_voltage, new_frequency = self.tuning_strategy.adjust(self.current_voltage, self.current_frequency, temp, hashrate, power)
            self.current_voltage = new_voltage
            self.current_frequency = self.hardware_gateway.set_settings(new_voltage, new_frequency)
            self.snapshot_manager.save(self.current_voltage, self.current_frequency)
            time.sleep(self.sample_interval)

    def stop(self) -> None:
        self.running = False
        self.snapshot_manager.save(self.current_voltage, self.current_frequency)

# Main Setup
def setup_logging_arguments(self):
    logging = self.parser.add_argument_group("Logging Options")
    logging.add_argument("--log-level", choices=["info", "debug"], default="info")

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")

    # New --ip flag (Explicit instead of positional)
    parser.add_argument("--ip", required=True, type=str, help="IP address of the Bitaxe miner")

    # YAML config support
    parser.add_argument("--config", type=str, help="Path to YAML configuration file")

    # Existing configuration/overrides via CLI
    parser.add_argument("-v", "--voltage", type=int, help="Initial voltage in mV")
    parser.add_argument("-f", "--frequency", type=int, help="Initial frequency in MHz")
    parser.add_argument("-t", "--target_temp", type=float, help="Target temperature in °C")
    parser.add_argument("-i", "--interval", type=int, help="Sample interval in seconds")
    parser.add_argument("-p", "--power_limit", type=float, help="Power limit in W")
    parser.add_argument("-s", "--setpoint", type=float, help="Target hashrate in GH/s")
    parser.add_argument("--temp-watch", action="store_true", help="Enable temperature-watch mode")
    parser.add_argument("--log-to-console", action="store_true", help="Log to console only (disables TUI)")

    # Logging level argument
    parser.add_argument("--logging-level", choices=["info", "debug"], default="info",
                        help="Logging detail (default: info)")

    return parser.parse_args()

def load_yaml_config(path: str) -> dict:
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load configuration file: {e}")
        sys.exit(1)


def main():
    args = parse_arguments()

    # Set logging level from command line flag
    handlers = [logging.FileHandler("bitaxepid_monitor.log")]
    if args.log_to_console:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(level=logging.DEBUG if args.logging_level == "debug" else logging.INFO, 
                        format="%(asctime)s - %(levelname)s - %(message)s",
                        handlers=handlers)

    config = DEFAULTS.copy()
    if args.config:
        try:
            with open(args.config, "r") as f:
                yaml_config = yaml.safe_load(f)
            if yaml_config is None:
                logger.error(f"Failed to parse YAML config file {args.config}: Empty or invalid content")
            else:
                logger.info(f"Loading configuration from file: {args.config}")
                logger.debug(f"Raw YAML config loaded: {yaml_config}")
                
                # Log differences from defaults if debug level is enabled
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    diverged = {k: v for k, v in yaml_config.items() if k in DEFAULTS and v != DEFAULTS[k]}
                    if diverged:
                        logger.debug(f"Values diverging from defaults: {diverged}")
                    else:
                        logger.debug("No values diverge from defaults in YAML config")
                
                config.update(yaml_config)
                logger.debug(f"Config target_temp after YAML load: {config['TARGET_TEMP']}")
        except FileNotFoundError:
            logger.error(f"Config file {args.config} not found, using defaults")
        except Exception as e:
            logger.error(f"Failed to load YAML config file {args.config}: {e}", exc_info=True)

    # Settings priorities: CLI args > YAML config > defaults
    initial_voltage = args.voltage if args.voltage is not None else config["INITIAL_VOLTAGE"]
    initial_frequency = args.frequency if args.frequency is not None else config["INITIAL_FREQUENCY"]
    target_temp = args.target_temp if args.target_temp is not None else config["TARGET_TEMP"]
    sample_interval = args.interval if args.interval is not None else config["SAMPLE_INTERVAL"]
    power_limit = args.power_limit if args.power_limit is not None else config["POWER_LIMIT"]
    setpoint = args.setpoint if args.setpoint is not None else config["HASHRATE_SETPOINT"]
    
    logger.debug(f"Final target_temp used: {target_temp}")
    logger.debug(f"Final hashrate_setpoint used: {setpoint}")

    snapshot_manager = JsonSnapshotManager(
        config.get("SNAPSHOT_FILE", SNAPSHOT_FILE),
        config.get("INITIAL_VOLTAGE", DEFAULTS["INITIAL_VOLTAGE"]),
        config.get("INITIAL_FREQUENCY", DEFAULTS["INITIAL_FREQUENCY"])
    )

    voltage_from_snapshot, frequency_from_snapshot = snapshot_manager.load()
    initial_voltage = initial_voltage if args.voltage else voltage_from_snapshot
    initial_frequency = initial_frequency if args.frequency else frequency_from_snapshot

    hardware_gateway = BitaxeAPI(
        args.ip,
        config["MIN_VOLTAGE"],
        config["MAX_VOLTAGE"],
        config["MIN_FREQUENCY"],
        config["MAX_FREQUENCY"],
        config["FREQUENCY_STEP"]
    )

    logger_instance = CSVLogger(config.get("LOG_FILE", LOG_FILE))

    if args.temp_watch:
        tuning_strategy = TempWatchTuningStrategy(
            config["MIN_VOLTAGE"],
            config["MIN_FREQUENCY"],
            config["VOLTAGE_STEP"],
            config["FREQUENCY_STEP"],
            target_temp
        )
    else:
        tuning_strategy = PIDTuningStrategy(
            config["PID_FREQ_KP"], config["PID_FREQ_KI"], config["PID_FREQ_KD"],
            config["PID_VOLT_KP"], config["PID_VOLT_KI"], config["PID_VOLT_KD"],
            config["MIN_VOLTAGE"], config["MAX_VOLTAGE"],
            config["MIN_FREQUENCY"], config["MAX_FREQUENCY"],
            config["VOLTAGE_STEP"], config["FREQUENCY_STEP"],
            setpoint, sample_interval,
            target_temp, power_limit
        )

    presenter = NullPresenter() if args.log_to_console else TUIPresenter()
    use_case = TuneBitaxeUseCase(
        tuning_strategy, hardware_gateway, logger_instance, snapshot_manager, presenter,
        sample_interval, initial_voltage, initial_frequency
    )

    def handle_sigint(signum: int, frame: Any) -> None:
        logger.info("Received SIGINT, exiting")
        use_case.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        logger.info(f"Starting BitaxePID Monitor. Target temp: {target_temp}°C, Target hashrate: {setpoint} GH/s")
        use_case.start()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        console.print(f"[{WARNING_COLOR}]Unexpected error: {e}[/]")
    finally:
        use_case.stop()
        logger.info("Exiting monitor")
        
if __name__ == "__main__":
    main()  
