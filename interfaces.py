#!/usr/bin/env python3
"""
Interfaces Module for BitaxePID Auto-Tuner

This module defines abstract base classes (ABCs) for components used in the
BitaxePID Auto-Tuner, ensuring a consistent interface across implementations.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple


class IBitaxeAPIClient(ABC):
    @abstractmethod
    def get_system_info(self) -> Optional[Dict[str, Any]]:
        """Retrieve system information from the Bitaxe miner."""
        pass

    @abstractmethod
    def set_settings(self, voltage: float, frequency: float) -> float:
        """Set the voltage and frequency settings on the Bitaxe miner."""
        pass

    @abstractmethod
    def set_stratum(self, primary: Dict[str, Any], backup: Dict[str, Any]) -> bool:
        """Configure the primary and backup stratum servers."""
        pass

    @abstractmethod
    def restart(self) -> bool:
        """Restart the Bitaxe miner and verify it comes back online."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close any open connections or resources used by the API client."""
        pass


class TuningStrategy(ABC):
    @abstractmethod
    def apply_strategy(
        self,
        current_voltage: float,
        current_frequency: float,
        temp: float,
        power: float,
    ) -> Tuple[float, float, Tuple[float, float, float], Tuple[float, float, float]]:
        """
        Calculate new voltage and frequency settings based on current conditions.

        Args:
            current_voltage (float): Current target voltage in millivolts (mV).
            current_frequency (float): Current target frequency in megahertz (MHz).
            temp (float): Median temperature over the last 60 seconds (°C).
            power (float): Median power consumption over the last 60 seconds (watts).

        Returns:
            Tuple containing:
            - float: New voltage in millivolts (mV).
            - float: New frequency in megahertz (MHz).
            - Tuple[float, float, float]: PID terms (P, I, D) for frequency controller.
            - Tuple[float, float, float]: PID terms (P, I, D) for voltage controller.
        """
        pass


class ILogger(ABC):
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
        """Log miner performance data to a CSV file."""
        pass

    @abstractmethod
    def save_snapshot(self, voltage: float, frequency: float) -> None:
        """Save a snapshot of the current voltage and frequency settings."""
        pass


class IConfigLoader(ABC):
    @abstractmethod
    def load_config(self, file_path: str) -> Dict[str, Any]:
        """Load configuration data from a specified file path."""
        pass


class ITerminalUI(ABC):
    @abstractmethod
    def update(
        self, system_info: Dict[str, Any], voltage: float, frequency: float
    ) -> None:
        """Update the terminal UI with the latest system info and settings."""
        pass
