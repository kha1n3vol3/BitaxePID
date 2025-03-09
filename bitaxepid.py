import argparse
import logging
import signal
import sys
import time
import os
from typing import Dict, Any, Optional, List
from urllib.parse import urlparse
from interfaces import IBitaxeAPIClient, ILogger, IConfigLoader, ITerminalUI, TuningStrategy
from implementations import BitaxeAPIClient, Logger, YamlConfigLoader, RichTerminalUI, NullTerminalUI, PIDTuningStrategy
from pools import get_fastest_pools, parse_endpoint
from rich.console import Console
import yaml
console = Console()

__version__ = "1.0.0"

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
        backup_stratum: Optional[Dict[str, Any]] = None
    ):
        self.tuning_strategy = tuning_strategy
        self.api_client = api_client
        self.logger = logger
        self.config_loader = config_loader
        self.terminal_ui = terminal_ui
        self.sample_interval = sample_interval
        self.running = True
        self.current_voltage = initial_voltage
        self.current_frequency = initial_frequency
        self.pools_file = pools_file
        self.config = config
        self.user_file = user_file

        # Get current system settings
        system_info = self.api_client.get_system_info()
        if system_info is None:
            logging.error("Failed to get system info")
            sys.exit(1)
            
        current_stratum_user = system_info.get("stratumUser", "")
        current_fallback_user = system_info.get("fallbackStratumUser", "")

        # Only load from user.yaml if needed
        self.stratum_users = {}
        if not current_stratum_user or not current_fallback_user:
            self.stratum_users = self._load_stratum_users()
            logging.debug(f"Loaded stratum users from user.yaml: {self.stratum_users}")

        # Determine stratum endpoints
        if primary_stratum and backup_stratum:
            stratum_info = [primary_stratum, backup_stratum]
        else:
            stratum_info = self._get_top_pools()

        if len(stratum_info) < 2:
            logging.error("At least two stratum endpoints are required. Provide via command-line or ensure pools_file has sufficient entries.")
            sys.exit(1)

        primary = stratum_info[0]
        backup = stratum_info[1]

        # Only set users if system values are empty
        if not current_stratum_user:
            primary["user"] = (
                primary.get("user") or  # Command line argument
                self.stratum_users.get("stratumUser", "") # user.yaml fallback
            )
        else:
            primary["user"] = current_stratum_user
            logging.info(f"Preserving existing primary stratum user: {current_stratum_user}")

        if not current_fallback_user:
            backup["user"] = (
                backup.get("user") or # Command line argument
                self.stratum_users.get("fallbackStratumUser", "") or
                self.stratum_users.get("stratumUser", "")
            )
        else:
            backup["user"] = current_fallback_user
            logging.info(f"Preserving existing backup stratum user: {current_fallback_user}")

        # Validate stratum users
        if not primary["user"] or not backup["user"]:
            logging.error(f"Stratum users not properly configured. Primary: '{primary['user']}', Backup: '{backup['user']}'")
            logging.error("Please check your configuration or provide users via command line arguments")
            sys.exit(1)

        logging.info(f"Setting primary stratum: {primary['endpoint']}:{primary['port']} with user {primary['user']}")
        logging.info(f"Setting backup stratum: {backup['endpoint']}:{backup['port']} with user {backup['user']}")

        # Configure API client with stratum settings
        if self.api_client.set_stratum(primary, backup):
            logging.info("Stratum configuration successful, restarting miner...")
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.show_banner()
            time.sleep(1)
            self.api_client.restart()
        else:
            logging.error("Failed to set stratum endpoints, not restarting")
            sys.exit(1)

        # Initialize hardware settings
        logging.info(f"Initializing hardware settings: Voltage={self.current_voltage}mV, Frequency={self.current_frequency}MHz")
        self.api_client.set_settings(self.current_voltage, self.current_frequency)


    def stop_tuning(self):
        """Stop the tuning process gracefully"""
        self.running = False
        if isinstance(self.terminal_ui, RichTerminalUI):
            self.terminal_ui.stop()
        print("\nTuning stopped gracefully")

    def start_tuning(self):
        """Start the tuning process."""
        try:
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.start()
            
            while self.running:
                try:
                    system_info = self.api_client.get_system_info()
                    if not system_info:
                        time.sleep(1)
                        continue

                    # Update TUI
                    self.terminal_ui.update(system_info, self.current_voltage, self.current_frequency)

                    # Log current state
                    self.logger.log_to_csv(
                        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                        frequency=self.current_frequency,
                        voltage=self.current_voltage,
                        hashrate=system_info.get("hashRate", 0),
                        temp=system_info.get("temp", 0),
                        pid_settings=self.config
                    )

                    # Apply tuning strategy
                    new_voltage, new_frequency = self.tuning_strategy.apply_strategy(
                        current_voltage=self.current_voltage,
                        current_frequency=self.current_frequency,
                        temp=system_info.get("temp", 0),
                        hashrate=system_info.get("hashRate", 0),
                        power=system_info.get("power", 0)
                    )

                    # Apply new settings if they changed
                    if new_voltage != self.current_voltage or new_frequency != self.current_frequency:
                        self.current_voltage = new_voltage
                        self.current_frequency = new_frequency
                        self.api_client.set_settings(new_voltage, new_frequency)
                        self.logger.save_snapshot(new_voltage, new_frequency)

                    time.sleep(self.sample_interval)

                except KeyboardInterrupt:
                    break
                except Exception as e:
                    print(f"Error in tuning loop: {e}")
                    time.sleep(1)

        finally:
            if isinstance(self.terminal_ui, RichTerminalUI):
                self.terminal_ui.stop()

    def _load_stratum_users(self) -> Dict[str, str]:
        """Load stratum users from the user_file."""
        if not self.user_file:
            self.user_file = "user.yaml"
            logging.debug(f"No user file specified, using default: {self.user_file}")

        try:
            if os.path.exists(self.user_file):
                with open(self.user_file, 'r') as f:
                    users = yaml.safe_load(f)
                    if not users:
                        logging.error(f"Empty or invalid user file: {self.user_file}")
                        return {}
                    required_keys = ['stratumUser', 'fallbackStratumUser']
                    if not all(key in users for key in required_keys):
                        logging.error(f"Missing required keys in {self.user_file}. Required: {required_keys}")
                        return {}
                    logging.debug(f"Loaded stratum users from {self.user_file}: {users}")
                    return users
            else:
                logging.error(f"User file not found: {self.user_file}")
                return {}
        except Exception as e:
            logging.error(f"Error loading user file {self.user_file}: {e}")
            return {}

    def _get_top_pools(self) -> List[Dict[str, Any]]:
        """Load top pools from pools_file and ensure they have 'port' key."""
        if os.path.exists(self.pools_file):
            with open(self.pools_file, 'r') as f:
                pools = yaml.safe_load(f) or []
            for pool in pools:
                endpoint = pool.get('endpoint', '')
                try:
                    hostname, port = parse_endpoint(endpoint)
                    pool['port'] = port
                    pool['endpoint'] = hostname
                except ValueError:
                    pool['port'] = 0
                    logging.warning(f"Invalid endpoint {endpoint}, port set to 0.")
            return pools[:2]
        else:
            return []

