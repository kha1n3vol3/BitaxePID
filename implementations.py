#!/usr/bin/env python3
"""
Implementations Module for BitaxePID Auto-Tuner

This module provides concrete implementations of the interfaces defined in `interfaces.py` for the BitaxePID
auto-tuner. It includes classes for interacting with the Bitaxe miner API, logging data to CSV and JSON,
loading YAML configurations, displaying a rich terminal UI, and applying a PID-based tuning strategy.

Usage:
    >>> from implementations import BitaxeAPIClient, Logger
    >>> client = BitaxeAPIClient("192.168.1.1")
    >>> logger = Logger("log.csv", "snapshot.json")
    >>> system_info = client.get_system_info()
    >>> logger.log_to_csv("2025-03-11 10:00:00", 485, 1200, 500, 48, {"PID_FREQ_KP": 0.2}, 14.6, 4812.5, 3001.25, 1312, 485, 3870)

Dependencies:
    - urllib3, pyyaml, simple_pid, rich, pyfiglet, csv, json, os, time, typing
"""

import csv
import json
import os
import time
from typing import Dict, Any, Optional, Tuple
import urllib3
from urllib3.util.retry import Retry
from interfaces import IBitaxeAPIClient, ILogger, IConfigLoader, ITerminalUI, TuningStrategy
from simple_pid import PID
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box
import pyfiglet
from logging import getLogger
import yaml

# Color constants for Cyberdeck TUI theme
BACKGROUND = "#121212"
TEXT_COLOR = "#E0E0E0"
PRIMARY_ACCENT = "#39FF14"
SECONDARY_ACCENT = "#00BFFF"
WARNING_COLOR = "#FF9933"
ERROR_COLOR = "#FF0000"
DECORATIVE_COLOR = "#FF0099"
TABLE_HEADER = DECORATIVE_COLOR
TABLE_ROW_EVEN = "#222222"
TABLE_ROW_ODD = "#444444"
PROGRESS_BAR_BG = "#333333"

console = Console()


