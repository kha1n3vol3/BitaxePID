import requests
import time
import signal
import sys
import argparse
import logging
import json
import os
import csv
from typing import Dict, Optional, Any
from simple_pid import PID
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.progress import Progress, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich import box
import pyfiglet

console = Console()

# ANSI Color Codes for console output (fallback if rich fails)
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
RESET = "\033[0m"

# Cyberpunk Color Palette for TUI
SUCCESS_COLOR = "#00ff00"  # NEON_GREEN
WARNING_COLOR = "#ff9900"  # NEON_ORANGE
CRITICAL_COLOR = "#ff0000"  # Red for errors
TEXT_GREY = "#cccccc"
WHITE = "#ffffff"
NEON_CYAN = "#00ffff"
NEON_GREEN = "#00ff00"
NEON_YELLOW = "#ffff00"
NEON_PINK = "#ff0099"
DARK_GREY = "#121212"
MID_GREY = "#2a2a2a"
BLACK = "#000000"

# Configuration Defaults
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

# Argument Parsing
def parse_arguments():
    parser = argparse.ArgumentParser(description="Bitaxe 601 Gamma Auto-Tuner")
    parser.add_argument("bitaxe_ip", type=str, help="IP address of the Bitaxe (e.g., 192.168.68.111)")
    parser.add_argument("-v", "--voltage", type=int, default=DEFAULTS["INITIAL_VOLTAGE"], help=f"Initial voltage in mV (default: {DEFAULTS['INITIAL_VOLTAGE']})")
    parser.add_argument("-f", "--frequency", type=int, default=DEFAULTS["INITIAL_FREQUENCY"], help=f"Initial frequency in MHz (default: {DEFAULTS['INITIAL_FREQUENCY']})")
    parser.add_argument("-t", "--target_temp", type=float, default=DEFAULTS["TARGET_TEMP"], help=f"Target temp in °C (default: {DEFAULTS['TARGET_TEMP']})")
    parser.add_argument("-i", "--interval", type=int, default=DEFAULTS["SAMPLE_INTERVAL"], help=f"Sample interval in seconds (default: {DEFAULTS['SAMPLE_INTERVAL']})")
    parser.add_argument("-p", "--power_limit", type=float, default=DEFAULTS["POWER_LIMIT"], help=f"Power limit in W (default: {DEFAULTS['POWER_LIMIT']})")
    parser.add_argument("-s", "--setpoint", type=float, default=DEFAULTS["HASHRATE_SETPOINT"], help=f"Target hashrate in GH/s (default: {DEFAULTS['HASHRATE_SETPOINT']})")
    parser.add_argument("--temp-watch", action="store_true", help="Enable temp-watch mode to only adjust frequency/voltage to control temp")
    parser.add_argument("--log-to-console", action="store_true", help="Log to console only (disables TUI)")
    return parser.parse_args()

args = parse_arguments()
bitaxe_ip = f"http://{args.bitaxe_ip}"
target_temp = args.target_temp
sample_interval = args.interval
power_limit = args.power_limit
hashrate_setpoint = args.setpoint
temp_watch = args.temp_watch
log_to_console = args.log_to_console

SNAPSHOT_FILE = "bitaxe_snapshot.json"
LOG_FILE = "bitaxe_tuning_log.csv"
current_voltage = float(args.voltage)
current_frequency = float(args.frequency)

# Load snapshot if available
if os.path.exists(SNAPSHOT_FILE) and not args.voltage and not args.frequency:
    try:
        with open(SNAPSHOT_FILE, 'r') as f:
            snapshot = json.load(f)
            current_voltage = float(snapshot.get("voltage", DEFAULTS["INITIAL_VOLTAGE"]))
            current_frequency = float(snapshot.get("frequency", DEFAULTS["INITIAL_FREQUENCY"]))
    except Exception as e:
        console.print(f"[{WARNING_COLOR}]Failed to load snapshot: {e}[/]")

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bitaxe_monitor.log"), logging.StreamHandler() if log_to_console else logging.NullHandler()]
)
logger = logging.getLogger(__name__)

