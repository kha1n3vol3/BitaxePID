#!/usr/bin/env python3
"""
Implementations Module for BitaxePID Auto-Tuner

This module provides concrete implementations of interfaces for the BitaxePID
Auto-Tuner, including API client, logging, configuration loading, terminal UI,
and PID tuning strategy.
"""

# Standard library imports
import csv
import json
import os
import time
from logging import getLogger
from typing import Dict, Any, Optional, Tuple

# Third-party imports
import pyfiglet
import urllib3
import yaml
from fastdigest import TDigest
from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from simple_pid import PID
from urllib3.util.retry import Retry

# Local application imports
from interfaces import (
    IBitaxeAPIClient,
    ILogger,
    IConfigLoader,
    ITerminalUI,
    TuningStrategy,
)

# Color constants for rich console styling
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
    """API client for interacting with the Bitaxe miner.

    This class provides methods to communicate with the Bitaxe miner via its
    HTTP API, including fetching system information, setting operational
    parameters, configuring stratum servers, and restarting the device.

    Attributes:
        bitaxepid_url (str): Base URL for the Bitaxe API.
        logger (Logger): Logger instance for event logging.
        http_pool (HTTPConnectionPool): Connection pool for HTTP requests.
    """

    def __init__(
        self, ip: str, timeout: int = 15, retries: int = 5, pool_maxsize: int = 10
    ) -> None:
        """Initializes the BitaxeAPIClient with connection settings.

        Args:
            ip (str): IP address of the Bitaxe miner.
            timeout (int, optional): HTTP request timeout in seconds.
                Defaults to 15.
            retries (int, optional): Number of retries for failed requests.
                Defaults to 5.
            pool_maxsize (int, optional): Maximum size of the connection pool.
                Defaults to 10.
        """
        self.bitaxepid_url = f"http://{ip}"
        self.logger = getLogger(__name__)
        retry_strategy = Retry(
            total=retries,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
        )
        self.http_pool = urllib3.HTTPConnectionPool(
            host=ip,
            port=80,
            timeout=urllib3.Timeout(connect=timeout, read=timeout),
            maxsize=pool_maxsize,
            retries=retry_strategy,
            block=False,
        )
        self.logger.info(
            f"Initialized BitaxeAPIClient for {ip} with timeout={timeout}s, "
            f"retries={retries}, pool_maxsize={pool_maxsize}"
        )

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """Fetches system information from the Bitaxe miner.

        Returns:
            Optional[Dict[str, Any]]: System info dictionary if successful,
                None otherwise.
        """
        try:
            response = self.http_pool.request("GET", "/api/system/info")
            if response.status == 200:
                return json.loads(response.data.decode("utf-8"))
            self.logger.error(f"Failed to fetch system info: HTTP {response.status}")
            console.print(
                f"[{ERROR_COLOR}]Failed to fetch system info: "
                f"HTTP {response.status}[/]"
            )
            return None
        except Exception as e:
            self.logger.error(f"Error fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Error fetching system info: {e}[/]")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
        """Sets voltage and frequency settings on the Bitaxe miner.

        Args:
            voltage (float): Target voltage in millivolts.
            frequency (float): Target frequency in MHz.

        Returns:
            float: The frequency set, returned even on failure for consistency.
        """
        settings = {"coreVoltage": voltage, "frequency": frequency}
        try:
            response = self.http_pool.request(
                "PATCH",
                "/api/system",
                body=json.dumps(settings).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            if response.status == 200:
                self.logger.info(
                    f"Applied settings: Voltage={voltage}mV, "
                    f"Frequency={frequency}MHz"
                )
                console.print(
                    f"[{PRIMARY_ACCENT}]Applied settings: Voltage={voltage}mV, "
                    f"Frequency={frequency}MHz[/]"
                )
                time.sleep(2)  # Allow settings to stabilize
                return frequency
            self.logger.error(f"Failed to set settings: HTTP {response.status}")
            return frequency
        except Exception as e:
            self.logger.error(f"Error setting system settings: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting system settings: {e}[/]")
            return frequency

    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """Configures primary and backup stratum servers.

        Args:
            primary (Dict[str, Any]): Primary stratum settings with 'hostname',
                'port', and optional 'user'.
            backup (Dict[str, Any]): Backup stratum settings with 'hostname',
                'port', and optional 'user'.

        Returns:
            bool: True if configuration succeeds, False otherwise.
        """
        settings = {
            "stratumURL": primary["hostname"],
            "stratumPort": primary["port"],
            "fallbackStratumURL": backup["hostname"],
            "fallbackStratumPort": backup["port"],
            "stratumUser": primary.get("user", ""),
            "fallbackStratumUser": backup.get("user", ""),
        }
        try:
            response = self.http_pool.request(
                "PATCH",
                "/api/system",
                body=json.dumps(settings).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            if response.status == 200:
                self.logger.info(
                    f"Set stratum: Primary={primary['hostname']}:{primary['port']}, "
                    f"Backup={backup['hostname']}:{backup['port']}"
                )
                console.print(
                    f"[{PRIMARY_ACCENT}]Set stratum configuration successfully[/]"
                )
                time.sleep(1)  # Allow configuration to take effect
                return True
            self.logger.error(f"Failed to set stratum: HTTP {response.status}")
            return False
        except Exception as e:
            self.logger.error(f"Error setting stratum endpoints: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting stratum endpoints: {e}[/]")
            return False

    def restart(self) -> bool:
        """Restarts the Bitaxe miner and verifies it comes back online.

        Returns:
            bool: True if restart succeeds and miner responds, False otherwise.
        """
        try:
            response = self.http_pool.request("POST", "/api/system/restart")
            if response.status == 200:
                self.logger.info("Restarted Bitaxe miner")
                console.print(f"[{PRIMARY_ACCENT}]Restarted Bitaxe miner[/]")
                time.sleep(5)  # Wait for restart
                for _ in range(3):  # Retry checking if miner is back online
                    if self.get_system_info():
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
        """Closes the HTTP connection pool.

        Cleans up resources by closing the connection pool and logging the event.
        """
        self.http_pool.close()
        self.logger.info("BitaxeAPIClient connection pool closed")
        console.print(f"[{PRIMARY_ACCENT}]BitaxeAPIClient connection pool closed[/]")


class Logger(ILogger):
    """Logger for recording miner data to CSV and saving snapshots.

    Handles logging of operational data to a CSV file and saving voltage and
    frequency snapshots to a JSON file. Also supports t-digest logging for
    statistical analysis.

    Attributes:
        log_file (str): Path to the CSV log file.
        snapshot_file (str): Path to the JSON snapshot file.
    """

    def __init__(self, log_file: str, snapshot_file: str) -> None:
        """Initializes the Logger with file paths.

        Args:
            log_file (str): Path to the CSV log file.
            snapshot_file (str): Path to the JSON snapshot file.
        """
        self.log_file = log_file
        self.snapshot_file = snapshot_file
        self._initialize_csv()
        os.makedirs("./temps", exist_ok=True)  # Ensure temp directory exists

    def _initialize_csv(self) -> None:
        """Initializes the CSV log file with headers if it does not exist."""
        if not os.path.exists(self.log_file):
            headers = [
                "mac_address",
                "timestamp",
                "target_frequency",
                "target_voltage",
                "hashrate",
                "temp",
                "power",
                "board_voltage",
                "current",
                "core_voltage_actual",
                "frequency",
                "fanrpm",
                "pid_freq_kp",
                "pid_freq_ki",
                "pid_freq_kd",
                "pid_volt_kp",
                "pid_volt_ki",
                "pid_volt_kd",
                "initial_frequency",
                "min_frequency",
                "max_frequency",
                "initial_voltage",
                "min_voltage",
                "max_voltage",
                "frequency_step",
                "voltage_step",
                "target_temp",
                "sample_interval",
                "power_limit",
                "hashrate_setpoint",
            ]
            with open(self.log_file, "w", newline="") as f:
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
        fanrpm: int,
    ) -> None:
        """Logs miner data to the CSV file and updates t-digest statistics.

        Args:
            mac_address (str): MAC address of the miner.
            timestamp (str): Timestamp of the log entry.
            target_frequency (float): Target frequency in MHz.
            target_voltage (float): Target voltage in millivolts.
            hashrate (float): Current hashrate.
            temp (float): Current temperature in Celsius.
            pid_settings (Dict[str, Any]): PID controller settings.
            power (float): Current power consumption in watts.
            board_voltage (float): Measured board voltage.
            current (float): Measured current.
            core_voltage_actual (float): Actual core voltage.
            frequency (float): Actual frequency in MHz.
            fanrpm (int): Fan speed in RPM.
        """
        with open(self.log_file, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    mac_address,
                    timestamp,
                    target_frequency,
                    target_voltage,
                    hashrate,
                    temp,
                    power,
                    board_voltage,
                    current,
                    core_voltage_actual,
                    frequency,
                    fanrpm,
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
                    pid_settings.get("HASHRATE_SETPOINT", ""),
                ]
            )
        # Log metrics to t-digest files for statistical analysis
        rounded_temp = round(temp)
        metrics = {
            "power": power,
            "voltage": board_voltage,
            "current": current,
            "temp": temp,
            "coreVoltage": target_voltage,
            "coreVoltageActual": core_voltage_actual,
            "frequency": frequency,
        }
        for metric, value in metrics.items():
            if value is not None:
                filename = f"./temps/{rounded_temp}c-{metric}.json"
                tdigest = self.load_tdigest(filename)
                tdigest.update(value)
                self.save_tdigest(filename, tdigest)

    def load_tdigest(self, filename: str) -> TDigest:
        """Loads a t-digest object from a JSON file.

        Args:
            filename (str): Path to the t-digest JSON file.

        Returns:
            TDigest: The loaded t-digest object, or a new one if loading fails.
        """
        if os.path.exists(filename):
            try:
                with open(filename, "r") as f:
                    data = json.load(f)
                    return TDigest.from_dict(data)
            except Exception as e:
                console.print(
                    f"[{ERROR_COLOR}]Error loading t-digest {filename}: {e}[/]"
                )
        return TDigest()

    def save_tdigest(self, filename: str, tdigest: TDigest) -> None:
        """Saves a t-digest object to a JSON file.

        Args:
            filename (str): Path to save the t-digest JSON file.
            tdigest (TDigest): The t-digest object to save.
        """
        try:
            with open(filename, "w") as f:
                json.dump(tdigest.to_dict(), f)
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Error saving t-digest {filename}: {e}[/]")

    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """Saves a snapshot of voltage and frequency to a JSON file.

        Args:
            voltage (float): Current voltage in millivolts.
            frequency (float): Current frequency in MHz.
        """
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, "w") as f:
                json.dump(snapshot, f)
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to save snapshot: {e}[/]")


