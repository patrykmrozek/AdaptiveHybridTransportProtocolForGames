"""
Network Emulator for simulating real-world network conditions.

This module provides packet loss, delay, and jitter emulation for QUIC applications.
"""

import asyncio
import random
from typing import Callable, Optional, Set


class NetworkEmulator:
    """
    Emulates network conditions (packet loss, delay, jitter) for QUIC packets.
    
    Usage:
        emulator = NetworkEmulator(enabled=True, delay_ms=50, jitter_ms=10, packet_loss_rate=0.05)
        await emulator.transmit(original_transmit_func)
        
        # For selective dropping (test retransmission):
        emulator = NetworkEmulator(enabled=True, packet_loss_rate=0.0, drop_sequences={3, 7, 12})
        await emulator.transmit(original_transmit_func, packet_info={"seq": 3, "type": "reliable"})
    """
    
    def __init__(self, enabled: bool = False, delay_ms: float = 0, jitter_ms: float = 0, 
                 packet_loss_rate: float = 0.0, drop_sequences: Optional[Set[int]] = None,
                 drop_once: bool = True):
        """
        Initialize network emulator.
        
        Args:
            enabled: If False, transmit() just forwards to original (no emulation)
            delay_ms: Base delay in milliseconds
            jitter_ms: Jitter variation in milliseconds (±jitter/2)
            packet_loss_rate: Probability of packet loss (0.0 to 1.0)
            drop_sequences: Set of sequence numbers to selectively drop (for reliable packets only)
        """
        self.enabled = enabled
        self.delay_ms = delay_ms
        self.jitter_ms = jitter_ms
        self.packet_loss_rate = packet_loss_rate
        self.drop_sequences = drop_sequences or set()
        self._dropped_count = 0
        self._total_count = 0
        self.drop_once = drop_once
        self._already_dropped: Set[int] = set()
    
    async def transmit(self, original_transmit_func: Callable, packet_info: Optional[dict] = None):
        """
        Drop-in replacement for transmit() that applies network emulation.
        
        Args:
            original_transmit_func: The original transmit() function to call
            packet_info: Optional dict with packet metadata, e.g., {"seq": 5, "type": "reliable"}
        """
        self._total_count += 1
        
        # If emulation is disabled, just forward immediately
        if not self.enabled:
            original_transmit_func()
            return
        
        # Check for selective sequence dropping (for reliable packets from client)
        if packet_info and packet_info.get("type") == "reliable":
            seq = packet_info.get("seq")
            if seq is not None and seq in self.drop_sequences:
                #print(f"[emulator]  🎯 DROP: Seq={seq}")
                if (self.drop_once and seq not in self._already_dropped) or (not self.drop_once):
                    #print(f"[emulator] ALREADY DROPPED: Seq={seq} ({len(self._already_dropped)} times)")
                    self._dropped_count += 1
                    self._already_dropped.add(seq)  # future retries go through
                    return
        
        # Check for random packet loss (only if not using selective dropping, or for non-reliable packets)
        if self.packet_loss_rate > 0:
            if random.random() < self.packet_loss_rate:
                self._dropped_count += 1
                print(f"[emulator] 📦 Packet DROPPED (loss rate: {self.packet_loss_rate:.2%}, "
                      f"total dropped: {self._dropped_count}/{self._total_count})")
                return  # Don't call transmit() - packet is lost
        
        # Apply delay with jitter
        if self.delay_ms > 0:
            # Calculate actual delay: base_delay + random jitter (-jitter/2 to +jitter/2)
            jitter_offset = random.uniform(-self.jitter_ms / 2, self.jitter_ms / 2)
            actual_delay_ms = max(0, self.delay_ms + jitter_offset)  # Ensure non-negative
            
            # Convert to seconds and sleep
            await asyncio.sleep(actual_delay_ms / 1000.0)
        
        # Call original transmit function
        original_transmit_func()
    
    def get_stats(self) -> dict:
        """Get emulation statistics."""
        return {
            "enabled": self.enabled,
            "total_packets": self._total_count,
            "dropped_packets": self._dropped_count,
            "drop_rate": self._dropped_count / max(1, self._total_count),
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "packet_loss_rate": self.packet_loss_rate,
            "drop_sequences": self.drop_sequences
        }

    def should_drop_unreliable_frame(self) -> bool:
        if not self.enabled or self.packet_loss_rate <= 0:
            self._total_count += 1
            return False
        import random
        drop = random.random() < self.packet_loss_rate
        self._total_count += 1
        if drop:
            self._dropped_count += 1
            print(f"[emulator] 📦 Unreliable FRAME DROPPED (p={self.packet_loss_rate:.2%}, "
                  f"dropped={self._dropped_count}/{self._total_count})")
        return drop
    
    def reset_stats(self):
        """Reset emulation statistics."""
        self._dropped_count = 0
        self._total_count = 0