# PID Controllers
pid_freq = PID(Kp=DEFAULTS["PID_FREQ_KP"], Ki=DEFAULTS["PID_FREQ_KI"], Kd=DEFAULTS["PID_FREQ_KD"],
               setpoint=hashrate_setpoint, sample_time=sample_interval)
pid_freq.output_limits = (DEFAULTS["MIN_FREQUENCY"], DEFAULTS["MAX_FREQUENCY"])
pid_volt = PID(Kp=DEFAULTS["PID_VOLT_KP"], Ki=DEFAULTS["PID_VOLT_KI"], Kd=DEFAULTS["PID_VOLT_KD"],
               setpoint=hashrate_setpoint, sample_time=sample_interval)
pid_volt.output_limits = (DEFAULTS["MIN_VOLTAGE"], DEFAULTS["MAX_VOLTAGE"])

# State
running = True
log_messages = []
last_hashrate = None
stagnation_count = 0
drop_count = 0

def handle_sigint(signum: int, frame: Optional[Any]) -> None:
    global running
    save_snapshot()
    logger.info("Received SIGINT, exiting Bitaxe Monitor")
    if not log_to_console:
        console.print(f"[{WARNING_COLOR}]Exiting Bitaxe Monitor.[/]")
    running = False

signal.signal(signal.SIGINT, handle_sigint)

def save_snapshot():
    snapshot = {"voltage": current_voltage, "frequency": current_frequency}
    try:
        with open(SNAPSHOT_FILE, 'w') as f:
            json.dump(snapshot, f)
    except Exception as e:
        logger.error(f"Failed to save snapshot: {e}")

def log_tuning_data(timestamp, frequency, voltage, hashrate, temp):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, 'a', newline='') as csvfile:
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

def get_system_info() -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(f"{bitaxe_ip}/api/system/info", timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching system info: {e}")
        if not log_to_console:
            console.print(f"[{WARNING_COLOR}]Error fetching system info: {e}[/]")
        return None

def set_system_settings(core_voltage: float, frequency: float) -> float:
    frequency = round(frequency / DEFAULTS["FREQUENCY_STEP"]) * DEFAULTS["FREQUENCY_STEP"]
    frequency = max(DEFAULTS["MIN_FREQUENCY"], min(DEFAULTS["MAX_FREQUENCY"], frequency))
    core_voltage = max(DEFAULTS["MIN_VOLTAGE"], min(DEFAULTS["MAX_VOLTAGE"], core_voltage))
    settings = {"coreVoltage": core_voltage, "frequency": frequency}
    try:
        response = requests.patch(f"{bitaxe_ip}/api/system", json=settings, timeout=10)
        response.raise_for_status()
        logger.info(f"Applied settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz")
        if not log_to_console:
            console.print(f"[{SUCCESS_COLOR}]Applying settings: Voltage = {core_voltage}mV, Frequency = {frequency}MHz[/]")
        time.sleep(2)
    except requests.exceptions.RequestException as e:
        logger.error(f"Error setting system settings: {e}")
        if not log_to_console:
            console.print(f"[{WARNING_COLOR}]Error setting system settings: {e}[/]")
    return frequency

def create_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="top", size=15),
        Layout(name="bottom", ratio=1)
    )
    layout["top"].split_row(
        Layout(name="hashrate", ratio=3),
        Layout(name="header", ratio=2)
    )
    layout["bottom"].split_column(
        Layout(name="main", ratio=1),
        Layout(name="log", size=10)
    )
    layout["main"].split_row(
        Layout(name="stats", ratio=1),
        Layout(name="progress", ratio=2)
    )
    return layout