def parse_stratum(url: str) -> Dict[str, Any]:
    """Parse a stratum URL into a dictionary."""
    parsed = urlparse(url)
    if parsed.scheme != 'stratum+tcp':
        raise ValueError(f"Invalid stratum scheme: {parsed.scheme}. Use 'stratum+tcp://host:port'")
    return {"endpoint": parsed.hostname, "port": parsed.port}



def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="BitaxePID Auto-Tuner")
    parser.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    parser.add_argument("--ip", required=True, type=str, help="IP address of the Bitaxe miner")
    parser.add_argument("--config", type=str, help="Path to optional user YAML configuration file")
    parser.add_argument("--user-file", type=str, default="user.yaml", help="Path to user YAML file for stratum users (default: user.yaml)")
    parser.add_argument("--pools-file", type=str, default="pools.yaml", help="Path to pools YAML file (default: pools.yaml)")
    parser.add_argument("--primary-stratum", type=str, help="Primary stratum URL (e.g., stratum+tcp://host:port)")
    parser.add_argument("--backup-stratum", type=str, help="Backup stratum URL (e.g., stratum+tcp://host:port)")
    parser.add_argument("--stratum-user", type=str, help="Stratum user for primary pool")
    parser.add_argument("--fallback-stratum-user", type=str, help="Stratum user for backup pool")
    parser.add_argument("--voltage", type=float, help="Initial voltage override")
    parser.add_argument("--frequency", type=float, help="Initial frequency override")
    parser.add_argument("--sample-interval", type=float, help="Sample interval override (seconds)")
    parser.add_argument("--log-to-console", action="store_true", help="Log to console instead of UI")
    parser.add_argument("--logging-level", type=str, choices=["info", "debug"], default="info", help="Logging level")
    return parser.parse_args()

