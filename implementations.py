#!/usr/bin/env python3
"""
Implementations Module for BitaxePID Auto-Tuner

This module provides concrete implementations of interfaces for the BitaxePID
Auto-Tuner, including API client, logging, configuration loading, terminal UI,
and PID tuning strategy.
"""

import csv
import json
import os
import time
from logging import getLogger
from typing import Dict, Any, Optional, Tuple

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
    def __init__(
        self, ip: str, timeout: int = 15, retries: int = 5, pool_maxsize: int = 10
    ) -> None:
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
        try:
            response = self.http_pool.request("GET", "/api/system/info")
            if response.status == 200:
                return json.loads(response.data.decode("utf-8"))
            self.logger.error(f"Failed to fetch system info: HTTP {response.status}")
            console.print(
                f"[{ERROR_COLOR}]Failed to fetch system info: HTTP {response.status}[/]"
            )
            return None
        except Exception as e:
            self.logger.error(f"Error fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Error fetching system info: {e}[/]")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
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
                    f"Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz"
                )
                console.print(
                    f"[{PRIMARY_ACCENT}]Applied settings: Voltage={voltage}mV, "
                    f"Frequency={frequency}MHz[/]"
                )
                time.sleep(2)
                return frequency
            self.logger.error(f"Failed to set settings: HTTP {response.status}")
            return frequency
        except Exception as e:
            self.logger.error(f"Error setting system settings: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting system settings: {e}[/]")
            return frequency

    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
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
                time.sleep(1)
                return True
            self.logger.error(f"Failed to set stratum: HTTP {response.status}")
            return False
        except Exception as e:
            self.logger.error(f"Error setting stratum endpoints: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting stratum endpoints: {e}[/]")
            return False

    def restart(self) -> bool:
        try:
            response = self.http_pool.request("POST", "/api/system/restart")
            if response.status == 200:
                self.logger.info("Restarted Bitaxe miner")
                console.print(f"[{PRIMARY_ACCENT}]Restarted Bitaxe miner[/]")
                time.sleep(5)
                for _ in range(3):
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
        self.http_pool.close()
        self.logger.info("BitaxeAPIClient connection pool closed")
        console.print(f"[{PRIMARY_ACCENT}]BitaxeAPIClient connection pool closed[/]")


class Logger(ILogger):
    def __init__(self, log_file: str, snapshot_file: str) -> None:
        self.log_file = log_file
        self.snapshot_file = snapshot_file
        self._initialize_csv()
        os.makedirs("./temps", exist_ok=True)

    def _initialize_csv(self) -> None:
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
                "median_temp",
                "median_power",
                "recommended_voltage",
                "recommended_frequency",
                "P_freq",
                "I_freq",
                "D_freq",
                "P_volt",
                "I_volt",
                "D_volt",
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
        median_temp: Optional[float] = None,
        median_power: Optional[float] = None,
        recommended_voltage: Optional[float] = None,
        recommended_frequency: Optional[float] = None,
        pid_freq_terms: Optional[Tuple[float, float, float]] = None,
        pid_volt_terms: Optional[Tuple[float, float, float]] = None,
    ) -> None:
        """
        Log miner performance data to CSV, including PID outputs and terms.

        Args:
            mac_address (str): MAC address of the miner.
            timestamp (str): Timestamp of the log entry.
            target_frequency (float): Current target frequency (MHz).
            target_voltage (float): Current target voltage (mV).
            hashrate (float): Current hashrate.
            temp (float): Current temperature (°C).
            pid_settings (Dict[str, Any]): PID controller settings.
            power (float): Current power consumption (watts).
            board_voltage (float): Measured board voltage.
            current (float): Measured current.
            core_voltage_actual (float): Actual core voltage.
            frequency (float): Actual frequency (MHz).
            fanrpm (int): Fan speed (RPM).
            median_temp (Optional[float]): Median temperature over 60 seconds.
            median_power (Optional[float]): Median power over 60 seconds.
            recommended_voltage (Optional[float]): Recommended voltage from PID.
            recommended_frequency (Optional[float]): Recommended frequency from PID.
            pid_freq_terms (Optional[Tuple]): P, I, D terms for frequency PID.
            pid_volt_terms (Optional[Tuple]): P, I, D terms for voltage PID.
        """
        with open(self.log_file, "a", newline="") as f:
            writer = csv.writer(f)
            P_freq = I_freq = D_freq = P_volt = I_volt = D_volt = ""
            if pid_freq_terms:
                P_freq, I_freq, D_freq = pid_freq_terms
            if pid_volt_terms:
                P_volt, I_volt, D_volt = pid_volt_terms
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
                    median_temp if median_temp is not None else "",
                    median_power if median_power is not None else "",
                    recommended_voltage if recommended_voltage is not None else "",
                    recommended_frequency if recommended_frequency is not None else "",
                    P_freq,
                    I_freq,
                    D_freq,
                    P_volt,
                    I_volt,
                    D_volt,
                ]
            )

    def save_snapshot(self, voltage: float, frequency: float) -> None:
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, "w") as f:
                json.dump(snapshot, f)
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to save snapshot: {e}[/]")


