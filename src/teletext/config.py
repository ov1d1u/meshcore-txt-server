"""Configuration for the USB Teletext service."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """The server configuration is missing or invalid."""


@dataclass(frozen=True)
class ServerConfig:
    node_name: str


def load_config(path: Path) -> ServerConfig:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as source:
            parser.read_file(source)
    except (OSError, UnicodeError, configparser.Error) as exc:
        raise ConfigError(f"Could not read configuration {path}: {exc}") from exc

    if not parser.has_section("server") or not parser.has_option("server", "node_name"):
        raise ConfigError(f"{path} must contain [server] node_name")
    name = parser.get("server", "node_name").strip()
    if not name:
        raise ConfigError("[server] node_name must not be empty")
    name = name[:-4] + "-txt" if name.lower().endswith("-txt") else name + "-txt"
    # MeshCore advertisements with location data leave 23 bytes for the name.
    if len(name.encode("utf-8")) > 23:
        raise ConfigError("[server] node_name with -txt must fit in 23 UTF-8 bytes")
    return ServerConfig(node_name=name)
