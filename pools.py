"""
Pools Management Module
This module provides functions for managing mining pool endpoints, measuring network latencies,
and selecting optimal mining pools based on connectivity performance. It facilitates loading pool
information from YAML configuration files, performing latency tests, and identifying the fastest pools.
Auxiliary reflection functions are included to support introspection and debugging.

Latency measurements are cached in pools.yaml and refreshed every 15 minutes by default.
"""

import time
import socket
import yaml
import statistics
import inspect
from typing import List, Dict, Union, Optional, Any
import os


# --- Pool Management Functions ---
def parse_endpoint(endpoint_str: str) -> tuple[str, int]:
    """
    Parses a pool endpoint string into hostname and port components.
    Args:
        endpoint_str: Pool endpoint string (e.g., 'stratum+tcp://host:port').
    Returns:
        A tuple containing hostname and port number.
    Raises:
        ValueError: If the endpoint format is invalid or missing port.
    Example:
        >>> parse_endpoint('stratum+tcp://solo.ckpool.org:3333')
        ('solo.ckpool.org', 3333)
    """
    if endpoint_str.startswith("stratum+tcp://"):
        endpoint_str = endpoint_str[len("stratum+tcp://") :]
    if ":" in endpoint_str:
        hostname, port_str = endpoint_str.split(":", 1)
        return hostname, int(port_str)
    raise ValueError(f"Invalid endpoint, missing port: {endpoint_str}")


def load_pools(yaml_file: str = "pools.yaml") -> List[Dict[str, Any]]:
    """
    Load mining pool configurations from a YAML file.
    Args:
        yaml_file: Path to the YAML file containing pool data.
    Returns:
        List of dictionaries containing pool details (endpoint, fee, latency, last_tested).
    """
    try:
        with open(yaml_file, "r") as file:
            data = yaml.safe_load(file)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error loading pools from {yaml_file}: {e}")
        return []


def load_user_yaml(user_yaml: str = "user.yaml") -> Dict[str, str]:
    """
    Load user configuration from a YAML file.
    Args:
        user_yaml: Path to the user YAML file.
    Returns:
        Dictionary containing user configurations (stratumUser, fallbackStratumUser).
    """
    try:
        with open(user_yaml, "r") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        print(f"User YAML file {user_yaml} not found. Using empty user configurations.")
        return {}


def measure_latency(
    endpoint: str,
    port: int,
    timeout: float = 5.0,
    attempts: int = 5,
    delay: float = 0.5,
) -> float:
    """
    Measures the median latency to a given network endpoint with thorough testing.
    Args:
        endpoint: Hostname of the pool (e.g., 'solo.ckpool.org').
        port: Port number of the pool (e.g., 3333).
        timeout: Timeout for each connection attempt in seconds.
        attempts: Number of attempts to measure latency.
        delay: Delay between attempts in seconds.
    Returns:
        Median latency in milliseconds, or infinity if unreachable.
    """
    latencies = []
    print(f"Testing latency for {endpoint}:{port}")

    for i in range(attempts):
        start_time = time.time()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((endpoint, port))
            # Send a dummy request to ensure connection is fully established
            sock.send(b"\n")
            sock.close()
            latency = (time.time() - start_time) * 1000  # Convert to milliseconds
            latencies.append(latency)
            print(f"Attempt {i+1}/{attempts}: {latency:.0f}ms")
        except (socket.timeout, socket.error) as e:
            print(f"Attempt {i+1}/{attempts}: Failed ({str(e)})")
            latencies.append(float("inf"))
        time.sleep(delay)

    median_latency = statistics.median(latencies) if latencies else float("inf")
    print(f"Median latency: {median_latency:.0f}ms")
    return median_latency


