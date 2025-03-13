# Use the official Python base image
FROM python:latest

# Add metadata about the maintainer
LABEL maintainer="bitaxepid@starficient.com"

# Copy the requirements file into the container
COPY requirements.txt requirements.txt

# Install the dependencies listed in requirements.txt
RUN pip3 install -r requirements.txt

# Copy all required application files into the container
COPY BM1366.yaml BM1366.yaml
COPY BM1368.yaml BM1368.yaml
COPY BM1370.yaml BM1370.yaml
COPY BM1397.yaml BM1397.yaml
COPY bitaxepid.py bitaxepid.py
COPY implementations.py implementations.py
COPY interfaces.py interfaces.py
COPY pools.py pools.py
COPY pools.yaml pools.yaml
COPY pools2.yaml pools2.yaml
COPY setup.sh setup.sh
COPY start.sh start.sh
COPY user.yaml user.yaml

# Ensure the shell scripts are executable
RUN chmod +x setup.sh start.sh
EXPOSE 8093
# Set the default command to run the start.sh script
ENTRYPOINT ["bash", "./start.sh"]

