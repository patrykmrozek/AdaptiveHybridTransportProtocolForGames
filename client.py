import asyncio
import json
import random
import time
from typing import Optional, Dict
from metrics import RollingStats, Jitter
from server import METRIC_SUMMARY_EVERY_S

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    DatagramFrameReceived,
    StreamDataReceived,
    HandshakeCompleted,
)

ALPN = "game/1"
RELIABLE_CHANNEL = 0  # Used for Stream Data (Critical State)
UNRELIABLE_CHANNEL = 1  # Used for Datagrams (Movement)

def make_dgram(msg_type: int, channel: int, seq: int, payload: bytes) -> bytes:
    # Header: MsgType(1B), Channel(1B), Seq(2B) + Payload
    return bytes([msg_type & 0xFF, channel & 0xFF]) + seq.to_bytes(2, "big") + payload

def make_reliable_data(seq: int, payload: dict) -> bytes:
    # Application-layer reliable packet structure for state updates
    return json.dumps({
        "type": "state_update",
        "channel": RELIABLE_CHANNEL,
        "seq": seq,
        "ts": time.time(),  # Sender timestamp for RTT calculation
        "data": payload,
    }).encode()

class GameClientProtocol:
    """
    Thin wrapper (gameNetAPI) to send data via reliable (Stream) and unreliable (Datagram) channels.
    """

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.quic: QuicConnection = endpoint._quic
        self.seq_by_channel = {RELIABLE_CHANNEL: -1, UNRELIABLE_CHANNEL: -1}
        self.ctrl_stream_id: Optional[int] = None

        self._start_time = time.time()
        self._metrics_task = Optional[asyncio.Task()] = None
        self._inflight: Dict[int, float] = {} #seq: send_ts to calc RTT

        self.metrics = {
            "reliable": {
                "tx": 0, "ack": 0, "bytes_tx": 0,
                "rtt": RollingStats(), "jitter": Jitter()
            },
            "unreliable": {
                "tx": 0, "bytes_tx": 0,
            }
        }

    def _print_metrics_summary(self):
        now = time.time()
        dur = max(1e-6, now - self._start_time)

        r = self.metrics["reliable"]
        r_p = r["rtt"].percentiles()
        r_tput = r["bytes_tx"] / 1024.0 / dur
        pdr = 100.0 * r["ack"] / max(1, r["tx"])
        print("\n[client] 📊 --- METRIC SUMMARY ---")
        print(f"[metrics][RELIABLE] TX={r['tx']} ACK={r['ack']} PDR={pdr:.1f}% BytesTX={r['bytes_tx']}")
        if len(r["rtt"].samples) > 0:
            print(f"RTT(ms): avg={r['rtt'].avg():.2f} "
                  f"p50={r_p.get(50, float('nan')):.2f} "
                  f"p95={r_p.get(95, float('nan')):.2f} "
                  f"jitter(RFC3550)={r['jitter'].value():.2f}")
        else:
            print("RTT(ms): No samples yet")
        print(f"Throughput ≈ {r_tput:.2f} kB/s")

        u = self.metrics["unreliable"]
        u_tput = u["bytes_tx"] / 1024.0 / dur
        print(f"[metrics][UNRELIABLE] TX={u['tx']} BytesTX={u['bytes_tx']}")
        print(f"Throughput ≈ {u_tput:.2f} kB/s")
        print("[client] --------------------------\n")


    def next_seq(self, channel: int) -> int:
        s = self.seq_by_channel.get(channel, -1) + 1
        self.seq_by_channel[channel] = s
        return s
    
    # --- Client Methods (API) ---

    def send_reliable_state(self):
        """Send a reliable, sequenced game state update."""
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            
        seq = self.next_seq(RELIABLE_CHANNEL)
        critical_state = make_reliable_data(
            seq=seq,
            payload={"player_id": 1, "score": random.randint(0, 100)}
        )
        self.quic.send_stream_data(self.ctrl_stream_id, critical_state, end_stream=False)
        self.endpoint.transmit()
        print(f"[client] [Reliable] SENT: Seq={seq}, Size={len(critical_state)}, TS={time.time():.4f}")


    def send_unreliable_movement(self):
        """Send an unreliable, sequenced movement update."""
        x = random.uniform(-10, 10)
        y = random.uniform(-10, 10)
        # Include timestamp in payload for One-Way Latency (OWL) calculation
        payload = f"pos:{x:.2f},{y:.2f},ts:{time.time():.4f}".encode() 
        seq = self.next_seq(UNRELIABLE_CHANNEL)
        datagram = make_dgram(msg_type=1, channel=UNRELIABLE_CHANNEL, seq=seq, payload=payload)
        self.quic.send_datagram_frame(datagram)
        self.endpoint.transmit()
        print(f"[client] [Unreliable] SENT: Seq={seq}, Size={len(datagram)}")

    

    async def run(self):
        # Initial reliable hello for connection setup
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            hello = json.dumps({"type": "client_hello"}).encode()
            self.quic.send_stream_data(self.ctrl_stream_id, hello, end_stream=False)
            self.endpoint.transmit()

        
        recv_task = asyncio.create_task(self._recv_loop())
        # Part f Randomized sending loop
        mixed_task = asyncio.create_task(self._mixed_loop(reliable_hz=5, unreliable_hz=30)) 

        await asyncio.sleep(10)  
        recv_task.cancel()
        mixed_task.cancel()
        
        self.quic.close()
        self.endpoint.transmit()

    async def _recv_loop(self):
        """Keeps the connection alive and flushes QUIC events."""
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    async def _mixed_loop(self, reliable_hz: int = 5, unreliable_hz: int = 30):
        """
        Sends data randomly across both channels.(not a fixed rate)
        """
        reliable_period = 1.0 / reliable_hz
        unreliable_period = 1.0 / unreliable_hz
        
        last_reliable_send = 0.0
        last_unreliable_send = 0.0

        try:
            while True:
                now = time.time()
                
                # Check for reliable send opportunity
                if now - last_reliable_send >= reliable_period:
                    # Randomly decide to send reliable (e.g., 50% chance when available)
                    if random.random() < 0.5:
                        self.send_reliable_state()
                        last_reliable_send = now
                
                # Check for unreliable send opportunity (higher rate)
                if now - last_unreliable_send >= unreliable_period:
                    self.send_unreliable_movement()
                    last_unreliable_send = now
                    
                await asyncio.sleep(min(reliable_period, unreliable_period) / 2)
                
        except asyncio.CancelledError:
            pass