class YamlConfigLoader(IConfigLoader):
    """Loads configuration from a YAML file."""

    def load_config(self, file_path: str) -> Dict[str, Any]:
        """Loads configuration data from a specified YAML file.

        Args:
            file_path (str): Path to the YAML configuration file.

        Returns:
            Dict[str, Any]: Configuration data, or empty dict if loading fails.
        """
        try:
            with open(file_path, "r") as f:
                config = yaml.safe_load(f)
                return config if config else {}
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to load config {file_path}: {e}[/]")
            return {}


class RichTerminalUI(ITerminalUI):
    """Rich-based terminal UI for displaying miner status.

    Provides a dynamic, visually appealing interface using the Rich library to
    display system information, hashrate, and logs.

    Attributes:
        log_messages (list[str]): List of recent log messages.
        has_data (bool): Indicates if data has been received.
        sections (dict): Mapping of section names to keys for display.
        layout (Layout): Rich layout object for UI structure.
        live (Live): Rich Live object for continuous updates.
        _started (bool): Tracks if the live display has started.
    """

    def __init__(self) -> None:
        """Initializes the terminal UI with layout and sections."""
        self.log_messages: list[str] = []
        self.has_data = False
        # Define sections and their display keys
        self.sections = {
            "Network": [
                "ssid",
                "macAddr",
                "wifiStatus",
                "stratumDiff",
                "isUsingFallbackStratum",
                "stratumURL",
                "stratumPort",
                "fallbackStratumURL",
                "fallbackStratumPort",
            ],
            "Chip": ["ASICModel", "asicCount", "smallCoreCount"],
            "Power": ["power", "voltage", "current"],
            "Thermal": ["temp", "vrTemp", "overheat_mode"],
            "Mining Performance": [
                "bestDiff",
                "bestSessionDiff",
                "sharesAccepted",
                "sharesRejected",
            ],
            "System": [
                "freeHeap",
                "uptimeSeconds",
                "version",
                "idfVersion",
                "boardVersion",
            ],
            "Display & Fans": ["autofanspeed", "fanspeed", "fanrpm"],
        }
        self.layout = self.create_layout()
        self.live = Live(self.layout, console=console, refresh_per_second=1)
        self._started = False

    def show_banner(self) -> None:
        """Displays a banner from a text file if available."""
        try:
            with open("banner.txt", "r") as f:
                console.print(f.read())
            console.print("\nWaiting for miner data...", style=PRIMARY_ACCENT)
        except FileNotFoundError:
            console.print("Banner file not found", style=ERROR_COLOR)

    def create_layout(self) -> Layout:
        """Creates the Rich layout structure for the UI.

        Returns:
            Layout: Configured Rich layout object.
        """
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=7),
            Layout(name="middle"),
            Layout(name="bottom", size=3),
        )
        layout["top"].split_row(Layout(name="hashrate"), Layout(name="header"))
        layout["middle"].split_row(
            Layout(name="left_column"), Layout(name="right_column")
        )
        layout["left_column"].split_column(
            Layout(name="network"), Layout(name="chip"), Layout(name="power")
        )
        layout["right_column"].split_column(
            Layout(name="thermal"),
            Layout(name="mining_performance"),
            Layout(name="system"),
            Layout(name="display_fans"),
        )
        layout["bottom"].name = "log"
        return layout

    def update(
        self, system_info: Dict[str, Any], voltage: float, frequency: float
    ) -> None:
        """Updates the terminal UI with the latest miner data.

        Args:
            system_info (Dict[str, Any]): Current system information.
            voltage (float): Current voltage in millivolts.
            frequency (float): Current frequency in MHz.
        """
        if not self.has_data:
            console.clear()
            self.has_data = True

        # Update hashrate display
        hashrate = system_info.get("hashRate", 0)
        hashrate_str = (
            f"{hashrate:.2f} Th/s" if hashrate > 999 else f"{int(hashrate)} Gh/s"
        )
        ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
        self.layout["hashrate"].update(
            Panel(ascii_art, title="Hashrate", border_style=PRIMARY_ACCENT)
        )

        # Update header section
        header_table = Table(show_header=False, box=None)
        header_table.add_column("", style=DECORATIVE_COLOR, justify="right")
        header_table.add_column("", style=TEXT_COLOR)
        header_table.add_row("Hostname", system_info.get("hostname", "N/A"))
        header_table.add_row("Voltage", f"{int(voltage)}mV")
        header_table.add_row("Frequency", f"{int(frequency)}MHz")
        header_table.add_row("Temperature", f"{system_info.get('temp', 'N/A')}°C")
        header_table.add_row("Stratum User", system_info.get("stratumUser", "N/A"))
        header_table.add_row(
            "Backup User", system_info.get("fallbackStratumUser", "N/A")
        )
        self.layout["header"].update(Panel(header_table, title="System Status"))

        # Update section panels
        section_layouts = {
            "Network": "network",
            "Chip": "chip",
            "Power": "power",
            "Thermal": "thermal",
            "Mining Performance": "mining_performance",
            "System": "system",
            "Display & Fans": "display_fans",
        }
        for section_name, layout_name in section_layouts.items():
            table = Table(show_header=False, box=None)
            table.add_column("", style=DECORATIVE_COLOR)
            table.add_column("", style=TEXT_COLOR)
            for key in self.sections[section_name]:
                if key in system_info:
                    value = system_info[key]
                    if key in ["stratumURL", "fallbackStratumURL"]:
                        port_key = (
                            "stratumPort"
                            if key == "stratumURL"
                            else "fallbackStratumPort"
                        )
                        value = f"{value}:{system_info.get(port_key, '')}"
                    elif isinstance(value, (int, float)):
                        value = f"{int(value)}"
                    table.add_row(key, str(value))
            self.layout[layout_name].update(Panel(table, title=section_name))

        # Update log panel
        status = (
            f"{time.strftime('%Y-%m-d %H:%M:%S')} - Voltage: {int(voltage)}mV, "
            f"Frequency: {int(frequency)}MHz, Hashrate: {hashrate_str}, "
            f"Temp: {system_info.get('temp', 'N/A')}°C"
        )
        self.log_messages.append(status)
        if len(self.log_messages) > 6:
            self.log_messages.pop(0)
        self.layout["log"].update(
            Panel(Text("\n".join(self.log_messages)), title="Log")
        )

    def start(self) -> None:
        """Starts the live update of the terminal UI."""
        if not self._started:
            self.live.start()
            self._started = True

    def stop(self) -> None:
        """Stops the live update of the terminal UI."""
        if self._started:
            self.live.stop()
            self._started = False


