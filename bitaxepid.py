#!/usr/bin/env python3
"""
BitaxePID Auto-Tuner (Refactored)

Automated tuning system for Bitaxe 601 Gamma Bitcoin miners (BM1366 ASIC), designed with clean architecture principles.
Optimizes hashrate while respecting temperature and power constraints using PID or temp-watch tuning modes.
Features include a terminal user interface (TUI), CSV logging, JSON snapshots, and integration with pool latency monitoring.
Supports user-configurable stratum endpoints via YAML overrides, falling back to lowest-latency pools from pools.py.

Usage:
    Run with required IP address and optional configuration:
    ```bash
    python bitaxepid2.py --ip 192.168.68.111 [--config BM1366.yaml] [options]
    ```

Dependencies:
    - Standard library: time, socket, yaml, etc.
    - External: requests, rich, simple_pid, pyfiglet, pools.py (in same directory)
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from typing import Dict, Optional, Any, Tuple, Protocol, List
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
from pools import load_pools, measure_latency, parse_endpoint

# Color constants for Cyberdeck TUI theme
BACKGROUND = "#121212"          # Dark background
TEXT_COLOR = "#E0E0E0"          # Light text
PRIMARY_ACCENT = "#39FF14"      # Neon green for highlights
SECONDARY_ACCENT = "#00BFFF"    # Bright blue for secondary highlights
WARNING_COLOR = "#FF9933"       # Orange for warnings
ERROR_COLOR = "#FF0000"         # Red for errors
DECORATIVE_COLOR = "#FF0099"    # Pink for decorative elements
TABLE_HEADER = DECORATIVE_COLOR # Header color for tables
TABLE_ROW_EVEN = "#222222"      # Dark gray for even rows
TABLE_ROW_ODD = "#444444"       # Mid gray for odd rows
PROGRESS_BAR_BG = "#333333"     # Gray for progress bar background

# Default configuration values
DEFAULTS: Dict[str, Any] = {
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
    "PID_FREQ_KP": 0.2,
    "PID_FREQ_KI": 0.01,
    "PID_FREQ_KD": 0.02,
    "PID_VOLT_KP": 0.1,
    "PID_VOLT_KI": 0.01,
    "PID_VOLT_KD": 0.02,
    "LOG_FILE": "bitaxepid_tuning_log.csv",
    "SNAPSHOT_FILE": "bitaxepid_snapshot.json",
    "POOLS_FILE": "pools.yaml"
}

SNAPSHOT_FILE = "bitaxepid_snapshot.json"
LOG_FILE = "bitaxepid_tuning_log.csv"

console = Console()
logger = logging.getLogger(__name__)

# --- Pool Integration Functions ---

def get_top_pools(config: Dict[str, Any], pools_file: str = "pools.yaml") -> List[Dict[str, Any]]:
    """
    Retrieve the two stratum endpoints, prioritizing user overrides from config.

    Checks the config for user-specified PRIMARY_STRATUM and BACKUP_STRATUM.
    Overrides in the configuration are now provided as a single endpoint string (including port).
    If not fully overridden, falls back to the lowest-latency pools from pools.py.

    Args:
        config (Dict[str, Any]): Loaded configuration dictionary from YAML.
        pools_file (str): Path to the pools YAML file. Defaults to 'pools.yaml'.

    Returns:
        List[Dict[str, Any]]: List of up to two dictionaries with 'endpoint' and 'port' keys.
    """
    stratum_info: List[Dict[str, Any]] = []

    # Check for user overrides in config (using a single 'endpoint' string)
    primary = config.get("PRIMARY_STRATUM")
    backup = config.get("BACKUP_STRATUM")
    if primary and "endpoint" in primary:
        hostname, port = parse_endpoint(primary["endpoint"])
        stratum_info.append({"endpoint": hostname, "port": port})
    if backup and "endpoint" in backup:
        hostname, port = parse_endpoint(backup["endpoint"])
        stratum_info.append({"endpoint": hostname, "port": port})

    # Fill remaining slots with pools.py results if needed
    if len(stratum_info) < 2:
        try:
            auto_pools = load_pools(pools_file)
            results: List[Dict[str, Any]] = []
            for pool in auto_pools:
                # Handle pools where only an 'endpoint' key is provided.
                if "endpoint" in pool and "port" not in pool:
                    hostname, port = parse_endpoint(pool["endpoint"])
                else:
                    hostname = pool["endpoint"]
                    port = pool["port"]
                latency = measure_latency(hostname, port)
                results.append({"endpoint": hostname, "port": port, "latency": latency})
            results.sort(key=lambda x: x["latency"])
            top_auto = [res for res in results if res["latency"] < float('inf')][:2 - len(stratum_info)]
            stratum_info.extend([{"endpoint": res["endpoint"], "port": res["port"]} for res in top_auto])
        except (FileNotFoundError, yaml.YAMLError) as e:
            logger.error(f"Failed to load pools from {pools_file}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error fetching pools: {e}")

    return stratum_info[:2]


# --- Domain Layer Protocols and Implementations ---

class TuningStrategy(Protocol):
    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        """Adjust voltage and frequency based on current metrics."""
        pass

class PIDTuningStrategy:
    """PID-based tuning strategy for optimizing hashrate within constraints."""

    def __init__(self, kp_freq: float, ki_freq: float, kd_freq: float, kp_volt: float, ki_volt: float, kd_volt: float,
                 min_voltage: float, max_voltage: float, min_frequency: float, max_frequency: float,
                 voltage_step: float, frequency_step: float, setpoint: float, sample_interval: float,
                 target_temp: float, power_limit: float) -> None:
        """
        Initialize the PID tuning strategy with control parameters.

        Args:
            kp_freq (float): Proportional gain for frequency PID.
            ki_freq (float): Integral gain for frequency PID.
            kd_freq (float): Derivative gain for frequency PID.
            kp_volt (float): Proportional gain for voltage PID.
            ki_volt (float): Integral gain for voltage PID.
            kd_volt (float): Derivative gain for voltage PID.
            min_voltage (float): Minimum allowed voltage in mV.
            max_voltage (float): Maximum allowed voltage in mV.
            min_frequency (float): Minimum allowed frequency in MHz.
            max_frequency (float): Maximum allowed frequency in MHz.
            voltage_step (float): Voltage adjustment step size in mV.
            frequency_step (float): Frequency adjustment step size in MHz.
            setpoint (float): Target hashrate in GH/s.
            sample_interval (float): PID sample interval in seconds.
            target_temp (float): Target temperature in °C.
            power_limit (float): Power limit in watts.
        """
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
        self.last_hashrate: Optional[float] = None
        self.stagnation_count = 0
        self.drop_count = 0

    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        """
        Adjust voltage and frequency based on current system metrics using PID control.

        Prioritizes temperature and power limits over hashrate optimization.

        Args:
            current_voltage (float): Current voltage in mV.
            current_frequency (float): Current frequency in MHz.
            temp (float): Current temperature in °C.
            hashrate (float): Current hashrate in GH/s.
            power (float): Current power consumption in watts.

        Returns:
            Tuple[float, float]: New (voltage, frequency) settings.
        """
        freq_output = self.pid_freq(hashrate)
        volt_output = self.pid_volt(hashrate)
        proposed_frequency = round(freq_output / self.frequency_step) * self.frequency_step
        proposed_frequency = max(self.min_frequency, min(self.max_frequency, proposed_frequency))
        proposed_voltage = round(volt_output / self.voltage_step) * self.voltage_step
        proposed_voltage = max(self.min_voltage, min(self.max_voltage, proposed_voltage))

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

        if temp > self.target_temp:
            if current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                logger.info(f"Reducing frequency to {new_frequency}MHz due to temp {temp}°C > {self.target_temp}°C")
            elif current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                logger.info(f"Reducing voltage to {new_voltage}mV due to temp {temp}°C > {self.target_temp}°C")
        elif power > self.power_limit * 1.075:
            if current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                logger.info(f"Reducing voltage to {new_voltage}mV due to power {power}W > {self.power_limit * 1.075}W")

        elif hashrate < self.pid_freq.setpoint:
            if self.drop_count >= 30 and current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                logger.info(f"Reducing frequency to {new_frequency}MHz due to repeated hashrate drops")
            else:
                if hashrate < 0.85 * self.pid_freq.setpoint and current_voltage < self.max_voltage:
                    new_voltage = min(proposed_voltage, current_voltage + self.voltage_step)
                    logger.info(f"Increasing voltage to {new_voltage}mV due to hashrate {hashrate} < {0.85 * self.pid_freq.setpoint}")
                new_frequency = proposed_frequency
                logger.info(f"Adjusting frequency to {new_frequency}MHz via PID")
                if current_frequency >= self.max_frequency and current_voltage < self.max_voltage:
                    new_voltage = current_voltage + self.voltage_step
                    logger.info(f"Increasing voltage to {new_voltage}mV as frequency at max")
        else:
            logger.info(f"System stable at Voltage={current_voltage}mV, Frequency={new_frequency}MHz")

        self.last_hashrate = hashrate
        return new_voltage, new_frequency

class TempWatchTuningStrategy:
    """Simple temperature-based tuning strategy."""

    def __init__(self, min_voltage: float, min_frequency: float, voltage_step: float, frequency_step: float, target_temp: float) -> None:
        """
        Initialize the temperature-watch tuning strategy.

        Args:
            min_voltage (float): Minimum allowed voltage in mV.
            min_frequency (float): Minimum allowed frequency in MHz.
            voltage_step (float): Voltage adjustment step size in mV.
            frequency_step (float): Frequency adjustment step size in MHz.
            target_temp (float): Target temperature in °C.
        """
        self.min_voltage = min_voltage
        self.min_frequency = min_frequency
        self.voltage_step = voltage_step
        self.frequency_step = frequency_step
        self.target_temp = target_temp

    def adjust(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
        """
        Adjust voltage and frequency based on temperature only.

        Args:
            current_voltage (float): Current voltage in mV.
            current_frequency (float): Current frequency in MHz.
            temp (float): Current temperature in °C.
            hashrate (float): Current hashrate in GH/s (unused).
            power (float): Current power in watts (unused).

        Returns:
            Tuple[float, float]: New (voltage, frequency) settings.
        """
        if temp > self.target_temp:
            if current_frequency > self.min_frequency:
                return current_voltage, current_frequency - self.frequency_step
            elif current_voltage > self.min_voltage:
                return current_voltage - self.voltage_step, current_frequency
        return current_voltage, current_frequency

# --- Infrastructure Layer ---

class HardwareGateway(Protocol):
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """Fetch current system metrics."""
        pass

    def set_settings(self, voltage: float, frequency: float) -> float:
        """Apply voltage and frequency settings."""
        pass

class BitaxeAPI:
    """Interface to Bitaxe miner hardware via HTTP API."""

    def __init__(self, bitaxepid_ip: str, min_voltage: float, max_voltage: float, min_frequency: float, max_frequency: float, frequency_step: float) -> None:
        """
        Initialize the Bitaxe API client.

        Args:
            bitaxepid_ip (str): IP address of the Bitaxe miner.
            min_voltage (float): Minimum allowed voltage in mV.
            max_voltage (float): Maximum allowed voltage in mV.
            min_frequency (float): Minimum allowed frequency in MHz.
            max_frequency (float): Maximum allowed frequency in MHz.
            frequency_step (float): Frequency adjustment step size in MHz.
        """
        self.bitaxepid_url = f"http://{bitaxepid_ip}"
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.frequency_step = frequency_step

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve system information from the Bitaxe miner.

        Returns:
            Optional[Dict[str, Any]]: System metrics or None if request fails.
        """
        try:
            response = requests.get(f"{self.bitaxepid_url}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching system info: {e}")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
        """
        Apply specified voltage and frequency settings to the miner.

        Args:
            voltage (float): Desired voltage in mV.
            frequency (float): Desired frequency in MHz.

        Returns:
            float: Applied frequency in MHz (may be adjusted to step boundary).
        """
        frequency = round(frequency / self.frequency_step) * self.frequency_step
        frequency = max(self.min_frequency, min(self.max_frequency, frequency))
        voltage = max(self.min_voltage, min(self.max_voltage, voltage))
        settings = {"coreVoltage": voltage, "frequency": frequency}
        try:
            response = requests.patch(f"{self.bitaxepid_url}/api/system", json=settings, timeout=10)
            response.raise_for_status()
            logger.info(f"Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz")
            time.sleep(2)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting system settings: {e}")
        return frequency

