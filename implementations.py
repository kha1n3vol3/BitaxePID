import requests
import yaml
import json
import os
import csv
import time
from typing import Dict, Any, Optional, Tuple
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
    """Concrete implementation of the Bitaxe API client."""
    def __init__(self, bitaxepid_ip: str):
        self.bitaxepid_url = f"http://{bitaxepid_ip}"
        self.logger = getLogger(__name__)
        self.timeout = 5  # Reduced timeout to 5 seconds
        self.max_retries = 3
        self.retry_delay = 2  # Seconds between retries

    def _make_request(self, method: str, endpoint: str, **kwargs) -> Optional[requests.Response]:
        """Make HTTP request with retry logic"""
        kwargs['timeout'] = self.timeout  # Use shorter timeout
        
        for attempt in range(self.max_retries):
            try:
                response = requests.request(method, f"{self.bitaxepid_url}{endpoint}", **kwargs)
                response.raise_for_status()
                return response
            except requests.exceptions.RequestException as e:
                if attempt == self.max_retries - 1:  # Last attempt
                    self.logger.error(f"Failed after {self.max_retries} attempts: {e}")
                    raise
                self.logger.warning(f"Attempt {attempt + 1} failed, retrying in {self.retry_delay}s: {e}")
                time.sleep(self.retry_delay)

    def get_system_info(self) -> Optional[Dict[str, Any]]:
        try:
            response = self._make_request("GET", "/api/system/info")
            return response.json() if response else None
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error fetching system info: {e}")
            console.print(f"[{ERROR_COLOR}]Error fetching system info: {e}[/]")
            return None

    def set_settings(self, voltage: float, frequency: float) -> float:
        settings = {"coreVoltage": voltage, "frequency": frequency}
        try:
            self._make_request("PATCH", "/api/system", json=settings)
            self.logger.info(f"Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz")
            console.print(f"[{PRIMARY_ACCENT}]Applied settings: Voltage={voltage}mV, Frequency={frequency}MHz[/]")
            time.sleep(2)  # Delay to allow settings to take effect
            
            # Verify settings were applied
            system_info = self.get_system_info()
            if system_info:
                actual_voltage = system_info.get("coreVoltage", 0)
                actual_freq = system_info.get("frequency", 0)
                if abs(actual_voltage - voltage) > 5 or abs(actual_freq - frequency) > 5:
                    self.logger.warning(f"Settings mismatch - Requested: {voltage}mV/{frequency}MHz, "
                                      f"Actual: {actual_voltage}mV/{actual_freq}MHz")
            return frequency
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error setting system settings: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting system settings: {e}[/]")
            return frequency

    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        settings = {
            "stratumURL": primary["endpoint"],
            "stratumPort": primary["port"],
            "fallbackStratumURL": backup["endpoint"],
            "fallbackStratumPort": backup["port"],
            "stratumUser": primary.get("user", ""),
            "fallbackStratumUser": backup.get("user", "")
        }
        
        try:
            self._make_request("PATCH", "/api/system", json=settings)
            self.logger.info(f"Set stratum: Primary={primary['endpoint']}:{primary['port']} "
                           f"User={primary.get('user', '')}, "
                           f"Backup={backup['endpoint']}:{backup['port']} "
                           f"User={backup.get('user', '')}")
            console.print(f"[{PRIMARY_ACCENT}]Set stratum configuration successfully[/]")
            
            # Verify stratum settings
            time.sleep(1)  # Brief delay before verification
            system_info = self.get_system_info()
            if system_info:
                if (system_info.get("stratumURL") != primary["endpoint"] or
                    system_info.get("stratumPort") != primary["port"] or
                    system_info.get("fallbackStratumURL") != backup["endpoint"] or
                    system_info.get("fallbackStratumPort") != backup["port"] or
                    system_info.get("stratumUser") != primary.get("user", "") or
                    system_info.get("fallbackStratumUser") != backup.get("user", "")):
                    self.logger.warning("Stratum settings verification failed - settings may not have been applied correctly")
                    return False
            return True
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error setting stratum endpoints: {e}")
            console.print(f"[{ERROR_COLOR}]Error setting stratum endpoints: {e}[/]")
            return False

    def restart(self) -> bool:
        try:
            self._make_request("POST", "/api/system/restart")
            self.logger.info("Restarted Bitaxe miner")
            console.print(f"[{PRIMARY_ACCENT}]Restarted Bitaxe miner[/]")
            
            # Wait for restart and verify connectivity
            time.sleep(5)  # Allow time for restart
            retry_count = 0
            while retry_count < 3:
                try:
                    system_info = self.get_system_info()
                    if system_info:
                        self.logger.info("Miner successfully restarted and responding")
                        return True
                except requests.exceptions.RequestException:
                    retry_count += 1
                    time.sleep(2)
            
            self.logger.warning("Miner restart completed but not responding")
            return False
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Error restarting Bitaxe miner: {e}")
            console.print(f"[{ERROR_COLOR}]Error restarting Bitaxe miner: {e}[/]")
            return False