class ClientEvents(QuicConnectionProtocol):
    """
    Handles events from the server and logs RTT part g
    """
    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            # ready to send;
            pass
        elif isinstance(event, StreamDataReceived):
            # Reliable data from server (echo ACK from server)
            rx_ts = time.time()
            try:
                data_str = event.data.decode()
                if not data_str.startswith("ack:"):
                    return
                
                # Parse the echoed reliable packet to get the original timestamp
                original_packet = json.loads(data_str[4:])
                
                # Calculate RTT based on sender's original timestamp
                sent_ts = original_packet.get("ts", rx_ts)
                rtt = (rx_ts - sent_ts) * 1000  # RTT in milliseconds
                
                # (Requirement g) RTT Logging
                print(f"[client] [Reliable] ACK RX: AppSeq={original_packet.get('seq')}, RTT={rtt:.2f}ms")
            except Exception:
                # Handle initial client_hello ACK or malformed data
                if "client_hello" not in data_str:
                    print(f"[client] [Reliable] Data RX: {event.data!r}")

        elif isinstance(event, DatagramFrameReceived):
            # Unreliable from server
            pass

async def main(host: str = "127.0.0.1", port: int = 4433):
    cfg = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
    )
    # In dev, skip certificate validation (don't do this in prod)
    cfg.verify_mode = False
    cfg.max_datagram_frame_size = 1200

    async with connect(host, port, configuration=cfg, create_protocol=ClientEvents) as endpoint:
        client = GameClientProtocol(endpoint)
        await client.run()

if __name__ == "__main__":
    asyncio.run(main())