def measure_pools(yaml_file: str = "pools.yaml") -> List[Dict[str, Any]]:
    """
    Loads pools from a YAML file, measures latency for each, and saves results back to file
    while preserving existing pool information.
    Args:
        yaml_file: Path to the YAML file containing pool data.
    Returns:
        List of pool dictionaries with updated latency measurements and timestamps.
    """
    # First verify we can read the file
    try:
        with open(yaml_file, "r") as f:
            pools = yaml.safe_load(f)
            if not isinstance(pools, list):
                print(f"Error: Invalid pools data format in {yaml_file}")
                return []
    except Exception as e:
        print(f"Error reading {yaml_file}: {e}")
        return []

    print(f"\nMeasuring latency for {len(pools)} pools...")
    updated_pools = []

    for pool in pools:
        try:
            endpoint_str = pool["endpoint"]
            hostname, port = parse_endpoint(endpoint_str)
            latency = measure_latency(hostname, port)

            # Create new dict with all existing data plus latency info
            updated_pool = pool.copy()
            updated_pool.update(
                {
                    "latency": latency,
                    "port": port,
                    "last_tested": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            updated_pools.append(updated_pool)

            print(f"Updated pool data for {endpoint_str}: latency={latency:.0f}ms")

        except ValueError as e:
            print(f"Error parsing endpoint {endpoint_str}: {e}")
            updated_pool = pool.copy()
            updated_pool.update(
                {
                    "latency": float("inf"),
                    "port": 0,
                    "last_tested": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            updated_pools.append(updated_pool)

    # Try to save the updated data
    try:
        # First write to a temporary file
        temp_file = f"{yaml_file}.tmp"
        with open(temp_file, "w") as f:
            yaml.safe_dump(updated_pools, f, default_flow_style=False, sort_keys=False)

        # If successful, rename to the actual file
        import os

        os.replace(temp_file, yaml_file)
        print(f"\nSuccessfully updated {yaml_file} with new latency data")

        # Verify the file was written correctly
        with open(yaml_file, "r") as f:
            verify_pools = yaml.safe_load(f)
            if not verify_pools or len(verify_pools) != len(pools):
                print(f"Warning: File verification failed for {yaml_file}")
            else:
                print(f"File verification successful: {len(verify_pools)} pools saved")

    except Exception as e:
        print(f"Error saving pool data to {yaml_file}: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass
        return updated_pools

    return updated_pools


def get_fastest_pools(
    yaml_file: str = "pools.yaml",
    stratum_user: Optional[str] = None,
    fallback_stratum_user: Optional[str] = None,
    user_yaml: str = "user.yaml",
    force_measure: bool = False,
    latency_expiry_minutes: int = 15,
) -> List[Dict[str, Union[str, int]]]:
    """
    Retrieves the two fastest pools, measuring latency if expired or forced.
    Args:
        yaml_file: Path to the YAML file containing pool data.
        stratum_user: Optional stratum user for primary pool.
        fallback_stratum_user: Optional stratum user for backup pool.
        user_yaml: Path to the user YAML file for default users.
        force_measure: If True, force new latency measurements.
        latency_expiry_minutes: Minutes before latency measurements expire (default 15).
    Returns:
        List of up to two fastest pools with latency, port, and user keys.
    """
    # Load existing pools first
    pools = load_pools(yaml_file)

    # Check if we need to measure latencies
    current_time = time.time()
    need_measure = force_measure

    if not need_measure:
        for pool in pools:
            if "latency" not in pool or "last_tested" not in pool:
                need_measure = True
                break
            try:
                # Convert last_tested string to timestamp
                last_tested = time.strptime(pool["last_tested"], "%Y-%m-%d %H:%M:%S")
                last_tested_timestamp = time.mktime(last_tested)

                # Check if latency measurement has expired
                minutes_since_test = (current_time - last_tested_timestamp) / 60
                if minutes_since_test > latency_expiry_minutes:
                    print(
                        f"Latency data expired for {pool['endpoint']} "
                        f"(last tested: {pool['last_tested']}, "
                        f"{minutes_since_test:.1f} minutes ago)"
                    )
                    need_measure = True
                    break
            except (ValueError, KeyError) as e:
                print(f"Error checking latency expiry: {e}")
                need_measure = True
                break

    if need_measure:
        print("Measuring pool latencies...")
        pools = measure_pools(yaml_file)
    else:
        print("Using cached pool latencies")

    valid_pools = [
        pool for pool in pools if pool.get("latency", float("inf")) != float("inf")
    ]
    sorted_pools = sorted(valid_pools, key=lambda x: x.get("latency", float("inf")))[:2]

    if not sorted_pools:
        print("No valid pools found.")
        return []

    if len(sorted_pools) < 2:
        print("Warning: Only one valid pool found. Duplicating for backup.")
        sorted_pools.append(sorted_pools[0].copy())

    # Load default users from user.yaml if not provided
    if stratum_user is None or fallback_stratum_user is None:
        user_config = load_user_yaml(user_yaml)
        default_stratum_user = user_config.get("stratumUser", "")
        default_fallback_user = user_config.get(
            "fallbackStratumUser", default_stratum_user
        )
    else:
        default_stratum_user = stratum_user
        default_fallback_user = fallback_stratum_user

    # Assign users: use provided values if available, otherwise fall back to user.yaml defaults
    sorted_pools[0]["user"] = (
        stratum_user if stratum_user is not None else default_stratum_user
    )
    sorted_pools[1]["user"] = (
        fallback_stratum_user
        if fallback_stratum_user is not None
        else default_fallback_user
    )

    # Log selected pools
    print(f"\nSelected pools:")
    for i, pool in enumerate(sorted_pools):
        print(
            f"{'Primary' if i == 0 else 'Backup'} pool: "
            f"{pool['endpoint']} (latency: {pool['latency']:.0f}ms, "
            f"last tested: {pool['last_tested']})"
        )

    return sorted_pools


def test_file_permissions(yaml_file: str = "pools.yaml") -> bool:
    """
    Test if we have proper file permissions for the pools file.
    """
    try:
        # Test read
        with open(yaml_file, "r") as f:
            data = yaml.safe_load(f)
            print(f"Successfully read {yaml_file}")

        # Test write
        with open(f"{yaml_file}.test", "w") as f:
            yaml.safe_dump(data, f)
        print(f"Successfully wrote test file")

        # Clean up test file
        os.remove(f"{yaml_file}.test")
        print(f"Successfully cleaned up test file")

        return True
    except Exception as e:
        print(f"File permission test failed: {e}")
        return False


# Add to main function:
def main() -> None:
    """
    Main function to measure pool latencies and output them in YAML format.
    """
    if not test_file_permissions():
        print("ERROR: File permission test failed. Please check file permissions.")
        return

    pools_with_latency = measure_pools()
    print("\nCurrent pool latencies:")
    print(yaml.safe_dump(pools_with_latency, default_flow_style=False))


if __name__ == "__main__":
    main()