class YamlConfigLoader(IConfigLoader):
    def load_config(self, file_path: str) -> Dict[str, Any]:
        try:
            with open(file_path, "r") as f:
                config = yaml.safe_load(f)
                return config if config else {}
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to load config {file_path}: {e}[/]")
            return {}


class RichTerminalUI(ITerminalUI):
    def __init__(self) -> None:
        self.log_messages: list[str] = []
        self.has_data = False
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
        try:
            with open("banner.txt", "r") as f:
                console.print(f.read())
            console.print("\nWaiting for miner data...", style=PRIMARY_ACCENT)
        except FileNotFoundError:
            console.print("Banner file not found", style=ERROR_COLOR)

    def create_layout(self) -> Layout:
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
        if not self.has_data:
            console.clear()
            self.has_data = True
        hashrate = system_info.get("hashRate", 0)
        hashrate_str = (
            f"{hashrate:.2f} Th/s" if hashrate > 999 else f"{int(hashrate)} Gh/s"
        )
        ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
        self.layout["hashrate"].update(
            Panel(ascii_art, title="Hashrate", border_style=PRIMARY_ACCENT)
        )
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
        if not self._started:
            self.live.start()
            self._started = True

    def stop(self) -> None:
        if self._started:
            self.live.stop()
            self._started = False


class NullTerminalUI(ITerminalUI):
    def update(
        self, system_info: Dict[str, Any], voltage: float, frequency: float
    ) -> None:
        pass


class PIDTuningStrategy(TuningStrategy):
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
        target_temp: float,
        power_limit: float,
    ) -> None:
        """
        Initialize the dual PID tuning strategy for frequency and voltage.

        Args:
            kp_freq (float): Proportional gain for frequency PID.
            ki_freq (float): Integral gain for frequency PID.
            kd_freq (float): Derivative gain for frequency PID.
            kp_volt (float): Proportional gain for voltage PID.
            ki_volt (float): Integral gain for voltage PID.
            kd_volt (float): Derivative gain for voltage PID.
            min_voltage (float): Minimum voltage (mV).
            max_voltage (float): Maximum voltage (mV).
            min_frequency (float): Minimum frequency (MHz).
            max_frequency (float): Maximum frequency (MHz).
            voltage_step (float): Voltage adjustment step size (mV).
            frequency_step (float): Frequency adjustment step size (MHz).
            target_temp (float): Target temperature setpoint (°C).
            power_limit (float): Maximum power limit setpoint (watts).
        """
        # Frequency PID to regulate temperature
        self.pid_freq = PID(
            kp_freq, ki_freq, kd_freq, setpoint=target_temp, sample_time=60
        )
        self.pid_freq.output_limits = (min_frequency, max_frequency)
        # Voltage PID to manage power consumption
        self.pid_volt = PID(
            kp_volt, ki_volt, kd_volt, setpoint=power_limit, sample_time=60
        )
        self.pid_volt.output_limits = (min_voltage, max_voltage)
        self.min_voltage = min_voltage
        self.max_voltage = max_voltage
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.voltage_step = voltage_step
        self.frequency_step = frequency_step
        self.target_temp = target_temp
        self.power_limit = power_limit

    def apply_strategy(
        self,
        current_voltage: float,
        current_frequency: float,
        temp: float,
        power: float,
    ) -> Tuple[float, float, Tuple[float, float, float], Tuple[float, float, float]]:
        """
        Apply the dual PID strategy to adjust frequency and voltage.

        Frequency is adjusted to maintain target temperature, and voltage is
        adjusted to keep power at or below the power limit. Adjustments are
        based on median values collected over 60 seconds.

        Args:
            current_voltage (float): Current voltage (mV).
            current_frequency (float): Current frequency (MHz).
            temp (float): Median temperature over 60 seconds (°C).
            power (float): Median power over 60 seconds (watts).

        Returns:
            Tuple containing:
            - float: New voltage (mV).
            - float: New frequency (MHz).
            - Tuple[float, float, float]: Frequency PID terms (P, I, D).
            - Tuple[float, float, float]: Voltage PID terms (P, I, D).
        """
        # Compute new frequency to regulate temperature
        new_frequency = self.pid_freq(temp)
        new_frequency = round(new_frequency / self.frequency_step) * self.frequency_step
        new_frequency = max(self.min_frequency, min(self.max_frequency, new_frequency))

        # Compute new voltage to manage power consumption
        new_voltage = self.pid_volt(power)
        new_voltage = round(new_voltage / self.voltage_step) * self.voltage_step
        new_voltage = max(self.min_voltage, min(self.max_voltage, new_voltage))

        # Get PID terms for logging and analysis
        pid_freq_terms = self.pid_freq.components
        pid_volt_terms = self.pid_volt.components

        return new_voltage, new_frequency, pid_freq_terms, pid_volt_terms