class BitaxeAPIClient(IBitaxeAPIClient):
    """Concrete implementation of the Bitaxe API client using urllib3 for robust communication."""

    def __init__(self, ip: str, timeout: int = 10, retries: int = 5, pool_maxsize: int = 10) -> None:
        """
        Initialize the Bitaxe API client with a connection pool.

        Args:
            ip (str): IP address of the Bitaxe miner (e.g., "192.168.1.1").
            timeout (int): Timeout for each request in seconds (default: 10).
            retries (int): Number of retries for failed requests (default: 5).
            pool_maxsize (int): Maximum number of connections in the pool (default: 10).
        """
        self.bitaxepid_url = f"http://{ip}"
        self.logger = getLogger(__name__)
        retry_strategy = Retry(
            total=retries,
            backoff_factor=1,  # Exponential backoff: 1s, 2s, 4s, etc.
            status_forcelist=[500, 502, 503, 504],  # Retry on server errors
        )
        self.http_pool = urllib3.HTTPConnectionPool(
            host=ip,
            port=80,
            timeout=urllib3.Timeout(connect=timeout, read=timeout),
            maxsize=pool_maxsize,
            retries=retry_strategy,
            block=False
        )
        self.logger.info(f"Initialized BitaxeAPIClient for {ip} with timeout={timeout}s, retries={retries}, pool_maxsize={pool_maxsize}")

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve current system information from the miner.

        Returns:
            Optional[Dict[str, Any]]: System information as a dictionary (e.g., {"hashRate": 500, "temp": 48}), or None if unavailable.

        Example:
            >>> client = BitaxeAPIClient("192.168.1.1")
            >>> info = client.get_system_info()
            >>> info.get("hashRate")
            500.0
        """
        try:
            response = self.http_pool.request('GET', '/api/system/info')
            if response.status == 200:
                return json.loads(response.data.decode('utf-8'))
            else:
                self.logger.error(f"Failed to fetch system info: HTTP {response.status}")
                console.print(f"[{ERROR_COLOR}]Failed to fetch system info: HTTP {response.status}[/]")
                return None
        except urllib3.exceptions.MaxRetryError as e:
            self.logger.error(f"Max retries exceeded fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Max retries exceeded fetching system info: {e}[/]")
            return None
        except urllib3.exceptions.TimeoutError as e:
            self.logger.error(f"Timeout fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Timeout fetching system info: {e}[/]")
            return None
        except Exception as e:
            self.logger.error(f"Unexpected error fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Unexpected error fetching system info: {e}[/]")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
        """
        Set voltage and frequency on the miner and return the applied frequency.

        Args:
            voltage (float): Target core voltage to set (mV).
            frequency (float): Target frequency to set (MHz).

        Returns:
            float: The frequency applied by the miner (MHz), returned unchanged if setting fails.

        Example:
            >>> client = BitaxeAPIClient("192.168.1.1")
            >>> applied_freq = client.set_settings(1200, 485)
            >>> applied_freq
            485.0
        """
        settings = {"coreVoltage": voltage, "frequency": frequency}
        try:
            response = self.http_pool.request(
                'PATCH',
                '/api/system',
                body=json.dumps(settings).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            if response.status == 200:
                self.logger.info(f"Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz")
                console.print(f"[{PRIMARY_ACCENT}]Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz[/]")
                time.sleep(2)  # Allow settings to stabilize
                system_info = self.get_system_info()
                if system_info:
                    actual_voltage = system_info.get("coreVoltage", 0)
                    actual_freq = system_info.get("frequency", 0)
                    if abs(actual_voltage - voltage) > 5 or abs(actual_freq - frequency) > 5:
                        self.logger.warning(f"Settings mismatch - Requested: {voltage}mV/{frequency}MHz, "
                                          f"Actual: {actual_voltage}mV/{actual_freq}MHz")
                return frequency
            self.logger.error(f"Failed to set settings: HTTP {response.status}")
            return frequency
        except Exception as e:
            self.logger.error(f"Error setting system settings: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting system settings: {e}[/]")
            return frequency

    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """
        Configure primary and backup stratum pools.

        Args:
            primary (Dict[str, Any]): Configuration for the primary stratum pool (e.g., {"hostname": "solo.ckpool.org", "port": 3333, "user": "user1"}).
            backup (Dict[str, Any]): Configuration for the backup stratum pool (e.g., {"hostname": "pool.example.com", "port": 3333, "user": "user2"}).

        Returns:
            bool: True if the stratum settings were successfully applied, False otherwise.

        Example:
            >>> client = BitaxeAPIClient("192.168.1.1")
            >>> primary = {"hostname": "solo.ckpool.org", "port": 3333, "user": "user1"}
            >>> backup = {"hostname": "pool.example.com", "port": 3333, "user": "user2"}
            >>> success = client.set_stratum(primary, backup)
            >>> success
            True
        """
        settings = {
            "stratumURL": primary["hostname"],
            "stratumPort": primary["port"],
            "fallbackStratumURL": backup["hostname"],
            "fallbackStratumPort": backup["port"],
            "stratumUser": primary.get("user", ""),
            "fallbackStratumUser": backup.get("user", "")
        }
        try:
            response = self.http_pool.request(
                'PATCH',
                '/api/system',
                body=json.dumps(settings).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            if response.status == 200:
                self.logger.info(f"Set stratum: Primary={primary['hostname']}:{primary['port']} "
                               f"User={primary.get('user', '')}, "
                               f"Backup={backup['hostname']}:{backup['port']} "
                               f"User={backup.get('user', '')}")
                console.print(f"[{PRIMARY_ACCENT}]Set stratum configuration successfully[/]")
                time.sleep(1)
                system_info = self.get_system_info()
                if system_info and not all([
                    system_info.get("stratumURL") == primary["hostname"],
                    system_info.get("stratumPort") == primary["port"],
                    system_info.get("fallbackStratumURL") == backup["hostname"],
                    system_info.get("fallbackStratumPort") == backup["port"],
                    system_info.get("stratumUser") == primary.get("user", ""),
                    system_info.get("fallbackStratumUser") == backup.get("user", "")
                ]):
                    self.logger.warning("Stratum settings verification failed")
                    return False
                return True
            self.logger.error(f"Failed to set stratum: HTTP {response.status}")
            return False
        except Exception as e:
            self.logger.error(f"Error setting stratum endpoints: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting stratum endpoints: {e}[/]")
            return False

    def restart(self) -> bool:
        """
        Restart the Bitaxe miner.

        Returns:
            bool: True if the restart was successful and the miner responds, False otherwise.

        Example:
            >>> client = BitaxeAPIClient("192.168.1.1")
            >>> success = client.restart()
            >>> success
            True
        """
        try:
            response = self.http_pool.request('POST', '/api/system/restart')
            if response.status == 200:
                self.logger.info("Restarted Bitaxe miner")
                console.print(f"[{PRIMARY_ACCENT}]Restarted Bitaxe miner[/]")
                time.sleep(5)  # Wait for restart
                for _ in range(3):
                    if self.get_system_info():
                        self.logger.info("Miner successfully restarted and responding")
                        return True
                    time.sleep(2)
                self.logger.warning("Miner restart completed but not responding")
                return False
            self.logger.error(f"Failed to restart miner: HTTP {response.status}")
            return False
        except Exception as e:
            self.logger.error(f"Error restarting Bitaxe miner: {e}")
            console.print(f"[{ERROR_COLOR}]Error restarting Bitaxe miner: {e}[/]")
            return False

    def close(self) -> None:
        """
        Close the connection pool to free resources.

        Example:
            >>> client = BitaxeAPIClient("192.168.1.1")
            >>> client.close()
        """
        self.http_pool.close()
        self.logger.info("BitaxeAPIClient connection pool closed")
        console.print(f"[{PRIMARY_ACCENT}]BitaxeAPIClient connection pool closed[/]")


class Logger(ILogger):
    """Concrete implementation for logging miner data to CSV and snapshots to JSON."""

    def __init__(self, log_file: str, snapshot_file: str) -> None:
        """
        Initialize the logger with file paths.

        Args:
            log_file (str): Path to the CSV log file (e.g., "bitaxepid_tuning_log.csv").
            snapshot_file (str): Path to the JSON snapshot file (e.g., "bitaxepid_snapshot.json").
        """
        self.log_file = log_file
        self.snapshot_file = snapshot_file
        self._initialize_csv()

    def _initialize_csv(self) -> None:
        """Initialize the CSV file with an alphabetized header row (MAC address first) if it doesn't exist."""
        if not os.path.exists(self.log_file):
            headers = [
                "mac_address", "timestamp", "target_frequency", "target_voltage", "hashrate", "temp",
                "power", "board_voltage", "current", "core_voltage_actual", "frequency", "fanrpm",
                "pid_freq_kp", "pid_freq_ki", "pid_freq_kd", "pid_volt_kp", "pid_volt_ki", "pid_volt_kd",
                "initial_frequency", "min_frequency", "max_frequency", "initial_voltage", "min_voltage",
                "max_voltage", "frequency_step", "voltage_step", "target_temp", "sample_interval",
                "power_limit", "hashrate_setpoint"
            ]
            with open(self.log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(headers)

    def log_to_csv(
        self,
        mac_address: str,
        timestamp: str,
        target_frequency: float,
        target_voltage: float,
        hashrate: float,
        temp: float,
        pid_settings: Dict[str, Any],
        power: float,
        board_voltage: float,
        current: float,
        core_voltage_actual: float,
        frequency: float,
        fanrpm: int
    ) -> None:
        """
        Log miner performance data, including flattened PID settings and MAC address, to a CSV file.

        Args:
            mac_address (str): MAC address of the miner.
            timestamp (str): Time of the data point (e.g., "2025-03-11 10:00:00").
            target_frequency (float): Target frequency commanded by PID (MHz).
            target_voltage (float): Target core voltage commanded by PID (mV).
            hashrate (float): Measured hashrate (GH/s).
            temp (float): Measured temperature (°C).
            pid_settings (Dict[str, Any]): PID controller settings (e.g., {"PID_FREQ_KP": 0.2, "PID_VOLT_KI": 0.01}).
            power (float): Measured power consumption (W).
            board_voltage (float): Measured board voltage (mV).
            current (float): Measured current (mA).
            core_voltage_actual (float): Actual core voltage (mV).
            frequency (float): Actual frequency (MHz).
            fanrpm (int): Fan speed (RPM).
        """
        with open(self.log_file, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                mac_address, timestamp, target_frequency, target_voltage, hashrate, temp,
                power, board_voltage, current, core_voltage_actual, frequency, fanrpm,
                pid_settings.get("PID_FREQ_KP", ""),
                pid_settings.get("PID_FREQ_KI", ""),
                pid_settings.get("PID_FREQ_KD", ""),
                pid_settings.get("PID_VOLT_KP", ""),
                pid_settings.get("PID_VOLT_KI", ""),
                pid_settings.get("PID_VOLT_KD", ""),
                pid_settings.get("INITIAL_FREQUENCY", ""),
                pid_settings.get("MIN_FREQUENCY", ""),
                pid_settings.get("MAX_FREQUENCY", ""),
                pid_settings.get("INITIAL_VOLTAGE", ""),
                pid_settings.get("MIN_VOLTAGE", ""),
                pid_settings.get("MAX_VOLTAGE", ""),
                pid_settings.get("FREQUENCY_STEP", ""),
                pid_settings.get("VOLTAGE_STEP", ""),
                pid_settings.get("TARGET_TEMP", ""),
                pid_settings.get("SAMPLE_INTERVAL", ""),
                pid_settings.get("POWER_LIMIT", ""),
                pid_settings.get("HASHRATE_SETPOINT", "")
            ])

    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """
        Save current miner settings as a snapshot to a JSON file.

        Args:
            voltage (float): Current target voltage setting (mV).
            frequency (float): Current target frequency setting (MHz).
        """
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to save snapshot: {e}[/]")


