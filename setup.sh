#!/bin/bash

# Create a virtual environment
uv venv

# Activate the virtual environment
source .venv/bin/activate

# Install required Python packages
# uv pip install pyfiglet rich simple-pid logging requests argparse
uv pip install --requirement requirements.txt

# Deactivate the virtual environment
deactivate

