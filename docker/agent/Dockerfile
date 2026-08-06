FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/agent
COPY docker/agent/requirements.txt /opt/agent/requirements.txt
RUN pip install --no-cache-dir -r /opt/agent/requirements.txt

COPY docker/agent/run_agent.py /opt/agent/run_agent.py

WORKDIR /workspace
ENTRYPOINT ["python", "-u", "/opt/agent/run_agent.py"]
