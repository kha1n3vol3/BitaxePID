from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple

class IBitaxeAPIClient(ABC):
    """Interface for communicating with the Bitaxe miner hardware via an API."""
    @abstractmethod
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """Retrieve current system information from the miner."""
        pass

    @abstractmethod
    def set_settings(self, voltage: float, frequency: float) -> float:
        """Set voltage and frequency on the miner and return the actual applied frequency."""
        pass

    @abstractmethod
    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """Configure primary and backup stratum pools. Return success status."""
        pass

    @abstractmethod
    def restart(self) -> bool:
        """Restart the miner. Return True if successful."""
        pass

class ILogger(ABC):
    """Interface for logging miner data and snapshots."""
    @abstractmethod
    def log_to_csv(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float, pid_settings: Dict[str, float]) -> None:
        """Log miner performance data, including PID settings, to a CSV file."""
        pass

    @abstractmethod
    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """Save current miner settings as a snapshot."""
        pass

class IConfigLoader(ABC):
    """Interface for loading configuration data from external sources."""
    @abstractmethod
    def load_config(self, file_path: str) -> Dict[str, Any]:
        """Load configuration settings from a file."""
        pass

class ITerminalUI(ABC):
    """Interface for terminal-based user interfaces to display miner statistics."""
    @abstractmethod
    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """Update terminal UI with latest miner data."""
        pass

class TuningStrategy(ABC):
    """Interface for tuning strategies managing miner settings adjustments."""
    @abstractmethod
    def apply_strategy(self, current_voltage: float, current_frequency: float, temp: float,
                       hashrate: float, power: float) -> Tuple[float, float]:
        """Calculate new voltage and frequency settings based on the current miner status."""
        pass