"""
Pools Management Module
This module manages mining pool endpoints, measures network latencies using t-digest,
and selects optimal pools based on performance. Latency data is stored per pool in
./pools/hostname-port.tdigest.json files, and medians are cached in pools.yaml.
Measurements are performed non-blocking in a background thread.
"""

import time
import socket
import yaml
import os
import json
import queue
import threading
from typing import List, Dict, Union, Optional, Any

# Third-party imports
from fastdigest import TDigest

# Ensure the ./pools/ directory exists for storing t-digest files
os.makedirs("./pools", exist_ok=True)

# Queue for pools needing latency updates
update_queue = queue.Queue()


def background_updater():
    """
    Background thread to process latency updates from the queue.
    """
    while True:
        pool = update_queue.get()
        update_pool_latency(pool)
        update_queue.task_done()


# Start the background thread as a daemon to handle updates non-blockingly
threading.Thread(target=background_updater, daemon=True).start()


# --- Helper Functions ---


def parse_endpoint(endpoint_str: str) -> tuple[str, int]:
    """
    Parse a pool endpoint string into hostname and port.

    Args:
        endpoint_str (str): Pool endpoint (e.g., 'stratum+tcp://stratum.solomining.io:7777').

    Returns:
        tuple[str, int]: Tuple of (hostname, port).

    Raises:
        ValueError: If the endpoint format is invalid.
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
        yaml_file (str): Path to the YAML file.

    Returns:
        List[Dict[str, Any]]: List of pool dictionaries.
    """
    try:
        with open(yaml_file, "r") as file:
            data = yaml.safe_load(file)
            return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error loading pools from {yaml_file}: {e}")
        return []


def load_tdigest(hostname: str, port: int) -> TDigest:
    """
    Load a t-digest from a JSON file or create a new one if not found.

    Args:
        hostname (str): Pool hostname.
        port (int): Pool port.

    Returns:
        TDigest: TDigest object.
    """
    filename = f"./pools/{hostname}-{port}.tdigest.json"
    if os.path.exists(filename):
        try:
            with open(filename, "r") as f:
                data = json.load(f)
                return TDigest.from_dict(data)
        except Exception as e:
            print(f"Error loading t-digest for {hostname}:{port}: {e}")
    return TDigest()


def save_tdigest(hostname: str, port: int, tdigest: TDigest) -> None:
    """
    Save a t-digest to a JSON file.

    Args:
        hostname (str): Pool hostname.
        port (int): Pool port.
        tdigest (TDigest): TDigest object to save.
    """
    filename = f"./pools/{hostname}-{port}.tdigest.json"
    try:
        with open(filename, "w") as f:
            json.dump(tdigest.to_dict(), f)
    except Exception as e:
        print(f"Error saving t-digest for {hostname}:{port}: {e}")


def measure_single_latency(hostname: str, port: int, timeout: float = 5.0) -> float:
    """
    Measure a single latency to a pool endpoint.

    Args:
        hostname (str): Pool hostname.
        port (int): Pool port.
        timeout (float): Connection timeout in seconds.

    Returns:
        float: Latency in milliseconds, or infinity if unreachable.
    """
    start_time = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((hostname, port))
        sock.send(b"\n")
        sock.close()
        return (time.time() - start_time) * 1000  # Convert to milliseconds
    except (socket.timeout, socket.error):
        return float("inf")


def update_single_pool(pool: Dict[str, Any], attempts: int = 5) -> float:
    """
    Update the t-digest for a pool with new latency measurements and return the median.

    Args:
        pool (Dict[str, Any]): Pool dictionary with 'endpoint'.
        attempts (int): Number of latency measurement attempts.

    Returns:
        float: Median latency in milliseconds, or infinity if no successful measurements.
    """
    endpoint_str = pool["endpoint"]
    hostname, port = parse_endpoint(endpoint_str)
    tdigest = load_tdigest(hostname, port)
    successful_updates = 0
    for _ in range(attempts):
        latency = measure_single_latency(hostname, port)
        if latency != float("inf"):
            tdigest.update(latency)
            successful_updates += 1
    save_tdigest(hostname, port, tdigest)
    if successful_updates > 0:
        return tdigest.quantile(0.5)
    else:
        print(f"No successful latency measurements for {endpoint_str}")
        return float("inf")


def update_pools_yaml(endpoint: str, median: float) -> None:
    """
    Update the median latency for a specific pool in pools.yaml atomically.

    Args:
        endpoint (str): Pool endpoint string.
        median (float): Median latency to write.
    """
    try:
        with open("pools.yaml", "r") as f:
            pools = yaml.safe_load(f)
        for pool in pools:
            if pool["endpoint"] == endpoint:
                pool["latency"] = median
                pool["last_tested"] = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        else:
            print(f"Pool {endpoint} not found in pools.yaml")
            return
        with open("pools.yaml.tmp", "w") as f:
            yaml.safe_dump(pools, f)
        os.replace("pools.yaml.tmp", "pools.yaml")
    except Exception as e:
        print(f"Error updating pools.yaml: {e}")


