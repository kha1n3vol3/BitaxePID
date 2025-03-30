#!/usr/bin/env python3
"""
Interfaces Module for BitaxePID Auto-Tuner

This module defines abstract base classes (ABCs) for various components
used in the BitaxePID Auto-Tuner, ensuring a consistent interface for
different implementations.
"""

# Standard library imports
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple


class IBitaxeAPIClient(ABC):
    """
    Abstract base class for Bitaxe API clients.

    Defines the interface for interacting with the Bitaxe miner's API.
    """

    @abstractmethod
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve system information from the Bitaxe miner.

        Returns:
            Optional[Dict[str, Any]]: A dictionary containing system information
                if successful, None otherwise.
        """
        pass

    @abstractmethod
    def set_settings(self, voltage: float, frequency: float) -> float:
        """
        Set the voltage and frequency settings on the Bitaxe miner.

        Args:
            voltage (float): The target voltage in millivolts (mV).
            frequency (float): The target frequency in megahertz (MHz).

        Returns:
            float: The frequency set, returned even if the operation fails.
        """
        pass

    @abstractmethod
    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """
        Configure the primary and backup stratum servers.

        Args:
            primary (Dict[str, Any]): Dictionary containing primary stratum settings.
            backup (Dict[str, Any]): Dictionary containing backup stratum settings.

        Returns:
            bool: True if the configuration is successful, False otherwise.
        """
        pass

    @abstractmethod
    def restart(self) -> bool:
        """
        Restart the Bitaxe miner and verify it comes back online.

        Returns:
            bool: True if the restart is successful and the miner responds,
                False otherwise.
        """
        pass

    @abstractmethod
    def close(self) -> None:
        """
        Close any open connections or resources used by the API client.
        """
        pass


class TuningStrategy(ABC):
    """
    Abstract base class for tuning strategies.

    Defines the interface for strategies that adjust voltage and frequency
    based on current conditions.
    """

    @abstractmethod
    def apply_strategy(
        self,
        current_voltage: float,
        current_frequency: float,
        temp: float,
    ) -> Tuple[float, float]:
        """
        Calculate new voltage and frequency settings based on current conditions.

        This method should implement the logic to adjust voltage and frequency
        to optimize performance or maintain stability based on the current temperature.

        Args:
            current_voltage (float): Current target voltage in millivolts (mV).
            current_frequency (float): Current target frequency in megahertz (MHz).
            temp (float): Current temperature in degrees Celsius (°C).

        Returns:
            Tuple[float, float]: New voltage and frequency settings as
                (voltage in mV, frequency in MHz).
        """
        pass


class ILogger(ABC):
    """
    Abstract base class for logging implementations.

    Defines the interface for logging miner performance data and saving snapshots.
    """

    @abstractmethod
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
        """
        Log miner performance data to a CSV file and update t-digest files.

        This method records various metrics and settings to a CSV log for analysis
        and updates t-digest files for statistical summaries.

        Args:
            mac_address (str): MAC address of the miner.
            timestamp (str): Timestamp of the log entry.
            target_frequency (float): Target frequency in megahertz (MHz).
            target_voltage (float): Target voltage in millivolts (mV).
            hashrate (float): Current hashrate.
            temp (float): Current temperature in degrees Celsius (°C).
            pid_settings (Dict[str, Any]): Dictionary of PID controller settings.
            power (float): Current power consumption in watts.
            board_voltage (float): Measured board voltage.
            current (float): Measured current.
            core_voltage_actual (float): Actual core voltage.
            frequency (float): Actual frequency in megahertz (MHz).
            fanrpm (int): Fan speed in revolutions per minute (RPM).
        """
        pass

    @abstractmethod
    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """
        Save a snapshot of the current voltage and frequency settings.

        Args:
            voltage (float): Current voltage in millivolts (mV).
            frequency (float): Current frequency in megahertz (MHz).
        """
        pass


class IConfigLoader(ABC):
    """
    Abstract base class for configuration loaders.

    Defines the interface for loading configuration data from a file.
    """

    @abstractmethod
    def load_config(self, file_path: str) -> Dict[str, Any]:
        """
        Load configuration data from a specified file path.

        Args:
            file_path (str): Path to the configuration file.

        Returns:
            Dict[str, Any]: A dictionary containing the configuration data.
        """
        pass


class ITerminalUI(ABC):
    """
    Abstract base class for terminal user interfaces.

    Defines the interface for updating the terminal display with miner status.
    """

    @abstractmethod
    def update(
        self, system_info: Dict[str, Any], voltage: float, frequency: float
    ) -> None:
        """
        Update the terminal UI with the latest system information and settings.

        Args:
            system_info (Dict[str, Any]): Dictionary containing system information.
            voltage (float): Current voltage in millivolts (mV).
            frequency (float): Current frequency in megahertz (MHz).
        """
        pass
