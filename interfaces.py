#!/usr/bin/env python3
"""
Interfaces Module for BitaxePID Auto-Tuner

This module defines abstract base classes (ABCs) for the BitaxePID auto-tuner’s components, including
API communication, logging, configuration loading, terminal UI, and tuning strategies. These interfaces
ensure a consistent contract for implementations used in the tuning system.

Usage:
    >>> from interfaces import IBitaxeAPIClient
    >>> class MyClient(IBitaxeAPIClient):
    ...     def get_system_info(self):
    ...         return {"hashRate": 500}
    ...     def set_settings(self, voltage, frequency):
    ...         return frequency
    ...     def set_stratum(self, primary, backup):
    ...         return True
    ...     def restart(self):
    ...         return True
    >>> client = MyClient()
    >>> client.get_system_info()
    {'hashRate': 500}
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple


class IBitaxeAPIClient(ABC):
    """Interface for communicating with the Bitaxe miner hardware via an API."""

    @abstractmethod
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """
        Retrieve current system information from the miner.

        Returns:
            Optional[Dict[str, Any]]: System information as a dictionary (e.g., {"hashRate": 500, "temp": 48}), or None if unavailable.

        Example:
            >>> client.get_system_info()
            {'hashRate': 500.0, 'temp': 48, 'coreVoltageActual': 1200}
        """
        pass

    @abstractmethod
    def set_settings(self, voltage: float, frequency: float) -> float:
        """
        Set voltage and frequency on the miner and return the actual applied frequency.

        Args:
            voltage (float): Target core voltage to set (mV).
            frequency (float): Target frequency to set (MHz).

        Returns:
            float: The actual frequency applied by the miner (MHz).

        Example:
            >>> client.set_settings(1200, 485)
            485.0
        """
        pass

    @abstractmethod
    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """
        Configure primary and backup stratum pools.

        Args:
            primary (Dict[str, Any]): Configuration for the primary stratum pool (e.g., {"hostname": "solo.ckpool.org", "port": 3333}).
            backup (Dict[str, Any]): Configuration for the backup stratum pool (e.g., {"hostname": "pool.example.com", "port": 3333}).

        Returns:
            bool: True if the stratum settings were successfully applied, False otherwise.

        Example:
            >>> primary = {"hostname": "solo.ckpool.org", "port": 3333, "user": "user1"}
            >>> backup = {"hostname": "pool.example.com", "port": 3333, "user": "user2"}
            >>> client.set_stratum(primary, backup)
            True
        """
        pass

    @abstractmethod
    def restart(self) -> bool:
        """
        Restart the miner.

        Returns:
            bool: True if the restart was successful, False otherwise.

        Example:
            >>> client.restart()
            True
        """
        pass


class ILogger(ABC):
    """Interface for logging miner data and snapshots."""

    @abstractmethod
    def log_to_csv(
        self,
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
        Log miner performance data, including PID settings, to a CSV file.

        Args:
            timestamp (str): Time of the data point (e.g., "2025-03-11 10:00:00").
            target_frequency (float): Target frequency commanded by PID (MHz).
            target_voltage (float): Target core voltage commanded by PID (mV).
            hashrate (float): Measured hashrate (GH/s).
            temp (float): Measured temperature (°C).
            pid_settings (Dict[str, Any]): PID controller settings (e.g., {"PID_FREQ_KP": 0.2}).
            power (float): Measured power consumption (W).
            board_voltage (float): Measured board voltage (mV).
            current (float): Measured current (mA).
            core_voltage_actual (float): Actual core voltage (mV).
            frequency (float): Actual frequency (MHz).
            fanrpm (int): Fan speed (RPM).

        Example:
            >>> logger.log_to_csv("2025-03-11 10:00:00", 485, 1200, 500, 48, {"PID_FREQ_KP": 0.2}, 14.6, 4812.5, 3001.25, 1312, 485, 3870)
        """
        pass

    @abstractmethod
    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """
        Save current miner settings as a snapshot.

        Args:
            voltage (float): Current target voltage setting (mV).
            frequency (float): Current target frequency setting (MHz).

        Example:
            >>> logger.save_snapshot(1200, 485)
        """
        pass


class IConfigLoader(ABC):
    """Interface for loading configuration data from external sources."""

    @abstractmethod
    def load_config(self, file_path: str) -> Dict[str, Any]:
        """
        Load configuration settings from a file.

        Args:
            file_path (str): Path to the configuration file (e.g., "BM1366.yaml").

        Returns:
            Dict[str, Any]: Configuration data as a dictionary (e.g., {"INITIAL_VOLTAGE": 1200}).

        Example:
            >>> loader.load_config("BM1366.yaml")
            {'INITIAL_VOLTAGE': 1200, 'SAMPLE_INTERVAL': 5}
        """
        pass


class ITerminalUI(ABC):
    """Interface for terminal-based user interfaces to display miner statistics."""

    @abstractmethod
    def update(self, system_info: Dict[str, Any], voltage: float, frequency: float) -> None:
        """
        Update terminal UI with the latest miner data.

        Args:
            system_info (Dict[str, Any]): Current system information (e.g., {"hashRate": 500, "temp": 48}).
            voltage (float): Current target voltage setting (mV).
            frequency (float): Current target frequency setting (MHz).

        Example:
            >>> ui.update({"hashRate": 500, "temp": 48}, 1200, 485)
        """
        pass


class TuningStrategy(ABC):
    """Interface for tuning strategies managing miner settings adjustments."""

    @abstractmethod
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
            >>> strategy.apply_strategy(1200, 485, 48, 500, 14.6)
            (1220, 510)
        """
        pass