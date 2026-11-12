"""CRC-16-CCITT calculation for underwater acoustic weak-communication verification."""
from __future__ import annotations


def crc16_ccitt(data: bytes, initial: int = 0xFFFF) -> int:
    """Calculate CRC-16-CCITT checksum (polynomial 0x1021)."""
    crc = initial
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc & 0xFFFF
