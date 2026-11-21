"""Wake-on-LAN magic packets (the TV must have "Turn on via Wi-Fi/LAN" enabled)."""
from __future__ import annotations

import ipaddress
import re
import socket

_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}([:-]?[0-9a-fA-F]{2}){5}$")


def parse_mac(mac: str) -> bytes:
    if not _MAC_RE.match(mac.strip()):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return bytes.fromhex(re.sub(r"[:-]", "", mac.strip()))


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + parse_mac(mac) * 16


def broadcast_targets(tv_ip: str | None) -> list[str]:
    """Limited broadcast plus the TV's /24 directed broadcast (home LAN assumption)."""
    targets = ["255.255.255.255"]
    if tv_ip:
        net = ipaddress.ip_network(f"{tv_ip}/24", strict=False)
        targets.append(str(net.broadcast_address))
    return targets


def send_wol(mac: str, targets: list[str], repeats: int = 3) -> None:
    pkt = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(repeats):
            for dst in targets:
                for port in (9, 7):
                    s.sendto(pkt, (dst, port))