def update_tui(layout: Layout, info: Dict[str, Any], hash_rate: float):
    temp = info.get("temp", "N/A") if info else "N/A"
    power = info.get("power", 0) if info else 0
    voltage_reported = info.get("voltage", 0) if info else 0
    hostname = info.get("hostname", "Unknown") if info else "Unknown"
    core_voltage_actual = info.get("coreVoltageActual", 0) if info else 0
    frequency = info.get("frequency", 0) if info else 0
    best_diff = info.get("bestDiff", "N/A") if info else "N/A"
    best_session_diff = info.get("bestSessionDiff", "N/A") if info else "N/A"
    shares_accepted = info.get("sharesAccepted", 0) if info else 0
    shares_rejected = info.get("sharesRejected", 0) if info else 0
    uptime_seconds = info.get("uptimeSeconds", 0) if info else 0
    fan_rpm = info.get("fanrpm", 0) if info else 0
    free_heap = info.get("freeHeap", 0) if info else 0
    version = info.get("version", "N/A") if info else "N/A"
    wifi_status = info.get("wifiStatus", "N/A") if info else "N/A"

    uptime_str = time.strftime("%H:%M:%S", time.gmtime(uptime_seconds))
    free_heap_mb = free_heap / (1024 * 1024) if free_heap else 0

    # Hashrate Panel with ansi_shadow font and increased size, no decimals
    hashrate_str = f"{hash_rate:.0f} GH/s"
    ascii_art = pyfiglet.figlet_format(hashrate_str, font="ansi_regular")
    hashrate_text = Text(ascii_art, style=NEON_GREEN, overflow="crop")
    layout["hashrate"].update(Panel(hashrate_text, title="Hashrate", border_style=NEON_GREEN, style=f"on {BLACK}"))

    # Header
    mode = " [TEMP-WATCH]" if temp_watch else ""
    header_text = Text(f"Bitaxe 601 Gamma Auto-Tuner (Host: {hostname}){mode}", style=f"bold {NEON_PINK}", justify="center")
    layout["header"].update(Panel(header_text, style=f"on {DARK_GREY}", border_style=NEON_PINK))

    # Stats
    stats_table = Table(box=box.SIMPLE, style=TEXT_GREY)
    stats_table.add_column("Parameter", style=f"bold {TEXT_GREY}")
    stats_table.add_column("Value", style=f"bold {WHITE}")
    stats_table.add_row("Temperature", f"{temp if temp == 'N/A' else float(temp):.2f}°C" + (f" [{CRITICAL_COLOR}](OVERHEAT)[/]" if temp != "N/A" and float(temp) > target_temp else ""))
    stats_table.add_row("Power", f"{power:.2f}W" + (f" [{WARNING_COLOR}](OVER LIMIT)[/]" if power > power_limit else ""))
    stats_table.add_row("Voltage", f"{voltage_reported:.2f}mV")
    stats_table.add_row("Core Voltage", f"{core_voltage_actual:.2f}mV")
    stats_table.add_row("Frequency", f"{frequency:.2f}MHz")
    stats_table.add_row("Hashrate", f"{hash_rate:.2f} GH/s")
    stats_table.add_row("Best Diff", f"{best_diff}")
    stats_table.add_row("Best Session Diff", f"{best_session_diff}")
    stats_table.add_row("Shares", f"{shares_accepted:.0f} / {shares_rejected:.0f}")
    stats_table.add_row("Uptime", uptime_str)
    stats_table.add_row("Fan RPM", f"{fan_rpm:.2f}")
    stats_table.add_row("Free Heap", f"{free_heap_mb:.2f} MB")
    stats_table.add_row("Version", f"{version}")
    stats_table.add_row("WiFi Status", f"{wifi_status}")
    layout["stats"].update(Panel(stats_table, title="System Stats", border_style=NEON_CYAN))

    # Progress
    progress = Progress(
        TextColumn("{task.description}", style=WHITE),
        BarColumn(bar_width=40, complete_style=NEON_GREEN),
        TextColumn("{task.percentage:>3.0f}%")
    )
    progress.add_task(f"Hashrate (GH/s): {hash_rate:.2f}", total=hashrate_setpoint, completed=hash_rate)
    progress.add_task(f"Voltage (mV): {current_voltage:.2f}", total=DEFAULTS["MAX_VOLTAGE"], completed=current_voltage)
    progress.add_task(f"Frequency (MHz): {current_frequency:.2f}", total=DEFAULTS["MAX_FREQUENCY"], completed=current_frequency)
    layout["progress"].update(Panel(progress, title="Performance", border_style=NEON_GREEN))

    # Log
    log_text = Text("\n".join(log_messages[-8:]), style=f"{TEXT_GREY} on {BLACK}")
    layout["log"].update(Panel(log_text, title="Log", border_style=MID_GREY))