class NullTerminalUI(ITerminalUI):
    """A no-op terminal UI implementation."""

    def update(
        self, system_info: Dict[str, Any], voltage: float, frequency: float
    ) -> None:
        """Does nothing with the provided data.

        Args:
            system_info (Dict[str, Any]): System information (ignored).
            voltage (float): Voltage value (ignored).
            frequency (float): Frequency value (ignored).
        """
        pass


class PIDTuningStrategy(TuningStrategy):
    """PID-based tuning strategy for adjusting voltage and frequency.

    Uses PID control to maintain a target temperature by adjusting frequency
    and voltage within specified bounds.

    Attributes:
        pid_freq (PID): PID controller for frequency adjustments.
        min_voltage (float): Minimum allowed voltage.
        max_voltage (float): Maximum allowed voltage.
        min_frequency (float): Minimum allowed frequency.
        max_frequency (float): Maximum allowed frequency.
        voltage_step (float): Voltage adjustment increment.
        frequency_step (float): Frequency adjustment increment.
        target_temp (float): Target temperature setpoint.
        power_limit (float): Power limit (retained for future use).
        hashrate_setpoint (float): Hashrate setpoint (retained for compatibility).
    """

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
        power_limit: float,
    ) -> None:
        """Initializes the PID tuning strategy with control parameters.

        Args:
            kp_freq (float): Proportional gain for frequency PID.
            ki_freq (float): Integral gain for frequency PID.
            kd_freq (float): Derivative gain for frequency PID.
            kp_volt (float): Proportional gain for voltage (unused currently).
            ki_volt (float): Integral gain for voltage (unused currently).
            kd_volt (float): Derivative gain for voltage (unused currently).
            min_voltage (float): Minimum voltage in millivolts.
            max_voltage (float): Maximum voltage in millivolts.
            min_frequency (float): Minimum frequency in MHz.
            max_frequency (float): Maximum frequency in MHz.
            voltage_step (float): Voltage adjustment step size.
            frequency_step (float): Frequency adjustment step size.
            setpoint (float): Hashrate setpoint (retained for compatibility).
            sample_interval (float): PID sample interval in seconds.
            target_temp (float): Target temperature in Celsius.
            power_limit (float): Power limit in watts (retained for future use).
        """
        self.pid_freq = PID(
            kp_freq, ki_freq, kd_freq, setpoint=target_temp, sample_time=sample_interval
        )
        self.pid_freq.output_limits = (min_frequency, max_frequency)
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.voltage_step = voltage_step
        self.frequency_step = frequency_step
        self.target_temp = target_temp
        self.power_limit = power_limit  # Retained for potential future use
        self.hashrate_setpoint = setpoint  # Retained for compatibility

    def apply_strategy(
        self,
        current_voltage: float,
        current_frequency: float,
        temp: float,
        hashrate: float = 0.0,
        power: float = 0.0,
    ) -> Tuple[float, float]:
        """Applies the PID tuning strategy based on current conditions.

        Adjusts frequency and voltage to maintain target temperature using PID
        control, respecting defined limits and step sizes.

        Args:
            current_voltage (float): Current voltage in millivolts.
            current_frequency (float): Current frequency in MHz.
            temp (float): Current temperature in Celsius.
            hashrate (float, optional): Current hashrate (unused). Defaults to 0.0.
            power (float, optional): Current power (unused). Defaults to 0.0.

        Returns:
            Tuple[float, float]: New voltage and frequency values.
        """
        deadband = self.target_temp * 0.1  # 10% deadband around target
        new_frequency = current_frequency
        new_voltage = current_voltage
        if abs(temp - self.target_temp) > deadband:
            desired_frequency = self.pid_freq(temp)
            new_frequency = (
                round(desired_frequency / self.frequency_step) * self.frequency_step
            )
            new_frequency = max(
                self.min_frequency, min(self.max_frequency, new_frequency)
            )
            upper_limit = self.target_temp * 1.1  # 10% above target
            lower_limit = self.target_temp * 0.9  # 10% below target
            if temp > upper_limit and new_frequency == self.min_frequency:
                new_voltage = max(self.min_voltage, current_voltage - self.voltage_step)
                console.print(
                    f"[{WARNING_COLOR}]Reducing voltage to {new_voltage}mV "
                    f"due to temp {temp}°C > {upper_limit}°C[/]"
                )
            elif temp < lower_limit and new_frequency == self.max_frequency:
                new_voltage = min(self.max_voltage, current_voltage + self.voltage_step)
                console.print(
                    f"[{SECONDARY_ACCENT}]Increasing voltage to {new_voltage}mV "
                    f"due to temp {temp}°C < {lower_limit}°C[/]"
                )
        else:
            console.print(
                f"[{PRIMARY_ACCENT}]Temperature {temp}°C within ±10% of "
                f"target {self.target_temp}°C, no adjustment[/]"
            )
        return new_voltage, new_frequency