class YamlConfigLoader(IConfigLoader):
    """Concrete implementation for loading YAML configuration files."""

    def load_config(self, file_path: str) -> Dict[str, Any]:
        """
        Load configuration settings from a YAML file.

        Args:
            file_path (str): Path to the configuration file (e.g., "BM1366.yaml").

        Returns:
            Dict[str, Any]: Configuration data as a dictionary (e.g., {"INITIAL_VOLTAGE": 1200}), empty if loading fails.

        Example:
            >>> loader = YamlConfigLoader()
            >>> config = loader.load_config("BM1366.yaml")
            >>> config["INITIAL_VOLTAGE"]
            1200
        """
        try:
            with open(file_path, "r") as f:
                config = yaml.safe_load(f)
                if config is None:
                    raise ValueError("YAML file is empty")
                return config
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to load configuration file {file_path}: {e}[/]")
            return {}


class RichTerminalUI(ITerminalUI):
    """Rich terminal UI for displaying miner status."""

    def __init__(self) -> None:
        """Initialize the rich terminal UI with layout and sections."""
        self.log_messages: list[str] = []
        self.has_data = False
        self.sections = {
            "Network": ["ssid", "macAddr", "wifiStatus", "stratumDiff", "isUsingFallbackStratum",
                        "stratumURL", "stratumPort", "fallbackStratumURL", "fallbackStratumPort"],
            "Chip": ["ASICModel", "asicCount", "smallCoreCount"],
            "Power": ["power", "voltage", "current"],
            "Thermal": ["temp", "vrTemp", "overheat_mode"],
            "Mining Performance": ["bestDiff", "bestSessionDiff", "sharesAccepted", "sharesRejected"],
            "System": ["freeHeap", "uptimeSeconds", "version", "idfVersion", "boardVersion"],
            "Display & Fans": ["autofanspeed", "fanspeed", "fanrpm"]
        }
        self.layout = self.create_layout()
        self.live = Live(self.layout, console=console, refresh_per_second=1)
        self._started = False

    def show_banner(self) -> None:
        """Display an initial banner until data is available."""
        try:
            with open('banner.txt', 'r') as f:
                console.print(f.read())
            console.print("\nWaiting for miner data...", style=PRIMARY_ACCENT)
        except FileNotFoundError:
            console.print("Banner file not found", style=ERROR_COLOR)

    def create_layout(self) -> Layout:
        """
        Create a layout for the terminal UI.

        Returns:
            Layout: Rich layout object with defined sections.
        """
        layout = Layout()
        layout.split_column(Layout(name="top", size=7), Layout(name="middle"), Layout(name="bottom", size=3))
        layout["top"].split_row(Layout(name="hashrate"), Layout(name="header"))
        layout["middle"].split_row(Layout(name="left_column"), Layout(name="right_column"))
        layout["left_column"].split_column(Layout(name="network"), Layout(name="chip"), Layout(name="power"))
        layout["right_column"].split_column(Layout(name="thermal"), Layout(name="mining_performance"),
                                            Layout(name="system"), Layout(name="display_fans"))
        layout["bottom"].name = "log"
        return layout

    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """
        Update terminal UI with the latest miner data.

        Args:
            system_info (Dict[str, Any]): Current system information (e.g., {"hashRate": 500, "temp": 48}).
            voltage (float): Current target voltage setting (mV).
            frequency (float): Current target frequency setting (MHz).

        Example:
            >>> ui = RichTerminalUI()
            >>> ui.update({"hashRate": 500, "temp": 48}, 1200, 485)
        """
        try:
            if not self.has_data:
                console.clear()
                self.has_data = True

            # Handle hashrate display with unit conversion (hashRate in GH/s)
            hashrate = system_info.get("hashRate", 0)  # hashRate is in GH/s
            if hashrate > 999:  # Convert to Th/s when above 999 GH/s
                hashrate_ths = hashrate / 1000  # Convert GH/s to Th/s
                hashrate_str = f"{hashrate_ths:.2f} Th/s"  # Two decimal places for Th/s
            else:
                hashrate_str = f"{int(hashrate)} GH/s"  # For values <= 999, display in GH/s
            ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
            self.layout["hashrate"].update(Panel(ascii_art, title="Hashrate", border_style=PRIMARY_ACCENT))

            # Header section
            header_table = Table(show_header=False, box=None)
            header_table.add_column("", style=DECORATIVE_COLOR, justify="right")
            header_table.add_column("", style=TEXT_COLOR)
            header_table.add_row("Hostname", system_info.get("hostname", "N/A"))
            header_table.add_row("Voltage", f"{int(voltage)}mV")
            header_table.add_row("Frequency", f"{int(frequency)}MHz")
            header_table.add_row("Temperature", f"{system_info.get('temp', 'N/A')}°C")
            header_table.add_row("Stratum User", system_info.get("stratumUser", "N/A"))
            header_table.add_row("Backup User", system_info.get("fallbackStratumUser", "N/A"))
            self.layout["header"].update(Panel(header_table, title="System Status"))

            # Other sections (Network, Chip, Power, etc.)
            section_layouts = {
                "Network": "network", "Chip": "chip", "Power": "power", "Thermal": "thermal",
                "Mining Performance": "mining_performance", "System": "system", "Display & Fans": "display_fans"
            }
            for section_name, layout_name in section_layouts.items():
                table = Table(show_header=False, box=None)
                table.add_column("", style=DECORATIVE_COLOR)
                table.add_column("", style=TEXT_COLOR)
                for key in self.sections[section_name]:
                    if key in system_info:
                        value = system_info[key]
                        if key in ["stratumURL", "fallbackStratumURL"]:
                            port_key = "stratumPort" if key == "stratumURL" else "fallbackStratumPort"
                            value = f"{value}:{system_info.get(port_key, '')}"
                        elif isinstance(value, (int, float)):
                            value = f"{int(value)}"
                        table.add_row(key, str(value))
                self.layout[layout_name].update(Panel(table, title=section_name))

            # Log section
            status = (f"{time.strftime('%Y-%m-d %H:%M:%S')} - Voltage: {int(voltage)}mV, "
                    f"Frequency: {int(frequency)}MHz, Hashrate: {hashrate_str}, "
                    f"Temp: {system_info.get('temp', 'N/A')}°C")
            self.log_messages.append(status)
            if len(self.log_messages) > 6:
                self.log_messages.pop(0)
            self.layout["log"].update(Panel(Text("\n".join(self.log_messages)), title="Log"))

        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Error updating TUI: {e}[/]")

    def start(self) -> None:
        """Start the live display."""
        if not self._started:
            self.live.start()
            self._started = True

    def stop(self) -> None:
        """Stop the live display."""
        if self._started:
            self.live.stop()
            self._started = False


