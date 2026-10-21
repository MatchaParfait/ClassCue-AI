"""Exclusive socket binding shared by the launcher and API child."""

from __future__ import annotations

import os
import socket


API_BIND_FAILED = 75


def bind_server_socket(host: str, port: int) -> socket.socket:
    bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows SO_REUSEADDR can silently share a foreign process's port.
        if os.name == "nt":
            bound.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        bound.bind((host, port))
        return bound
    except BaseException:
        bound.close()
        raise
