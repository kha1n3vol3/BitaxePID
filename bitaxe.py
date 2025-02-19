#!/usr/bin/env python3
"""
Bitaxe 601 Gamma Auto-Tuner

This module provides an automated tuning system for the Bitaxe 601 Gamma Bitcoin miner.
It adjusts voltage and frequency settings to optimize hashrate while respecting temperature
and power constraints. The system supports two modes: normal PID-based optimization and
temp-watch mode for temperature control. It features a rich Terminal User Interface (TUI)
for real-time monitoring and logs tuning data to CSV and JSON files.

Usage:
    Run the script with the Bitaxe IP address as an argument:
    `python bitaxe_tuner.py 192.168.68.111 [options]`

    Use optional arguments to customize initial settings (e.g., voltage, frequency,
    target temperature). See `parse_arguments` for details.

Dependencies:
    - requests: For HTTP communication with the Bitaxe API
    - simple_pid: For PID control
    - rich: For TUI rendering
    - pyfiglet: For ASCII art in the TUI
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from typing import Dict, Optional, Any, Tuple

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

# Constants
SUCCESS_COLOR = "#00ff00"  # NEON_GREEN
WARNING_COLOR = "#ff9900"  # NEON_ORANGE
CRITICAL_COLOR = "#ff0000"  # Red for errors
TEXT_GREY = "#cccccc"
WHITE = "#ffffff"
NEON_CYAN = "#00ffff"
NEON_GREEN = "#00ff00"
NEON_PINK = "#ff0099"
DARK_GREY = "#121212"
MID_GREY = "#2a2a2a"
BLACK = "#000000"

DEFAULTS = {
    "INITIAL_VOLTAGE": 1200,
    "INITIAL_FREQUENCY": 450,
    "TARGET_TEMP": 45,
    "SAMPLE_INTERVAL": 10,
    "POWER_LIMIT": 15,
    "HASHRATE_SETPOINT": 450,
    "MIN_VOLTAGE": 1100,
    "MAX_VOLTAGE": 2400,
    "MIN_FREQUENCY": 400,
    "MAX_FREQUENCY": 550,
    "VOLTAGE_STEP": 10,
    "FREQUENCY_STEP": 25,
    "PID_FREQ_KP": 0.1,
    "PID_FREQ_KI": 0.01,
    "PID_FREQ_KD": 0.05,
    "PID_VOLT_KP": 0.05,
    "PID_VOLT_KI": 0.005,
    "PID_VOLT_KD": 0.02
}

SNAPSHOT_FILE = "bitaxe_snapshot.json"
LOG_FILE = "bitaxe_tuning_log.csv"

# Global State
console = Console()
logger = logging.getLogger(__name__)
running = True
log_messages = []
last_hashrate = None
stagnation_count = 0
drop_count = 0

class BitaxeTuner:
    """Manages the tuning process for a Bitaxe 601 Gamma miner."""

    def __init__(self, bitaxe_ip: str, args: argparse.Namespace) -> None:
        """
        Initialize the Bitaxe tuner with configuration and PID controllers.

        Args:
            bitaxe_ip (str): The IP address of the Bitaxe miner (e.g., "192.168.68.111").
            args (argparse.Namespace): Command-line arguments parsed by `parse_arguments`.
        """
        self.bitaxe_url = f"http://{bitaxe_ip}"
        self.args = args
        self.target_temp = args.target_temp
        self.sample_interval = args.interval
        self.power_limit = args.power_limit
        self.hashrate_setpoint = args.setpoint
        self.temp_watch = args.temp_watch
        self.log_to_console = args.log_to_console
        self.current_voltage = float(args.voltage)
        self.current_frequency = float(args.frequency)
        self._load_snapshot()
        self._setup_logging()
        self._setup_pid_controllers()

    def _load_snapshot(self) -> None:
        """Load previous settings from snapshot file if available and no overrides provided."""
        if os.path.exists(SNAPSHOT_FILE) and not self.args.voltage and not self.args.frequency:
            try:
                with open(SNAPSHOT_FILE, 'r') as f:
                    snapshot = json.load(f)
                    self.current_voltage = float(snapshot.get("voltage", DEFAULTS["INITIAL_VOLTAGE"]))
                    self.current_frequency = float(snapshot.get("frequency", DEFAULTS["INITIAL_FREQUENCY"]))
            except Exception as e:
                logger.error(f"Failed to load snapshot: {e}")
                console.print(f"[{WARNING_COLOR}]Failed to load snapshot: {e}[/]")

    def _setup_logging(self) -> None:
        """Configure logging to file and optionally to console."""
        handlers = [logging.FileHandler("bitaxe_monitor.log")]
        if self.log_to_console:
            handlers.append(logging.StreamHandler())
        else:
            handlers.append(logging.NullHandler())
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=handlers
        )

    def _setup_pid_controllers(self) -> None:
        """Initialize PID controllers for frequency and voltage adjustments."""
        self.pid_freq = PID(
            DEFAULTS["PID_FREQ_KP"], DEFAULTS["PID_FREQ_KI"], DEFAULTS["PID_FREQ_KD"],
            setpoint=self.hashrate_setpoint, sample_time=self.sample_interval
        )
        self.pid_freq.output_limits = (DEFAULTS["MIN_FREQUENCY"], DEFAULTS["MAX_FREQUENCY"])
        self.pid_volt = PID(
            DEFAULTS["PID_VOLT_KP"], DEFAULTS["PID_VOLT_KI"], DEFAULTS["PID_VOLT_KD"],
            setpoint=self.hashrate_setpoint, sample_time=self.sample_interval
        )
        self.pid_volt.output_limits = (DEFAULTS["MIN_VOLTAGE"], DEFAULTS["MAX_VOLTAGE"])

    def save_snapshot(self) -> None:
        """Save current voltage and frequency settings to a JSON snapshot file."""
        snapshot = {"voltage": self.current_voltage, "frequency": self.current_frequency}
        try:
            with open(SNAPSHOT_FILE, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            logger.error(f"Failed to save snapshot: {e}")

    def log_tuning_data(self, timestamp: str, hashrate: float, temp: float) -> None:
        """
        Log tuning data to a CSV file.

        Args:
            timestamp (str): Current timestamp in "YYYY-MM-DD HH:MM:SS" format.
            hashrate (float): Current hashrate in GH/s.
            temp (float): Current temperature in °C.
        """
        file_exists = os.path.isfile(LOG_FILE)
        with open(LOG_FILE, 'a', newline='') as csvfile:
            fieldnames = ["timestamp", "frequency_mhz", "voltage_mv", "hashrate_ghs", "temperature_c"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": timestamp,
                "frequency_mhz": self.current_frequency,
                "voltage_mv": self.current_voltage,
                "hashrate_ghs": hashrate,
                "temperature_c": temp
            })

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """
        Fetch system information from the Bitaxe API.

        Returns:
            Optional[Dict[str, Any]]: System info dictionary or None if the request fails.
        """
        try:
            response = requests.get(f"{self.bitaxe_url}/api/system/info", timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching system info: {e}")
            if not self.log_to_console:
                console.print(f"[{WARNING_COLOR}]Error fetching system info: {e}[/]")
            return None

    def set_system_settings(self, core_voltage: float, frequency: float) -> float:
        """
        Apply voltage and frequency settings to the Bitaxe miner.

        Args:
            core_voltage (float): Target core voltage in mV.
            frequency (float): Target frequency in MHz.

        Returns:
            float: Adjusted frequency applied to the system.

        Example:
            >>> tuner = BitaxeTuner("192.168.68.111", args)
            >>> tuner.set_system_settings(1200, 450)
            450.0
        """
        frequency = round(frequency / DEFAULTS["FREQUENCY_STEP"]) * DEFAULTS["FREQUENCY_STEP"]
        frequency = max(DEFAULTS["MIN_FREQUENCY"], min(DEFAULTS["MAX_FREQUENCY"], frequency))
        core_voltage = max(DEFAULTS["MIN_VOLTAGE"], min(DEFAULTS["MAX_VOLTAGE"], core_voltage))
        settings = {"coreVoltage": core_voltage, "frequency": frequency}
        try:
            response = requests.patch(f"{self.bitaxe_url}/api/system", json=settings, timeout=10)
            response.raise_for_status()
            logger.info(f"Applied settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz")
            if not self.log_to_console:
                console.print(f"[{SUCCESS_COLOR}]Applying settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz[/]")
            time.sleep(2)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error setting system settings: {e}")
            if not self.log_to_console:
                console.print(f"[{WARNING_COLOR}]Error setting system settings: {e}[/]")
        return frequency

    def create_layout(self) -> Layout:
        """Create the rich TUI layout structure."""
        layout = Layout()
        layout.split_column(Layout(name="top", size=15), Layout(name="bottom", ratio=1))
        layout["top"].split_row(Layout(name="hashrate", ratio=3), Layout(name="header", ratio=2))
        layout["bottom"].split_column(Layout(name="main", ratio=1), Layout(name="log", size=10))
        layout["main"].split_row(Layout(name="stats", ratio=1), Layout(name="progress", ratio=2))
        return layout

    def update_tui(self, layout: Layout, info: Dict[str, Any], hash_rate: float) -> None:
        """
        Update the Terminal User Interface with current system stats.

        Args:
            layout (Layout): The rich layout object to update.
            info (Dict[str, Any]): System info from the Bitaxe API.
            hash_rate (float): Current hashrate in GH/s.
        """
        temp = info.get("temp", "N/A")
        power = info.get("power", 0)
        hostname = info.get("hostname", "Unknown")
        frequency = info.get("frequency", 0)
        core_voltage = info.get("coreVoltageActual", 0)
        voltage = info.get("voltage", 0)

        # Hashrate Panel
        hashrate_str = f"{hash_rate:.0f} GH/s"
        ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
        hashrate_text = Text(ascii_art, style=NEON_GREEN, overflow="crop")
        layout["hashrate"].update(Panel(hashrate_text, title="Hashrate", border_style=NEON_GREEN, style=f"on {BLACK}"))

        # Header with Power Info as a Table
        mode = " [TEMP-WATCH]" if self.temp_watch else ""
        header_table = Table(box=box.SIMPLE, style=NEON_PINK, title=f"Bitaxe 601 Gamma Auto-Tuner (Host: {hostname}){mode}")
        header_table.add_column("Parameter", style=f"bold {NEON_PINK}", justify="right")
        header_table.add_column("Value", style=f"bold {WHITE}")

        power_str = f"{power:.2f}W" + (f" [{WARNING_COLOR}](OVER LIMIT)[/]" if power > self.power_limit else "")
        header_table.add_row("Power", power_str)
        header_table.add_row("Current", f"{info.get('current', 0):.2f}mA")
        header_table.add_row("Core Voltage", f"{core_voltage:.2f}mV")
        header_table.add_row("Voltage", f"{voltage:.0f}mV")

        layout["header"].update(Panel(header_table, style=f"on {DARK_GREY}", border_style=NEON_PINK))

        # Stats Table (Alphabetized, without power-related stats)
        stats_table = Table(box=box.SIMPLE, style=TEXT_GREY)
        stats_table.add_column("Parameter", style=f"bold {TEXT_GREY}")
        stats_table.add_column("Value", style=f"bold {WHITE}")

        stats = {
            "ASIC Model": info.get("ASICModel", "N/A"),
            "Best Diff": info.get("bestDiff", "N/A"),
            "Best Session Diff": info.get("bestSessionDiff", "N/A"),
            "Fan RPM": f"{info.get('fanrpm', 0):.0f}",
            "Fanspeed": f"{info.get('fanspeed', 0)}%",
            "Free Heap": f"{info.get('freeHeap', 0) / (1024 * 1024):.2f} MB",
            "Frequency": f"{frequency:.2f}MHz",
            "Hashrate": f"{hash_rate:.2f} GH/s",
            "MAC Address": info.get("macAddr", "N/A"),
            "SSID": info.get("ssid", "N/A"),
            "Stratum URL": info.get("stratumURL", "N/A"),
            "Temperature": f"{temp if temp == 'N/A' else float(temp):.2f}°C" +
                           (f" [{CRITICAL_COLOR}](OVERHEAT)[/]" if temp != "N/A" and float(temp) > self.target_temp else ""),
            "Uptime": time.strftime("%H:%M:%S", time.gmtime(info.get("uptimeSeconds", 0))),
            "Version": info.get("version", "N/A"),
            "WiFi Status": info.get("wifiStatus", "N/A"),
            "Shares": f"{info.get('sharesAccepted', 0):.0f} / {info.get('sharesRejected', 0):.0f}"
        }

        for param, value in sorted(stats.items()):
            stats_table.add_row(param, str(value))

        layout["stats"].update(Panel(stats_table, title="System Stats", border_style=NEON_CYAN))

        # Progress Bars
        progress = Progress(TextColumn("{task.description}", style=WHITE), BarColumn(bar_width=40, complete_style=NEON_GREEN),
                            TextColumn("{task.percentage:>3.0f}%"))
        progress.add_task(f"Hashrate (GH/s): {hash_rate:.2f}", total=self.hashrate_setpoint, completed=hash_rate)
        progress.add_task(f"Voltage (mV): {self.current_voltage:.2f}", total=DEFAULTS["MAX_VOLTAGE"], completed=self.current_voltage)
        progress.add_task(f"Frequency (MHz): {self.current_frequency:.2f}", total=DEFAULTS["MAX_FREQUENCY"], completed=self.current_frequency)
        layout["progress"].update(Panel(progress, title="Performance", border_style=NEON_GREEN))

        # Log Panel
        log_text = Text("\n".join(log_messages[-8:]), style=f"{TEXT_GREY} on {BLACK}")
        layout["log"].update(Panel(log_text, title="Log", border_style=MID_GREY))


    def monitor_and_adjust(self) -> None:
        """Main loop to monitor and adjust Bitaxe settings."""
        global running, log_messages, last_hashrate, stagnation_count, drop_count
        logger.info(f"Starting Bitaxe Monitor. Target temp: {self.target_temp}°C, "
                    f"Target hashrate: {self.hashrate_setpoint} GH/s, "
                    f"Initial Voltage: {self.current_voltage}mV, "
                    f"Initial Frequency: {self.current_frequency}MHz, Temp-watch: {self.temp_watch}")
        if not self.log_to_console:
            console.print(f"[{SUCCESS_COLOR}]Starting Bitaxe Monitor. Target temp: {self.target_temp}°C, "
                          f"Target hashrate: {self.hashrate_setpoint} GH/s, Temp-watch: {self.temp_watch}[/]")

        self.current_frequency = self.set_system_settings(self.current_voltage, self.current_frequency)
        layout = self.create_layout() if not self.log_to_console else None
        live = Live(layout, console=console, refresh_per_second=1) if not self.log_to_console else None
        if live:
            live.start()

        while running:
            try:
                info = self.get_system_info()
                if info is None:
                    log_messages.append("Failed to fetch system info, retrying...")
                    if live:
                        self.update_tui(layout, {}, 0)
                    time.sleep(self.sample_interval)
                    continue

                temp = info.get("temp", "N/A")
                hash_rate = info.get("hashRate", 0)
                power = info.get("power", 0)
                temp_float = float(temp) if temp != "N/A" else self.target_temp + 1
                timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                self.log_tuning_data(timestamp, hash_rate, temp_float)

                status = (f"Temp: {temp}°C | Hashrate: {hash_rate:.2f} GH/s | Power: {power}W | "
                          f"Current Settings -> Voltage: {self.current_voltage}mV, Frequency: {self.current_frequency}MHz")
                logger.info(status)
                log_messages.append(status)
                if live:
                    self.update_tui(layout, info, hash_rate)

                if self.temp_watch:
                    self._adjust_temp_watch(temp_float)
                else:
                    self._adjust_normal_mode(temp_float, hash_rate, power)

                self.current_frequency = self.set_system_settings(self.current_voltage, self.current_frequency)
                self.save_snapshot()
                last_hashrate = hash_rate
                time.sleep(self.sample_interval)
            except Exception as e:
                logger.error(f"Unexpected error in monitor loop: {e}")
                if not self.log_to_console:
                    console.print(f"[{WARNING_COLOR}]Unexpected error in monitor loop: {e}. Continuing...[/]")
                time.sleep(self.sample_interval)  # Prevent rapid crash looping

        if live:
            live.stop()

    def _adjust_temp_watch(self, temp_float: float) -> None:
        """Adjust settings in temp-watch mode to control temperature."""
        if temp_float > self.target_temp:
            logger.warning(f"Temp {temp_float}°C exceeds target {self.target_temp}°C")
            log_messages.append(f"Temp-watch: Temp {temp_float}°C > {self.target_temp}°C")
            if not self.log_to_console:
                console.print(f"[{WARNING_COLOR}]Temp {temp_float}°C exceeds target {self.target_temp}°C. Lowering settings.[/]")
            if self.current_frequency > DEFAULTS["MIN_FREQUENCY"]:
                self.current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                logger.info(f"Temp-watch: Reducing frequency to {self.current_frequency}MHz")
                log_messages.append(f"Temp-watch: Frequency reduced to {self.current_frequency}MHz")
            elif self.current_voltage > DEFAULTS["MIN_VOLTAGE"]:
                self.current_voltage -= DEFAULTS["VOLTAGE_STEP"]
                logger.info(f"Temp-watch: Reducing voltage to {self.current_voltage}mV")
                log_messages.append(f"Temp-watch: Voltage reduced to {self.current_voltage}mV")

    def _adjust_normal_mode(self, temp_float: float, hash_rate: float, power: float) -> None:
        """Adjust settings in normal mode using PID controllers."""
        global stagnation_count, drop_count
        try:
            freq_output = self.pid_freq(hash_rate)
            volt_output = self.pid_volt(hash_rate)
            proposed_frequency = round(freq_output / DEFAULTS["FREQUENCY_STEP"]) * DEFAULTS["FREQUENCY_STEP"]
            proposed_frequency = max(DEFAULTS["MIN_FREQUENCY"], min(DEFAULTS["MAX_FREQUENCY"], proposed_frequency))
            proposed_voltage = round(volt_output / DEFAULTS["VOLTAGE_STEP"]) * DEFAULTS["VOLTAGE_STEP"]
            proposed_voltage = max(DEFAULTS["MIN_VOLTAGE"], min(DEFAULTS["MAX_VOLTAGE"], proposed_voltage))

            hashrate_dropped = last_hashrate is not None and hash_rate < last_hashrate
            stagnated = last_hashrate == hash_rate

            if hashrate_dropped:
                drop_count += 1
            else:
                drop_count = 0
            if stagnated:
                stagnation_count += 1
            else:
                stagnation_count = 0

            if stagnation_count >= 3:
                logger.info("Hashrate stagnated, resetting PID controllers")
                log_messages.append("Hashrate stagnated, resetting PID...")
                self.pid_freq.reset()
                self.pid_volt.reset()
                stagnation_count = 0

            # Handle constraints
            if temp_float > self.target_temp or power > self.power_limit * 1.075:
                logger.warning(f"Constraint exceeded - Temp: {temp_float}°C > {self.target_temp}°C or Power: {power}W > {self.power_limit * 1.075}W")
                log_messages.append(f"Constraint exceeded: Temp {temp_float}°C, Power {power}W")
                if not self.log_to_console:
                    console.print(f"[{WARNING_COLOR}]Constraints exceeded! Lowering settings.[/]")
                if power > self.power_limit * 1.075 and self.current_voltage > DEFAULTS["MIN_VOLTAGE"]:
                    self.current_voltage -= DEFAULTS["VOLTAGE_STEP"]
                    logger.info(f"Power exceeded, reducing voltage to {self.current_voltage}mV")
                    log_messages.append(f"Power exceeded, voltage reduced to {self.current_voltage}mV")
                elif temp_float > self.target_temp:
                    if self.current_frequency > DEFAULTS["MIN_FREQUENCY"] and drop_count < 3:
                        self.current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                        logger.info(f"Reducing frequency to {self.current_frequency}MHz due to temperature")
                        log_messages.append(f"Frequency reduced to {self.current_frequency}MHz")
                    elif self.current_voltage > DEFAULTS["MIN_VOLTAGE"]:
                        self.current_voltage -= DEFAULTS["VOLTAGE_STEP"]
                        logger.info(f"Frequency at minimum, reducing voltage to {self.current_voltage}mV due to temperature")
                        log_messages.append(f"Frequency at min, voltage reduced to {self.current_voltage}mV")
                    else:
                        logger.info("Both frequency and voltage at minimum, no further adjustments possible")
                        log_messages.append("At minimum settings, no further adjustments")
            # Optimize hashrate if no constraints
            elif hash_rate < self.hashrate_setpoint:
                if drop_count >= 3 and self.current_frequency > DEFAULTS["MIN_FREQUENCY"]:
                    self.current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                    logger.info(f"Consistent hashrate drop, reducing frequency to {self.current_frequency}MHz")
                    log_messages.append(f"Consistent drop, frequency reduced to {self.current_frequency}MHz")
                else:
                    if hash_rate < 0.85 * self.hashrate_setpoint and self.current_voltage < DEFAULTS["MAX_VOLTAGE"]:
                        self.current_voltage = min(proposed_voltage, self.current_voltage + DEFAULTS["VOLTAGE_STEP"])
                        logger.info(f"Hashrate low, boosting voltage to {self.current_voltage}mV")
                        log_messages.append(f"Hashrate low, voltage boosted to {self.current_voltage}mV")
                    if self.current_voltage >= 1150:
                        self.current_frequency = proposed_frequency
                        logger.info(f"PID adjusted frequency to {self.current_frequency}MHz")
                        log_messages.append(f"Frequency adjusted to {self.current_frequency}MHz")
                    if self.current_frequency >= DEFAULTS["MAX_FREQUENCY"] and self.current_voltage < DEFAULTS["MAX_VOLTAGE"]:
                        self.current_voltage += DEFAULTS["VOLTAGE_STEP"]
                        logger.info(f"Max frequency reached, increasing voltage to {self.current_voltage}mV")
                        log_messages.append(f"Max frequency, voltage increased to {self.current_voltage}mV")
            else:
                logger.info("System stable, maintaining settings")
                log_messages.append("System stable, maintaining settings")
                if not self.log_to_console:
                    console.print(f"[{SUCCESS_COLOR}]Stable. No adjustment needed.[/]")
        except Exception as e:
            logger.error(f"Error in PID adjustment: {e}")
            log_messages.append(f"PID adjustment error: {e}")

def parse_arguments() -> argparse.Namespace:
    """
    Parse command-line arguments for the Bitaxe tuner.

    Returns:
        argparse.Namespace: Parsed arguments.

    Example:
        >>> args = parse_arguments()
        >>> args.bitaxe_ip
        '192.168.68.111'
    """
    parser = argparse.ArgumentParser(description="Bitaxe 601 Gamma Auto-Tuner")
    parser.add_argument("bitaxe_ip", type=str, help="IP address of the Bitaxe (e.g., 192.168.68.111)")
    parser.add_argument("-v", "--voltage", type=int, default=DEFAULTS["INITIAL_VOLTAGE"],
                        help=f"Initial voltage in mV (default: {DEFAULTS['INITIAL_VOLTAGE']})")
    parser.add_argument("-f", "--frequency", type=int, default=DEFAULTS["INITIAL_FREQUENCY"],
                        help=f"Initial frequency in MHz (default: {DEFAULTS['INITIAL_FREQUENCY']})")
    parser.add_argument("-t", "--target_temp", type=float, default=DEFAULTS["TARGET_TEMP"],
                        help=f"Target temp in °C (default: {DEFAULTS['TARGET_TEMP']})")
    parser.add_argument("-i", "--interval", type=int, default=DEFAULTS["SAMPLE_INTERVAL"],
                        help=f"Sample interval in seconds (default: {DEFAULTS['SAMPLE_INTERVAL']})")
    parser.add_argument("-p", "--power_limit", type=float, default=DEFAULTS["POWER_LIMIT"],
                        help=f"Power limit in W (default: {DEFAULTS['POWER_LIMIT']})")
    parser.add_argument("-s", "--setpoint", type=float, default=DEFAULTS["HASHRATE_SETPOINT"],
                        help=f"Target hashrate in GH/s (default: {DEFAULTS['HASHRATE_SETPOINT']})")
    parser.add_argument("--temp-watch", action="store_true",
                        help="Enable temp-watch mode to only adjust frequency/voltage to control temp")
    parser.add_argument("--log-to-console", action="store_true",
                        help="Log to console only (disables TUI)")
    return parser.parse_args()

def handle_sigint(signum: int, frame: Optional[Any], tuner: BitaxeTuner) -> None:
    """
    Handle SIGINT (Ctrl+C) to gracefully exit the program.

    Args:
        signum (int): Signal number.
        frame (Optional[Any]): Current stack frame.
        tuner (BitaxeTuner): The tuner instance to save state.
    """
    global running
    tuner.save_snapshot()
    logger.info("Received SIGINT, exiting Bitaxe Monitor")
    if not tuner.log_to_console:
        console.print(f"[{WARNING_COLOR}]Exiting Bitaxe Monitor.[/]")
    running = False

def main() -> None:
    """Entry point for the Bitaxe tuner script."""
    args = parse_arguments()
    tuner = BitaxeTuner(args.bitaxe_ip, args)
    signal.signal(signal.SIGINT, lambda signum, frame: handle_sigint(signum, frame, tuner))

    try:
        tuner.monitor_and_adjust()
    except Exception as e:
        logger.error(f"Unexpected error in main: {e}")
        if not tuner.log_to_console:
            console.print(f"[{WARNING_COLOR}]An unexpected error occurred: {e}[/]")
    finally:
        tuner.save_snapshot()
        logger.info("Exiting monitor")
        if not tuner.log_to_console:
            console.print(f"[{SUCCESS_COLOR}]Exiting monitor. Goodbye.[/]")

if __name__ == "__main__":
    # Automatically install dependencies if missing
    for package in ["rich", "pyfiglet", "simple_pid"]:
        try:
            __import__(package)
        except ImportError:
            console.print(f"[{WARNING_COLOR}]Installing '{package}'...[/]")
            os.system(f"pip install {package.replace('_', '-')}")
    main()