class Logger(ILogger):
    """Concrete implementation for logging miner data and snapshots."""
    def __init__(self, log_file: str, snapshot_file: str):
        self.log_file = log_file
        self.snapshot_file = snapshot_file

    def log_to_csv(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float, pid_settings: Dict[str, float]) -> None:
        file_exists = os.path.isfile(self.log_file)
        with open(self.log_file, 'a', newline='') as csvfile:
            fieldnames = ["timestamp", "frequency_mhz", "voltage_mv", "hashrate_ghs", "temperature_c",
                          "pid_freq_kp", "pid_freq_ki", "pid_freq_kd", "pid_volt_kp", "pid_volt_ki", "pid_volt_kd"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": timestamp,
                "frequency_mhz": frequency,
                "voltage_mv": voltage,
                "hashrate_ghs": hashrate,
                "temperature_c": temp,
                "pid_freq_kp": pid_settings["PID_FREQ_KP"],
                "pid_freq_ki": pid_settings["PID_FREQ_KI"],
                "pid_freq_kd": pid_settings["PID_FREQ_KD"],
                "pid_volt_kp": pid_settings["PID_VOLT_KP"],
                "pid_volt_ki": pid_settings["PID_VOLT_KI"],
                "pid_volt_kd": pid_settings["PID_VOLT_KD"]
            })

    def save_snapshot(self, voltage: float, frequency: float) -> None:
        snapshot = {"voltage": voltage, "frequency": frequency}
        try:
            with open(self.snapshot_file, 'w') as f:
                json.dump(snapshot, f)
        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Failed to save snapshot: {e}[/]")

