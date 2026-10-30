"""
Where is the app running? Lets one .env work on a laptop, in Docker and on a VM.

Inside a container, "localhost" is the container itself, not the machine that runs Docker. When a service
(PostgreSQL, Ollama) is configured as localhost and the app is in a container, we point it at the Docker host
instead (docker-compose.yml maps host.docker.internal to it). Disable with REWRITE_LOCALHOST_IN_DOCKER=false.
"""
import os
from typing import Optional

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
DOCKER_HOST_ALIAS = "host.docker.internal"


def env_true(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def in_container() -> bool:
    return env_true("RUNNING_IN_DOCKER", False) or os.path.exists("/.dockerenv")


def container_host(host: Optional[str]) -> Optional[str]:
    """Map a loopback host to the Docker host when running in a container; otherwise return it unchanged."""
    if host and host.strip().lower() in LOCAL_HOSTS and in_container() and env_true("REWRITE_LOCALHOST_IN_DOCKER", True):
        return DOCKER_HOST_ALIAS
    return host