class NullTerminalUI(ITerminalUI):
    """Null implementation of the terminal UI for console-only logging."""

    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """
        Do nothing (placeholder for UI updates).

        Args:
            system_info (Dict[str, Any]): Current system information (ignored).
            voltage (float): Current target voltage setting (mV, ignored).
            frequency (float): Current target frequency setting (MHz, ignored).
        """
        pass


class PIDTuningStrategy(TuningStrategy):
    """Concrete implementation of a PID-based tuning strategy for miner settings."""

    def __init__(
        self,
        kp_freq: float,
        ki_freq: float,
        kd_freq: float,
        kp_volt: float,
        ki_volt: float,
        kd_volt: float,
        min_voltage: float,
        max_voltage: float,
        min_frequency: float,
        max_frequency: float,
        voltage_step: float,
        frequency_step: float,
        setpoint: float,
        sample_interval: float,
        target_temp: float,
        power_limit: float
    ) -> None:
        """
        Initialize the PID tuning strategy with control parameters.

        Args:
            kp_freq (float): Proportional gain for frequency PID.
            ki_freq (float): Integral gain for frequency PID.
            kd_freq (float): Derivative gain for frequency PID.
            kp_volt (float): Proportional gain for voltage PID.
            ki_volt (float): Integral gain for voltage PID.
            kd_volt (float): Derivative gain for voltage PID.
            min_voltage (float): Minimum allowed voltage (mV).
            max_voltage (float): Maximum allowed voltage (mV).
            min_frequency (float): Minimum allowed frequency (MHz).
            max_frequency (float): Maximum allowed frequency (MHz).
            voltage_step (float): Voltage adjustment step size (mV).
            frequency_step (float): Frequency adjustment step size (MHz).
            setpoint (float): Target hashrate setpoint (GH/s).
            sample_interval (float): PID sample interval (seconds).
            target_temp (float): Target temperature (°C).
            power_limit (float): Power limit (W).
        """
        self.pid_freq = PID(kp_freq, ki_freq, kd_freq, setpoint=setpoint, sample_time=sample_interval)
        self.pid_volt = PID(kp_volt, ki_volt, kd_volt, setpoint=setpoint, sample_time=sample_interval)
        self.pid_freq.output_limits = (min_frequency, max_frequency)
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

    def apply_strategy(
        self,
        current_voltage: float,
        current_frequency: float,
        temp: float,
        hashrate: float,
        power: float
    ) -> Tuple[float, float]:
        """
        Calculate new voltage and frequency settings based on the current miner status.

        Args:
            current_voltage (float): Current target voltage setting (mV).
            current_frequency (float): Current target frequency setting (MHz).
            temp (float): Current temperature (°C).
            hashrate (float): Current hashrate (GH/s).
            power (float): Current power consumption (W).

        Returns:
            Tuple[float, float]: New (voltage, frequency) settings (mV, MHz).

        Example:
            >>> strategy = PIDTuningStrategy(0.2, 0.01, 0.02, 0.1, 0.01, 0.02, 1100, 1300, 400, 575, 10, 25, 525, 5, 55, 15)
            >>> new_settings = strategy.apply_strategy(1200, 485, 48, 500, 14.6)
            >>> new_settings
            (1200, 510)
        """
        freq_output = self.pid_freq(hashrate)
        volt_output = self.pid_volt(hashrate)
        proposed_frequency = round(freq_output / self.frequency_step) * self.frequency_step
        proposed_frequency = max(self.min_frequency, min(self.max_frequency, proposed_frequency))
        proposed_voltage = round(volt_output / self.voltage_step) * self.voltage_step
        proposed_voltage = max(self.min_voltage, min(self.max_voltage, proposed_voltage))

        hashrate_dropped = self.last_hashrate is not None and hashrate < self.last_hashrate
        stagnated = self.last_hashrate == hashrate
        self.drop_count = self.drop_count + 1 if hashrate_dropped else 0
        self.stagnation_count = self.stagnation_count + 1 if stagnated else 0

        new_voltage = current_voltage
        new_frequency = current_frequency

        if temp > self.target_temp:
            if current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                console.print(f"[{WARNING_COLOR}]Reducing frequency to {new_frequency}MHz due to temp {temp}°C > {self.target_temp}°C[/]")
            elif current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                console.print(f"[{WARNING_COLOR}]Reducing voltage to {new_voltage}mV due to temp {temp}°C > {self.target_temp}°C[/]")
        elif power > self.power_limit * 1.075:
            if current_voltage > self.min_voltage:
                new_voltage = current_voltage - self.voltage_step
                console.print(f"[{WARNING_COLOR}]Reducing voltage to {new_voltage}mV due to power {power}W > {self.power_limit * 1.075}W[/]")
        elif hashrate < self.pid_freq.setpoint:
            if self.drop_count >= 30 and current_frequency > self.min_frequency:
                new_frequency = current_frequency - self.frequency_step
                console.print(f"[{WARNING_COLOR}]Reducing frequency to {new_frequency}MHz due to repeated hashrate drops[/]")
            else:
                if hashrate < 0.85 * self.pid_freq.setpoint and current_voltage < self.max_voltage:
                    new_voltage = min(proposed_voltage, current_voltage + self.voltage_step)
                    console.print(f"[{SECONDARY_ACCENT}]Increasing voltage to {new_voltage}mV due to hashrate {hashrate} < {0.85 * self.pid_freq.setpoint}[/]")
                new_frequency = proposed_frequency
                console.print(f"[{SECONDARY_ACCENT}]Adjusting frequency to {new_frequency}MHz via PID[/]")
                if current_frequency >= self.max_frequency and current_voltage < self.max_voltage:
                    new_voltage = current_voltage + self.voltage_step
                    console.print(f"[{SECONDARY_ACCENT}]Increasing voltage to {new_voltage}mV as frequency at max[/]")
        else:
            console.print(f"[{PRIMARY_ACCENT}]System stable at Voltage={current_voltage}mV, Frequency={new_frequency}MHz[/]")

        self.last_hashrate = hashrate
        return new_voltage, new_frequency