class Logger(Protocol):
    def log(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float) -> None:
        """Log tuning metrics."""
        pass

class CSVLogger:
    """CSV-based logger for tuning metrics."""

    def __init__(self, log_file: str) -> None:
        """
        Initialize the CSV logger.

        Args:
            log_file (str): Path to the CSV log file.
        """
        self.log_file = log_file

    def log(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float) -> None:
        """
        Log a single entry of tuning metrics to CSV.

        Args:
            timestamp (str): Current timestamp (e.g., "2025-03-04 12:00:00").
            frequency (float): Frequency in MHz.
            voltage (float): Voltage in mV.
            hashrate (float): Hashrate in GH/s.
            temp (float): Temperature in °C.
        """
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
        """Load previous voltage and frequency settings."""
        pass

    def save(self, voltage: float, frequency: float) -> None:
        """Save current voltage and frequency settings."""
        pass

class JsonSnapshotManager:
    """JSON-based snapshot manager for persisting settings."""

    def __init__(self, snapshot_file: str, default_voltage: float, default_frequency: float) -> None:
        """
        Initialize the JSON snapshot manager.

        Args:
            snapshot_file (str): Path to the JSON snapshot file.
            default_voltage (float): Default voltage in mV if snapshot is unavailable.
            default_frequency (float): Default frequency in MHz if snapshot is unavailable.
        """
        self.snapshot_file = snapshot_file
        self.default_voltage = default_voltage
        self.default_frequency = default_frequency

    def load(self) -> Tuple[float, float]:
        """
        Load the last saved voltage and frequency settings from JSON.

        Returns:
            Tuple[float, float]: (voltage, frequency) settings in mV and MHz.
        """
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
        """
        Save current voltage and frequency settings to JSON.

        Args:
            voltage (float): Voltage in mV to save.
            frequency (float): Frequency in MHz to save.
        """
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

