"""
Module for measuring latency of Bitcoin mining pools, sorting them by performance,
and outputting a YAML list of the two pools with the lowest latency.

This module loads pool configurations from a YAML file, measures the TCP connection 
latency to each pool's endpoint, and outputs a YAML document containing the two fastest 
pools. Each YAML entry contains an "endpoint" and "port" field.

Usage:
    Run the script directly to test and rank pools from 'pools.yaml':
    ```bash
    python pools.py
    ```
"""

import time
import socket
import yaml
from typing import List, Dict, Union, Optional, Any
import inspect
import builtins

# --- Reflection Support Functions ---

def get_builtins() -> dict:
    """
    Mimics PyEval_GetBuiltins(): Returns the dictionary of built-in objects.

    Returns:
        dict: A dictionary containing the built-in namespace (borrowed reference).
    """
    return builtins.__dict__

def get_locals() -> Optional[dict]:
    """
    Mimics PyEval_GetLocals(): Returns the local variables in the current frame.

    Returns:
        Optional[dict]: Dictionary of local variables if a frame exists, else None.
    """
    frame = inspect.currentframe().f_back
    return frame.f_locals if frame else None

def get_globals() -> Optional[dict]:
    """
    Mimics PyEval_GetGlobals(): Returns the global variables in the current frame.

    Returns:
        Optional[dict]: Dictionary of global variables if a frame exists, else None.
    """
    frame = inspect.currentframe().f_back
    return frame.f_globals if frame else None

def get_frame() -> Optional[inspect.FrameInfo]:
    """
    Mimics PyEval_GetFrame(): Returns the current execution frame.

    Returns:
        Optional[inspect.FrameInfo]: The current frame object if it exists, else None.
    """
    return inspect.currentframe().f_back

def get_frame_line_number(frame: Optional[inspect.FrameInfo]) -> int:
    """
    Mimics PyFrame_GetLineNumber(): Returns the line number of the given frame.

    Args:
        frame (Optional[inspect.FrameInfo]): The frame object to inspect.

    Returns:
        int: The line number, or 0 if frame is None.
    """
    return frame.f_lineno if frame else 0

def get_func_name(func: Any) -> str:
    """
    Mimics PyEval_GetFuncName(): Returns the name of a function or its type.

    Args:
        func (Any): The object (function, class, instance, etc.) to name.

    Returns:
        str: The name of the function or its type.
    """
    if hasattr(func, '__name__'):
        return func.__name__
    return type(func).__name__

def get_func_desc(func: Any) -> str:
    """
    Mimics PyEval_GetFuncDesc(): Returns a description of the function type.

    Args:
        func (Any): The object to describe.

    Returns:
        str: A description like '()', 'constructor', 'instance', or 'object'.

    Example:
        >>> def foo(): pass
        >>> get_func_desc(foo)
        '()'
    """
    if inspect.isfunction(func) or inspect.ismethod(func):
        return '()'
    elif inspect.isclass(func):
        return 'constructor'
    elif hasattr(func, '__call__'):
        return 'instance'
    return 'object'

# --- Pool Management Functions ---

def parse_endpoint(endpoint_str: str) -> tuple[str, int]:
    """
    Extracts hostname and port from an endpoint string.

    Expects formats like 'stratum+tcp://host:port'. Raises an error if port is missing.

    Args:
        endpoint_str (str): The endpoint string to parse (e.g., 'stratum+tcp://btc.global.luxor.tech:700').

    Returns:
        tuple[str, int]: A tuple of (hostname, port).

    Raises:
        ValueError: If the port cannot be extracted from the endpoint string.

    Example:
        >>> parse_endpoint('stratum+tcp://btc.global.luxor.tech:700')
        ('btc.global.luxor.tech', 700)
    """
    # Strip protocol if present
    if endpoint_str.startswith("stratum+tcp://"):
        endpoint_str = endpoint_str[len("stratum+tcp://"):]
    # Split hostname and port
    if ":" in endpoint_str:
        hostname, port_str = endpoint_str.split(":", 1)
        return hostname, int(port_str)
    raise ValueError(f"Port not found in endpoint: {endpoint_str}")

def load_pools(yaml_file: str = "pools.yaml") -> List[Dict[str, Any]]:
    """
    Loads pool data from a YAML file.

    Args:
        yaml_file (str): Path to the YAML file. Defaults to 'pools.yaml'.

    Returns:
        List[Dict[str, Any]]: List of pool dictionaries with endpoint, port, fee, and www.

    Raises:
        FileNotFoundError: If the YAML file cannot be found.
        yaml.YAMLError: If the YAML file is malformed.
        ValueError: If an endpoint lacks a port.
    """
    with open(yaml_file, "r") as f:
        pools = yaml.safe_load(f)
    
    processed_pools = []
    for pool in pools:
        endpoint_str = pool.get("endpoint", "")
        hostname, port = parse_endpoint(endpoint_str)
        
        processed_pools.append({
            "endpoint": hostname,
            "port": port,
            "fee": pool.get("fee"),
            "www": pool.get("www")
        })
    return processed_pools

def measure_latency(endpoint: str, port: int, timeout: float = 3.0) -> float:
    """
    Measures TCP connection latency to an endpoint in milliseconds.

    Args:
        endpoint (str): The hostname or IP address to connect to.
        port (int): The port number to test.
        timeout (float): Connection timeout in seconds. Defaults to 3.0.

    Returns:
        float: Latency in milliseconds, or infinity if the connection fails.

    Example:
        >>> measure_latency('btc.global.luxor.tech', 700)
        45.23  # Example latency in ms
    """
    try:
        start = time.time()
        sock = socket.create_connection((endpoint, port), timeout=timeout)
        latency = (time.time() - start) * 1000  # Convert to milliseconds
        sock.close()
        return latency
    except (socket.error, socket.timeout):
        return float('inf')

def main() -> None:
    """
    Main function to measure pool latencies, determine the two fastest pools,
    and output their details in YAML format.

    Loads pools from 'pools.yaml', measures latency, sorts by performance,
    and prints a YAML document of the two pools with the lowest latency.
    Each entry in the output contains:
      - endpoint: Hostname of the pool.
      - port: Port number of the pool.
    """
    pools = load_pools("pools.yaml")
    results: List[Dict[str, Union[str, int, float]]] = []
    
    for pool in pools:
        endpoint = pool["endpoint"]
        port = pool["port"]
        latency = measure_latency(endpoint, port)
        results.append({
            "endpoint": endpoint,
            "port": port,
            "latency": latency
        })
    
    # Sort results by latency (lowest first)
    results.sort(key=lambda x: x["latency"])
    
    # Collect the top two reachable endpoints
    top_two = []
    for res in results:
        if res["latency"] < float('inf'):
            top_two.append({
                "endpoint": res["endpoint"],
                "port": res["port"]
            })
            if len(top_two) == 2:
                break
    
    if len(top_two) < 2:
        print("Less than two endpoints were reachable.")
    else:
        # Output the result as YAML
        print(yaml.safe_dump(top_two, default_flow_style=False))

if __name__ == "__main__":
    main()

