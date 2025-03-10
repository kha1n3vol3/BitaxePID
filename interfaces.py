from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple

class IBitaxeAPIClient(ABC):
    """Interface for communicating with the Bitaxe miner hardware via an API."""
    
    @abstractmethod
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """Retrieve current system information from the miner.
        
        Returns:
            Optional[Dict[str, Any]]: System information as a dictionary, or None if unavailable.
        """
        pass

    @abstractmethod
    def set_settings(self, voltage: float, frequency: float) -> float:
        """Set voltage and frequency on the miner and return the actual applied frequency.
        
        Args:
            voltage (float): Target voltage to set.
            frequency (float): Target frequency to set.
        
        Returns:
            float: The actual frequency applied by the miner.
        """
        pass

    @abstractmethod
    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """Configure primary and backup stratum pools.
        
        Args:
            primary (Dict[str, Any]): Configuration for the primary stratum pool.
            backup (Dict[str, Any]): Configuration for the backup stratum pool.
        
        Returns:
            bool: True if the stratum settings were successfully applied, False otherwise.
        """
        pass

    @abstractmethod
    def restart(self) -> bool:
        """Restart the miner.
        
        Returns:
            bool: True if the restart was successful, False otherwise.
        """
        pass

class ILogger(ABC):
    """Interface for logging miner data and snapshots."""
    
    @abstractmethod
    def log_to_csv(self, timestamp: str, frequency: float, voltage: float, hashrate: float, temp: float, pid_settings: Dict[str, float]) -> None:
        """Log miner performance data, including PID settings, to a CSV file.
        
        Args:
            timestamp (str): Time of the data point.
            frequency (float): Current frequency setting.
            voltage (float): Current voltage setting.
            hashrate (float): Measured hashrate.
            temp (float): Measured temperature.
            pid_settings (Dict[str, float]): PID controller settings.
        """
        pass

    @abstractmethod
    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """Save current miner settings as a snapshot.
        
        Args:
            voltage (float): Current voltage setting.
            frequency (float): Current frequency setting.
        """
        pass

class IConfigLoader(ABC):
    """Interface for loading configuration data from external sources."""
    
    @abstractmethod
    def load_config(self, file_path: str) -> Dict[str, Any]:
        """Load configuration settings from a file.
        
        Args:
            file_path (str): Path to the configuration file.
        
        Returns:
            Dict[str, Any]: Configuration data as a dictionary.
        """
        pass

class ITerminalUI(ABC):
    """Interface for terminal-based user interfaces to display miner statistics."""
    
    @abstractmethod
    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """Update terminal UI with latest miner data.
        
        Args:
            system_info (Dict[str, Any]): Current system information.
            voltage (float): Current voltage setting.
            frequency (float): Current frequency setting.
        """
        pass

class TuningStrategy(ABC):
    """Interface for tuning strategies managing miner settings adjustments."""
    
    @abstractmethod
    def apply_strategy(self, current_voltage: float, current_frequency: float, temp: float,
                       hashrate: float, power: float) -> Tuple[float, float]:
        """Calculate new voltage and frequency settings based on the current miner status.
        
        Args:
            current_voltage (float): Current voltage setting.
            current_frequency (float): Current frequency setting.
            temp (float): Current temperature.
            hashrate (float): Current hashrate.
            power (float): Current power consumption.
        
        Returns:
            Tuple[float, float]: New (voltage, frequency) settings.
        """
        pass