def load_config(config_loader: IConfigLoader, asic_yaml: str, user_config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and merge configurations from ASIC model YAML and optional user config."""
    if not os.path.exists(asic_yaml):
        logging.error(f"ASIC model YAML file {asic_yaml} not found.")
        sys.exit(1)
    config = config_loader.load_config(asic_yaml)
    if user_config_path and os.path.exists(user_config_path):
        user_config = config_loader.load_config(user_config_path)
        config.update(user_config)
    return config

def validate_config(config: Dict[str, Any]) -> None:
    """Validate that required configuration keys are present."""
    required_keys = [
        "INITIAL_VOLTAGE",      # Initial voltage for TuningManager
        "INITIAL_FREQUENCY",    # Initial frequency for TuningManager
        "SAMPLE_INTERVAL",      # Sampling interval for both
        "LOG_FILE",             # Log file path
        "SNAPSHOT_FILE",        # Snapshot file path
        "POOLS_FILE",           # Pools file path
        "PID_FREQ_KP",          # Frequency PID proportional gain
        "PID_FREQ_KI",          # Frequency PID integral gain
        "PID_FREQ_KD",          # Frequency PID derivative gain
        "PID_VOLT_KP",          # Voltage PID proportional gain
        "PID_VOLT_KI",          # Voltage PID integral gain
        "PID_VOLT_KD",          # Voltage PID derivative gain
        "MIN_VOLTAGE",          # Minimum allowed voltage
        "MAX_VOLTAGE",          # Maximum allowed voltage
        "MIN_FREQUENCY",        # Minimum allowed frequency
        "MAX_FREQUENCY",        # Maximum allowed frequency
        "VOLTAGE_STEP",         # Voltage adjustment step size
        "FREQUENCY_STEP",       # Frequency adjustment step size
        "HASHRATE_SETPOINT",    # Target hashrate for PID
        "TARGET_TEMP",          # Maximum allowable temperature
        "POWER_LIMIT"           # Maximum allowable power
    ]
    missing_keys = [key for key in required_keys if key not in config]
    if missing_keys:
        logging.error(f"Missing required configuration keys: {', '.join(missing_keys)}")
        sys.exit(1)

def main() -> None:
    args = parse_arguments()
    # Set up logging
    handlers = [logging.FileHandler("bitaxepid_monitor.log")]  # Optional: Make configurable
    if args.log_to_console:
        handlers.append(logging.StreamHandler())
    logging_level = logging.DEBUG if args.logging_level == "debug" else logging.INFO
    logging.basicConfig(level=logging_level, format="%(asctime)s - %(levelname)s - %(message)s", handlers=handlers)

    # Initialize API client
    api_client = BitaxeAPIClient(args.ip)

    # Get ASIC model from system info
    system_info = api_client.get_system_info()
    if system_info is None:
        logging.error("Failed to fetch system info from API.")
        sys.exit(1)
    asic_model = system_info.get("ASICModel", "default")
    asic_yaml = f"{asic_model}.yaml"

    # Load configuration
    config_loader = YamlConfigLoader()
    config = load_config(config_loader, asic_yaml, args.config)

    # Apply command-line overrides
    if args.voltage is not None:
        config["INITIAL_VOLTAGE"] = args.voltage
    if args.frequency is not None:
        config["INITIAL_FREQUENCY"] = args.frequency
    if args.sample_interval is not None:
        config["SAMPLE_INTERVAL"] = args.sample_interval

    # Validate required configuration keys
    validate_config(config)

    # Initialize components
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
        setpoint=config["HASHRATE_SETPOINT"],
        sample_interval=config["SAMPLE_INTERVAL"],
        target_temp=config["TARGET_TEMP"],
        power_limit=config["POWER_LIMIT"]
    )
    terminal_ui = NullTerminalUI() if args.log_to_console else RichTerminalUI()

    # Handle stratum settings from command-line
    primary_stratum = None
    backup_stratum = None
    if args.primary_stratum and args.backup_stratum:
        try:
            primary_stratum = parse_stratum(args.primary_stratum)
            backup_stratum = parse_stratum(args.backup_stratum)
            if args.stratum_user:
                primary_stratum["user"] = args.stratum_user
            if args.fallback_stratum_user:
                backup_stratum["user"] = args.fallback_stratum_user
        except ValueError as e:
            logging.error(f"Stratum URL parsing error: {e}")
            sys.exit(1)

    # Initialize TuningManager
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
        user_file=args.user_file,
        primary_stratum=primary_stratum,
        backup_stratum=backup_stratum
    )

    # Signal handling
    def signal_handler(sig, frame):
        logging.info("Shutting down gracefully...")
        tuning_manager.stop_tuning()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start tuning
    logging.info("Starting BitaxePID tuner...")
    tuning_manager.start_tuning()

if __name__ == "__main__":
    main()