# --- Presentation Layer ---

class Presenter(Protocol):
    def update(self, info: Dict[str, Any], hashrate: float, stratum_info: List[Dict[str, Any]]) -> None:
        """Update the presentation layer with system info."""
        pass

class TUIPresenter:
    """Rich-based terminal UI presenter for real-time monitoring."""

    def __init__(self) -> None:
        """Initialize the TUI with a layout and live display."""
        self.log_messages: List[str] = []
        self.layout = self.create_layout()
        self.live = Live(self.layout, console=console, refresh_per_second=1)
        self.live.start()

    def create_layout(self) -> Layout:
        """
        Create the TUI layout structure.

        Returns:
            Layout: A rich Layout object with defined sections.
        """
        layout = Layout()
        layout.split_column(Layout(name="top", size=15), Layout(name="bottom", ratio=1))
        layout["top"].split_row(Layout(name="hashrate", ratio=3), Layout(name="header", ratio=2))
        layout["bottom"].split_column(Layout(name="main", ratio=1), Layout(name="log", size=10))
        layout["main"].split_row(Layout(name="stats", ratio=1), Layout(name="progress", ratio=2))
        return layout

    def update(self, info: Dict[str, Any], hashrate: float, stratum_info: List[Dict[str, Any]]) -> None:
        """
        Update the TUI with current system metrics and stratum information.

        Args:
            info (Dict[str, Any]): System metrics from the miner.
            hashrate (float): Current hashrate in GH/s.
            stratum_info (List[Dict[str, Any]]): List of stratum endpoints and ports.
        """
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

        # Header with Power and Stratum Info
        header_table = Table(box=box.SIMPLE, style=DECORATIVE_COLOR, title=f"BitaxePID Auto-Tuner (Host: {hostname})")
        header_table.add_column("Parameter", style=f"bold {DECORATIVE_COLOR}", justify="right")
        header_table.add_column("Value", style=f"bold {TEXT_COLOR}")
        power_str = f"{power:.2f}W" + (f" [{WARNING_COLOR}](OVER LIMIT)[/]" if power > DEFAULTS["POWER_LIMIT"] else "")
        header_table.add_row("Power", power_str)
        header_table.add_row("Current", f"{info.get('current', 0):.2f}mA")
        header_table.add_row("Core Voltage", f"{core_voltage:.2f}mV")
        header_table.add_row("Voltage", f"{voltage:.0f}mV")
        primary_stratum = f"{stratum_info[0]['endpoint']}:{stratum_info[0]['port']}" if stratum_info else "N/A"
        backup_stratum = f"{stratum_info[1]['endpoint']}:{stratum_info[1]['port']}" if len(stratum_info) > 1 else "N/A"
        header_table.add_row("Primary Stratum", primary_stratum)
        header_table.add_row("Backup Stratum", backup_stratum)
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
    """No-op presenter for console-only logging."""

    def update(self, info: Dict[str, Any], hashrate: float, stratum_info: List[Dict[str, Any]]) -> None:
        """
        Do nothing (placeholder for console-only mode).

        Args:
            info (Dict[str, Any]): System metrics (unused).
            hashrate (float): Current hashrate in GH/s (unused).
            stratum_info (List[Dict[str, Any]]): Stratum endpoints (unused).
        """
        pass

