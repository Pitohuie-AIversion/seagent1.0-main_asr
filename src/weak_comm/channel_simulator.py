"""Underwater acoustic channel simulator for packet loss and bit corruption testing."""
from __future__ import annotations

import random
from typing import Sequence


class AcousticChannelSimulator:
    """Simulates physical impairments of subsea acoustic channels."""

    def __init__(
        self,
        packet_loss_rate: float = 0.3,
        bit_error_rate: float = 0.0,
        random_seed: int | None = None,
    ):
        self.packet_loss_rate = packet_loss_rate
        self.bit_error_rate = bit_error_rate
        self._rng = random.Random(random_seed)
        self.packets_transmitted = 0
        self.packets_dropped = 0
        self.packets_corrupted = 0

    def transmit(self, data: bytes) -> bytes | None:
        """Simulate packet transmission over acoustic channel."""
        self.packets_transmitted += 1

        # Simulate packet loss
        if self._rng.random() < self.packet_loss_rate:
            self.packets_dropped += 1
            return None

        # Simulate bit error corruption
        if self.bit_error_rate > 0.0 and self._rng.random() < self.bit_error_rate:
            self.packets_corrupted += 1
            corrupted = bytearray(data)
            flip_idx = self._rng.randint(0, len(corrupted) - 1)
            corrupted[flip_idx] ^= 0x01
            return bytes(corrupted)

        return data

    def reset_stats(self) -> None:
        self.packets_transmitted = 0
        self.packets_dropped = 0
        self.packets_corrupted = 0