def monitor_and_adjust():
    global current_voltage, current_frequency, running, log_messages, last_hashrate, stagnation_count, drop_count
    logger.info(f"Starting Bitaxe Monitor. Target temp: {target_temp}°C, Target hashrate: {hashrate_setpoint} GH/s, Initial Voltage: {current_voltage}mV, Initial Frequency: {current_frequency}MHz, Temp-watch: {temp_watch}")
    if not log_to_console:
        console.print(f"[{SUCCESS_COLOR}]Starting Bitaxe Monitor. Target temp: {target_temp}°C, Target hashrate: {hashrate_setpoint} GH/s, Temp-watch: {temp_watch}[/]")
    
    current_frequency = set_system_settings(current_voltage, current_frequency)
    
    layout = create_layout() if not log_to_console else None
    live = Live(layout, console=console, refresh_per_second=1) if not log_to_console else None
    if live:
        live.start()

    while running:
        info = get_system_info()
        if info is None:
            log_messages.append("Failed to fetch system info, retrying...")
            if live:
                update_tui(layout, {}, 0)
            time.sleep(sample_interval)
            continue

        temp = info.get("temp", "N/A")
        hash_rate = info.get("hashRate", 0)
        power = info.get("power", 0)
        voltage_reported = info.get("voltage", 0)

        temp_float = float(temp) if temp != "N/A" else target_temp + 1
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        log_tuning_data(timestamp, current_frequency, current_voltage, hash_rate, temp_float)

        status = f"Temp: {temp}°C | Hashrate: {hash_rate:.2f} GH/s | Power: {power}W | Voltage: {voltage_reported}mV | Current Settings -> Voltage: {current_voltage}mV, Frequency: {current_frequency}MHz"
        logger.info(status)
        log_messages.append(status)

        if live:
            update_tui(layout, info, hash_rate)

        if temp_watch:
            # Temp-watch mode: only lower frequency/voltage to control temp
            if temp_float > target_temp:
                logger.warning(f"Temp {temp_float}°C exceeds target {target_temp}°C")
                log_messages.append(f"Temp-watch: Temp {temp_float}°C > {target_temp}°C")
                if not log_to_console:
                    console.print(f"[{WARNING_COLOR}]Temp {temp_float}°C exceeds target {target_temp}°C. Lowering settings.[/]")
                if current_frequency > DEFAULTS["MIN_FREQUENCY"]:
                    current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                    logger.info(f"Temp-watch: Reducing frequency to {current_frequency}MHz")
                    log_messages.append(f"Temp-watch: Frequency reduced to {current_frequency}MHz")
                elif current_voltage > DEFAULTS["MIN_VOLTAGE"]:
                    current_voltage -= DEFAULTS["VOLTAGE_STEP"]
                    logger.info(f"Temp-watch: Reducing voltage to {current_voltage}mV")
                    log_messages.append(f"Temp-watch: Voltage reduced to {current_voltage}mV")
        else:
            # Normal mode: PID-based optimization
            freq_output = pid_freq(hash_rate)
            volt_output = pid_volt(hash_rate)

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
                pid_freq.reset()
                pid_volt.reset()
                stagnation_count = 0

            if temp_float > target_temp or power > power_limit * 1.075:
                logger.warning(f"Constraint exceeded - Temp: {temp}°C > {target_temp}°C or Power: {power}W > {power_limit * 1.075}W")
                log_messages.append(f"Constraint exceeded: Temp {temp}°C, Power {power}W")
                if not log_to_console:
                    console.print(f"[{WARNING_COLOR}]Constraints exceeded! Lowering settings.[/]")
                if power > power_limit * 1.075 and current_voltage > DEFAULTS["MIN_VOLTAGE"]:
                    current_voltage -= DEFAULTS["VOLTAGE_STEP"]
                    logger.info(f"Power exceeded, reducing voltage to {current_voltage}mV")
                    log_messages.append(f"Power exceeded, voltage reduced to {current_voltage}mV")
                elif current_frequency > DEFAULTS["MIN_FREQUENCY"] and drop_count < 3:
                    current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                    logger.info(f"Reducing frequency to {current_frequency}MHz due to constraints")
                    log_messages.append(f"Frequency reduced to {current_frequency}MHz")
            elif hash_rate < hashrate_setpoint:
                if drop_count >= 3 and current_frequency > DEFAULTS["MIN_FREQUENCY"]:
                    current_frequency -= DEFAULTS["FREQUENCY_STEP"]
                    logger.info(f"Consistent hashrate drop, reducing frequency to {current_frequency}MHz")
                    log_messages.append(f"Consistent drop, frequency reduced to {current_frequency}MHz")
                else:
                    if hash_rate < 0.85 * hashrate_setpoint and current_voltage < DEFAULTS["MAX_VOLTAGE"]:
                        current_voltage = min(proposed_voltage, current_voltage + DEFAULTS["VOLTAGE_STEP"])
                        logger.info(f"Hashrate low, boosting voltage to {current_voltage}mV")
                        log_messages.append(f"Hashrate low, voltage boosted to {current_voltage}mV")
                    if current_voltage >= 1150:
                        current_frequency = proposed_frequency
                        logger.info(f"PID adjusted frequency to {current_frequency}MHz")
                        log_messages.append(f"Frequency adjusted to {current_frequency}MHz")
                    if current_frequency >= DEFAULTS["MAX_FREQUENCY"] and current_voltage < DEFAULTS["MAX_VOLTAGE"]:
                        current_voltage += DEFAULTS["VOLTAGE_STEP"]
                        logger.info(f"Max frequency reached, increasing voltage to {current_voltage}mV")
                        log_messages.append(f"Max frequency, voltage increased to {current_voltage}mV")
            else:
                logger.info("System stable, maintaining settings")
                log_messages.append("System stable, maintaining settings")
                if not log_to_console:
                    console.print(f"[{SUCCESS_COLOR}]Stable. No adjustment needed.[/]")

        current_frequency = set_system_settings(current_voltage, current_frequency)
        save_snapshot()
        last_hashrate = hash_rate
        time.sleep(sample_interval)

    if live:
        live.stop()

if __name__ == "__main__":
    try:
        import rich
    except ImportError:
        console.print(f"[{WARNING_COLOR}]Installing 'rich' for TUI...[/]")
        os.system("pip install rich")
    try:
        import pyfiglet
    except ImportError:
        console.print(f"[{WARNING_COLOR}]Installing 'pyfiglet' for ANSI art...[/]")
        os.system("pip install pyfiglet")
    try:
        import simple_pid
    except ImportError:
        console.print(f"[{WARNING_COLOR}]Installing 'simple-pid' for PID control...[/]")
        os.system("pip install simple-pid")
    try:
        monitor_and_adjust()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if not log_to_console:
            console.print(f"[{WARNING_COLOR}]An unexpected error occurred: {e}[/]")
    finally:
        save_snapshot()
        logger.info("Exiting monitor")
        if not log_to_console:
            console.print(f"[{SUCCESS_COLOR}]Exiting monitor. Goodbye.[/]")