def update_pool_latency(pool: Dict[str, Any]) -> None:
    """
    Update latency for a single pool in the background and save to pools.yaml.

    Args:
        pool (Dict[str, Any]): Pool dictionary with 'endpoint'.
    """
    median = update_single_pool(pool)
    update_pools_yaml(pool["endpoint"], median)


# --- Core Functions ---


def measure_pools(yaml_file: str = "pools.yaml") -> List[Dict[str, Any]]:
    """
    Measure latencies for all pools synchronously and update pools.yaml.

    Args:
        yaml_file (str): Path to the YAML file.

    Returns:
        List[Dict[str, Any]]: Updated list of pool dictionaries.
    """
    pools = load_pools(yaml_file)
    for pool in pools:
        median = update_single_pool(pool)
        pool["latency"] = median
        pool["last_tested"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open("pools.yaml.tmp", "w") as f:
        yaml.safe_dump(pools, f)
    os.replace("pools.yaml.tmp", "pools.yaml")
    return pools


def get_fastest_pools(
    yaml_file: str = "pools.yaml",
    stratum_user: Optional[str] = None,
    fallback_stratum_user: Optional[str] = None,
    user_yaml: str = "user.yaml",
    force_measure: bool = False,
    latency_expiry_minutes: int = 15,
) -> List[Dict[str, Union[str, int]]]:
    """
    Retrieve the two fastest pools based on cached latencies, measuring if necessary.

    Args:
        yaml_file (str): Path to the YAML file.
        stratum_user (Optional[str]): Stratum user for primary pool.
        fallback_stratum_user (Optional[str]): Stratum user for backup pool.
        user_yaml (str): Path to user YAML file.
        force_measure (bool): Force new latency measurements.
        latency_expiry_minutes (int): Expiry time for latency data.

    Returns:
        List[Dict[str, Union[str, int]]]: List of up to two fastest pool dictionaries.
    """
    pools = load_pools(yaml_file)
    valid_pools = [p for p in pools if "latency" in p and p["latency"] != float("inf")]

    # If insufficient valid pools or forced, measure synchronously
    if len(valid_pools) < 2 or force_measure:
        print("Measuring all pools synchronously...")
        pools = measure_pools(yaml_file)
    else:
        # Queue pools needing updates
        current_time = time.time()
        for pool in pools:
            if "latency" not in pool or "last_tested" not in pool:
                update_queue.put(pool)
            else:
                try:
                    last_tested = time.strptime(
                        pool["last_tested"], "%Y-%m-%d %H:%M:%S"
                    )
                    last_tested_ts = time.mktime(last_tested)
                    if (current_time - last_tested_ts) / 60 > latency_expiry_minutes:
                        update_queue.put(pool)
                except:
                    update_queue.put(pool)

    # Select the two fastest pools
    valid_pools = [p for p in pools if p.get("latency", float("inf")) != float("inf")]
    sorted_pools = sorted(valid_pools, key=lambda x: x["latency"])[:2]

    if not sorted_pools:
        print("No valid pools found.")
        return []

    if len(sorted_pools) < 2:
        print("Warning: Only one valid pool found. Duplicating for backup.")
        sorted_pools.append(sorted_pools[0].copy())

    # Load default users if not provided
    if stratum_user is None or fallback_stratum_user is None:
        try:
            with open(user_yaml, "r") as f:
                user_config = yaml.safe_load(f) or {}
            default_stratum_user = user_config.get("stratumUser", "")
            default_fallback_user = user_config.get(
                "fallbackStratumUser", default_stratum_user
            )
        except:
            default_stratum_user = default_fallback_user = ""
    else:
        default_stratum_user = stratum_user
        default_fallback_user = fallback_stratum_user

    sorted_pools[0]["user"] = stratum_user if stratum_user else default_stratum_user
    sorted_pools[1]["user"] = (
        fallback_stratum_user if fallback_stratum_user else default_fallback_user
    )

    print("\nSelected pools:")
    for i, pool in enumerate(sorted_pools):
        print(
            f"{'Primary' if i == 0 else 'Backup'} pool: "
            f"{pool['endpoint']} (latency: {pool['latency']:.0f}ms, "
            f"last tested: {pool['last_tested']})"
        )

    return sorted_pools


def main():
    """
    Measure pool latencies and output them in YAML format.
    """
    pools_with_latency = measure_pools()
    print("\nCurrent pool latencies:")
    print(yaml.safe_dump(pools_with_latency, default_flow_style=False))


if __name__ == "__main__":
    main()