# --- Application Layer ---

class TuneBitaxeUseCase:
    """Core use case for tuning the Bitaxe miner."""

    def __init__(self, tuning_strategy: TuningStrategy, hardware_gateway: HardwareGateway, logger: Logger,
                 snapshot_manager: SnapshotManager, presenter: Presenter, sample_interval: float,
                 initial_voltage: float, initial_frequency: float, pools_file: str, config: Dict[str, Any]) -> None:
        """
        Initialize the tuning use case.

        Args:
            tuning_strategy (TuningStrategy): Strategy for adjusting settings.
            hardware_gateway (HardwareGateway): Interface to miner hardware.
            logger (Logger): Logging mechanism for metrics.
            snapshot_manager (SnapshotManager): Manager for persisting settings.
            presenter (Presenter): UI or logging presenter.
            sample_interval (float): Sampling interval in seconds.
            initial_voltage (float): Initial voltage in mV.
            initial_frequency (float): Initial frequency in MHz.
            pools_file (str): Path to pools YAML file.
            config (Dict[str, Any]): Configuration dictionary from YAML.
        """
        self.tuning_strategy = tuning_strategy
        self.hardware_gateway = hardware_gateway
        self.logger = logger
        self.snapshot_manager = snapshot_manager
        self.presenter = presenter
        self.sample_interval = sample_interval
        self.running = True
        self.current_voltage = initial_voltage
        self.current_frequency = initial_frequency
        self.pools_file = pools_file
        self.config = config
        # Fetch stratum info once during initialization
        self.stratum_info = get_top_pools(self.config, self.pools_file)
        self.hardware_gateway.set_settings(self.current_voltage, self.current_frequency)

    def start(self) -> None:
        """
        Start the tuning loop, adjusting settings and updating the UI/logging.
        """
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
            # Use the pre-fetched stratum_info instead of calling get_top_pools again
            self.presenter.update(info, hashrate, self.stratum_info)
            new_voltage, new_frequency = self.tuning_strategy.adjust(self.current_voltage, self.current_frequency, temp, hashrate, power)
            self.current_voltage = new_voltage
            self.current_frequency = self.hardware_gateway.set_settings(new_voltage, new_frequency)
            self.snapshot_manager.save(self.current_voltage, self.current_frequency)
            time.sleep(self.sample_interval)

    def stop(self) -> None:
        """Stop the tuning loop and save current settings."""
        self.running = False
        self.snapshot_manager.save(self.current_voltage, self.current_frequency)

