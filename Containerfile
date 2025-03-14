# Use the official Python base image
FROM python:latest

# Add metadata about the maintainer
LABEL maintainer="bitaxepid@starficient.com"

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip3 install -r requirements.txt

# Copy YAML and Python files into the container
COPY *.yaml *.py ./

# Copy shell scripts and make them executable
COPY setup.sh start.sh ./
RUN chmod +x setup.sh start.sh

# Expose port
EXPOSE 8093

# Set the default command
# podman run -it --publish 8093:8093 bitaxepid-container 192.168.68.111
# podman run --publish 8093:8093 bitaxepid-container 192.168.68.111

ENTRYPOINT ["bash", "./start.sh"]
