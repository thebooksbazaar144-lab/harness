"""Security boundaries.

- SSRF protection (on by default)
- Target validation against allowlists
- local_lab mode for authorized private targets
- Cloud-metadata endpoints are always rejected
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse

from .config import Settings

# Always-block targets regardless of allowlist (cloud metadata, link-local
# control planes commonly used for SSRF pivots).
_ALWAYS_BLOCK_HOSTS = {
    "169.254.169.254",  # AWS / GCP / Azure metadata
    "metadata.google.internal",  # GCP metadata v1
    "fd00:ec2::254",  # AWS IMDSv6
}


@dataclass(frozen=True)
class TargetDecision:
    allowed: bool
    reason: str
    host: str
    port: int | None
    scheme: str
    is_private: bool


class TargetValidationError(PermissionError):
    pass


def _is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # It's a hostname. Resolve it. If it resolves to a private IP, treat
        # as private (prevents DNS-based SSRF bypass).
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return False
        for info in infos:
            ip_str = info[4][0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                continue
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)


def parse_target(target: str) -> tuple[str, str, int | None]:
    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        # Allow bare host:port
        parsed = urlparse(f"http://{target}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise TargetValidationError(f"unsupported scheme: {scheme}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise TargetValidationError(f"missing host in target: {target!r}")
    port = parsed.port
    return scheme, host, port


def host_port_str(host: str, port: int | None) -> str:
    return f"{host}:{port}" if port else host


def validate_target(target: str, settings: Settings) -> TargetDecision:
    """Validate a target URL against the security policy.

    Raises TargetValidationError (a PermissionError) on rejection.
    """
    scheme, host, port = parse_target(target)
    host_port = host_port_str(host, port)

    if host in _ALWAYS_BLOCK_HOSTS:
        raise TargetValidationError(
            f"blocked SSRF target: {host} (cloud-metadata / control plane)"
        )

    is_private = _is_private_host(host)

    if is_private:
        if not settings.local_lab:
            raise TargetValidationError(
                f"private-network target {host!r} rejected: local_lab is off"
            )
        allowed_priv = set(settings.allowed_private_targets_list)
        # Allow matching by host or host:port
        if host not in allowed_priv and host_port not in allowed_priv:
            raise TargetValidationError(
                f"private target {host!r} not in allowed_private_targets"
            )
        return TargetDecision(
            allowed=True,
            reason="local_lab allowlist",
            host=host,
            port=port,
            scheme=scheme,
            is_private=True,
        )

    # Public host: optional allowlist when configured.
    allow_domains = settings.allowed_domains_list
    if allow_domains:
        # Match by suffix so "example.com" allows "www.example.com".
        if not any(host == d or host.endswith("." + d) for d in allow_domains):
            raise TargetValidationError(
                f"host {host!r} not in allowed_domains"
            )

    return TargetDecision(
        allowed=True,
        reason="public target",
        host=host,
        port=port,
        scheme=scheme,
        is_private=is_private,
    )


def validate_targets(targets: Iterable[str], settings: Settings) -> list[TargetDecision]:
    return [validate_target(t, settings) for t in targets]