# --- Main Setup ---

def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments for the BitaxePID Auto-Tuner.

    Returns:
        argparse.Namespace: Parsed command-line arguments.
    """
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")
    parser.add_argument("--ip", required=True, type=str, help="IP address of the Bitaxe miner")
    parser.add_argument("--config", type=str, help="Path to YAML configuration file")
    parser.add_argument("-v", "--voltage", type=int, help="Initial voltage in mV")
    parser.add_argument("-f", "--frequency", type=int, help="Initial frequency in MHz")
    parser.add_argument("-t", "--target_temp", type=float, help="Target temperature in °C")
    parser.add_argument("-i", "--interval", type=int, help="Sample interval in seconds")
    parser.add_argument("-p", "--power_limit", type=float, help="Power limit in W")
    parser.add_argument("-s", "--setpoint", type=float, help="Target hashrate in GH/s")
    parser.add_argument("--temp-watch", action="store_true", help="Enable temperature-watch mode")
    parser.add_argument("--log-to-console", action="store_true", help="Log to console only (disables TUI)")
    parser.add_argument("--logging-level", choices=["info", "debug"], default="info", help="Logging detail")
    parser.add_argument("--pools-file", type=str, default="pools.yaml", help="Path to pools YAML file")
    return parser.parse_args()

def load_yaml_config(path: str) -> Dict[str, Any]:
    """
    Load configuration from a YAML file.

    Args:
        path (str): Path to the YAML configuration file.

    Returns:
        Dict[str, Any]: Configuration dictionary.

    Raises:
        SystemExit: If the file cannot be loaded or is invalid.
    """
    try:
        with open(path, "r") as f:
            config = yaml.safe_load(f)
            if config is None:
                raise ValueError("YAML file is empty")
            return config
    except Exception as e:
        logger.error(f"Failed to load configuration file {path}: {e}")
        sys.exit(1)

def main() -> None:
    """
    Main entry point for the BitaxePID Auto-Tuner.

    Sets up logging, loads configuration, initializes components, and starts the tuning loop.
    """
    args = parse_arguments()

    handlers = [logging.FileHandler("bitaxepid_monitor.log")]
    if args.log_to_console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if args.logging_level == "debug" else logging.INFO,
                        format="%(asctime)s - %(levelname)s - %(message)s",
                        handlers=handlers)

    config = DEFAULTS.copy()
    if args.config:
        config.update(load_yaml_config(args.config))
    # print("Loaded config:", config)
        

    initial_voltage = args.voltage if args.voltage is not None else config["INITIAL_VOLTAGE"]
    initial_frequency = args.frequency if args.frequency is not None else config["INITIAL_FREQUENCY"]
    target_temp = args.target_temp if args.target_temp is not None else config["TARGET_TEMP"]
    sample_interval = args.interval if args.interval is not None else config["SAMPLE_INTERVAL"]
    power_limit = args.power_limit if args.power_limit is not None else config["POWER_LIMIT"]
    setpoint = args.setpoint if args.setpoint is not None else config["HASHRATE_SETPOINT"]

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
        sample_interval, initial_voltage, initial_frequency, args.pools_file, config
    )

    def handle_sigint(signum: int, frame: Any) -> None:
        logger.info("Received SIGINT, exiting")
        use_case.stop()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        logger.info(f"Starting BitaxePID Monitor with pools from {args.pools_file}")
        use_case.start()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        console.print(f"[{WARNING_COLOR}]Unexpected error: {e}[/]")
    finally:
        use_case.stop()
        logger.info("Exiting monitor")

if __name__ == "__main__":
    main()