class YamlConfigLoader(IConfigLoader):
    """Concrete implementation for loading YAML configuration files."""
    def load_config(self, file_path: str) -> Dict[str, Any]:
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
    def __init__(self):
        self.log_messages = []
        self.has_data = False  # Add flag for initial data
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
                "fallbackStratumPort"
            ],
            "Chip": [
                "ASICModel",
                "asicCount",
                "smallCoreCount"
            ],
            "Power": [
                "power",
                "voltage",
                "current"
            ],
            "Thermal": [
                "temp",
                "vrTemp",
                "overheat_mode"
            ],
            "Mining Performance": [
                "bestDiff",
                "bestSessionDiff",
                "sharesAccepted",
                "sharesRejected"
            ],
            "System": [
                "freeHeap",
                "uptimeSeconds",
                "version",
                "idfVersion",
                "boardVersion"
            ],
            "Display & Fans": [
                "autofanspeed",
                "fanspeed",
                "fanrpm"
            ]
        }
        self.layout = self.create_layout()
        self.live = Live(self.layout, console=console, refresh_per_second=1)
        self._started = False

    def show_banner(self):
        """Display the banner until data is available"""
        try:
            with open('banner.txt', 'r') as f:
                banner_text = f.read()
            console.print(banner_text)
            console.print("\nWaiting for miner data...", style=PRIMARY_ACCENT)
        except FileNotFoundError:
            console.print("Banner file not found", style=ERROR_COLOR)

    def create_layout(self) -> Layout:
        """Creates a layout with sections for different types of information."""
        layout = Layout()
        
        # Create main sections with reduced sizes
        layout.split_column(
            Layout(name="top", size=7),
            Layout(name="middle"),
            Layout(name="bottom", size=3)
        )

        # Split top section for hashrate and header
        layout["top"].split_row(
            Layout(name="hashrate"),
            Layout(name="header")
        )

        # Split middle section into columns
        layout["middle"].split_row(
            Layout(name="left_column"),
            Layout(name="right_column")
        )

        # Split columns into sections
        layout["left_column"].split_column(
            Layout(name="network"),
            Layout(name="chip"),
            Layout(name="power")
        )

        layout["right_column"].split_column(
            Layout(name="thermal"),
            Layout(name="mining_performance"),
            Layout(name="system"),
            Layout(name="display_fans")
        )

        # Bottom section for logs
        layout["bottom"].name = "log"

        return layout

    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """Updates the terminal UI with system information."""
        try:
            if not self.has_data:
                console.clear()  # Clear the banner
                self.has_data = True

            # Format hashrate for display without decimals
            hashrate = system_info.get("hashRate", 0)
            hashrate_str = f"{int(hashrate)} GH/s"
            ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
            self.layout["hashrate"].update(
                Panel(ascii_art, title="Hashrate", border_style=PRIMARY_ACCENT)
            )

            # Update system status with core information and stratum users
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

            # Update other sections
            section_layouts = {
                "Network": "network",
                "Chip": "chip",
                "Power": "power",
                "Thermal": "thermal",
                "Mining Performance": "mining_performance",
                "System": "system",
                "Display & Fans": "display_fans"
            }

            for section_name, layout_name in section_layouts.items():
                table = Table(show_header=False, box=None)
                table.add_column("", style=DECORATIVE_COLOR)
                table.add_column("", style=TEXT_COLOR)

                for key in self.sections[section_name]:
                    if key in system_info:
                        value = system_info[key]
                        # Special formatting for stratum URLs
                        if key in ["stratumURL", "fallbackStratumURL"]:
                            port_key = "stratumPort" if key == "stratumURL" else "fallbackStratumPort"
                            port = system_info.get(port_key, "")
                            value = f"{value}:{port}"
                        # Format numbers without decimals
                        elif isinstance(value, (int, float)):
                            value = f"{int(value)}"
                        table.add_row(key, str(value))

                self.layout[layout_name].update(Panel(table, title=section_name))

            # Update log with clean formatting
            status = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} - "
                     f"Voltage: {int(voltage)}mV, "
                     f"Frequency: {int(frequency)}MHz, "
                     f"Hashrate: {int(hashrate)} GH/s, "
                     f"Temp: {system_info.get('temp', 'N/A')}°C")
            
            self.log_messages.append(status)
            if len(self.log_messages) > 6:
                self.log_messages.pop(0)
            log_text = Text("\n".join(self.log_messages))
            self.layout["log"].update(Panel(log_text, title="Log"))

        except Exception as e:
            console.print(f"[{ERROR_COLOR}]Error updating TUI: {e}[/]")

    def start(self):
        """Start the live display."""
        if not self._started:
            self.live.start()
            self._started = True

    def stop(self):
        """Stop the live display."""
        if self._started:
            self.live.stop()
            self._started = False

class NullTerminalUI(ITerminalUI):
    """Null implementation of the terminal UI for console-only logging."""
    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        pass

class PIDTuningStrategy(TuningStrategy):
    """Concrete implementation of the PID-based tuning strategy."""
    def __init__(self, kp_freq: float, ki_freq: float, kd_freq: float, kp_volt: float, ki_volt: float, kd_volt: float,
                 min_voltage: float, max_voltage: float, min_frequency: float, max_frequency: float,
                 voltage_step: float, frequency_step: float, setpoint: float, sample_interval: float,
                 target_temp: float, power_limit: float):
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

    def apply_strategy(self, current_voltage: float, current_frequency: float, temp: float, hashrate: float, power: float) -> Tuple[float, float]:
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