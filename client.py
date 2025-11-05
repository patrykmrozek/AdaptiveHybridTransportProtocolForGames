import asyncio
import json
import random
import time
from sys import set_int_max_str_digits
from typing import Optional, Dict
from metrics import RollingStats, Jitter
from dataclasses import dataclass, field

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

@dataclass
class Inflight:
    payload_bytes: bytes #exact bytes to resend
    ctrl_stream_id: int #stream used for send
    seq: int
    ts_first_ms: int #time of first send
    ts_last_ms: int #time of last (re)send
    retries: int = 0

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
        self._metrics_task: Optional[asyncio.Task] = None
        self._retransmit_task: Optional[asyncio.Task] = None

        self._inflight: Dict[int, Inflight] = {} #seq: Inflight
        self.srtt_ms: float | None = None  # smooth rtt - estimate
        self.rttvar_ms = 0.0  # variation in RTT
        self.RTO_min_ms = 100  # lowerbound RTO
        self.RTO_max_ms = 3000  # upperbound RTO
        self.MAX_RETRIES = 5
        self.next_seq = 0  # increments 1 each send

        #storage for server rx counters
        self._server_unrel_rx = 0

        self.metrics = {
            "reliable": {
                "tx": 0, "ack": 0, "bytes_tx": 0,
                "rtt": RollingStats(), "jitter": Jitter()
            },
            "unreliable": {
                "tx": 0, "bytes_tx": 0,
            }
        }

    #helpers for rtt and rto calculations
    # returns RTO between min and max defined above, scaled by latency and variability
    def RTO_ms(self):
        if self.srtts_ms is None:
            return 1000
        rto = self.srtt_ms + 4 * self.rttvar_ms
        return max(self.RTO_min_ms, min(self.RTO_max_ms, int(rto)))

    #refines est rtt to adapt to network delay
    def _update_rtt(self, sample_ms):
        if self.srtt_ms is None:
            self.srtt_ms = sample_ms
            self.rttvar_ms = sample_ms / 2.0
        else:
            #weights
            w1 = 1/8
            w2 = 1/4
            #update rtt variability
            self.rttvar_ms = (1-w2) * self.rttvar_ms + w2 * abs(self.srtt_ms - sample_ms)
           #update rtt est
            self.srtt_ms = (1-w1) * self.srtt_ms + w1 * sample_ms

    def _print_metrics_summary(self):
        now = time.time()
        dur = max(1e-6, now - self._start_time)

        r = self.metrics["reliable"]
        r_p = r["rtt"].percentiles()
        r_tput = r["bytes_tx"] / 1024.0 / dur
        pdr = 100.0 * r["ack"] / max(1, r["tx"])
        print("\n[client] 📊 ---- METRIC SUMMARY ----")
        print(f"[metrics][RELIABLE] TX={r['tx']} ACK={r['ack']} PDR={pdr:.1f}% BytesTX={r['bytes_tx']}")
        if len(r["rtt"].samples) > 0:
            print(f"    RTT(ms): avg={r['rtt'].avg():.2f} "
                  f"p50={r_p.get(50, float('nan')):.2f} "
                  f"p95={r_p.get(95, float('nan')):.2f} "
                  f"jitter(RFC3550)={r['jitter'].value():.2f}")
        else:
            print("    RTT(ms): No samples yet")
        print(f"    Throughput ≈ {r_tput:.2f} kB/s")

        u = self.metrics["unreliable"]
        u_tput = u["bytes_tx"] / 1024.0 / dur
        u_pdr = 100.0 * self._server_unrel_rx / max(1, u["tx"])

        print(f"[metrics][UNRELIABLE] TX={u['tx']} BytesTX={u['bytes_tx']} PDR={u_pdr:.1f}%")
        print(f"    Throughput ≈ {u_tput:.2f} kB/s")
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

        self.metrics["reliable"]["tx"] += 1
        self.metrics["reliable"]["bytes_tx"] += len(critical_state)

        now_ms = int(time.time() * 1000)
        self._inflight[seq] = Inflight(
            payload_bytes=critical_state,
            ctrl_stream_id=self.ctrl_stream_id,
            seq=seq,
            ts_first_ms=now_ms,
            ts_last_ms=now_ms,
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

        self.metrics["unreliable"]["tx"] += 1
        self.metrics["unreliable"]["bytes_tx"] += len(datagram)

        self.quic.send_datagram_frame(datagram)
        self.endpoint.transmit()
        print(f"[client] [Unreliable] SENT: Seq={seq}, Size={len(datagram)}")

    #scans un-acked packets and resends any whose timer expired
    async def retransmit_scheduler(self):
        try:
            while True:
                if not self._inflight:
                    await asyncio.sleep(0.01)
                    continue

                now_ms = int(time.time() * 1000)
                rto = self.RTO_ms() #read a single RTO
                for seq, item in list(self._inflight.items()):
                    if now_ms - item.ts_last_ms >= rto: #if expired
                        if item.retries >= self.MAX_RETRIES:
                            #give up on packet
                            del self._inflight[seq]
                            print(f"[client] Drop seq={seq} after {item.retries} retries")
                            continue
                        #resend same exact data on same stream - retransmit
                        self.quic.send_stream_data(item.ctrl_stream_id, item.payload_bytes, end_stream=False)
                        self.endpoint.transmit()
                        item.retries += 1
                        item.ts_last_ms = now_ms
                        print(f"[client] Retransmit seq={seq} (try {item.retries})")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass


    
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
        self._retransmit_task = asyncio.create_task(self.retransmit_scheduler())

        await asyncio.sleep(10)  
        recv_task.cancel()
        mixed_task.cancel()
        if self._metrics_task:
            self._metrics_task.cancel()
        if self._retransmit_task:
            self._retransmit_task.cancel()
        
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
            data_str = None
            try:
                data_str = event.data.decode()
                if data_str.startswith("ack:"):
                    # Parse the echoed reliable packet to get the original timestamp
                    original_packet = json.loads(data_str[4:])
                    seq = original_packet.get("seq")

                    # Calculate RTT based on sender's original timestamp
                    sent_ts = float(original_packet.get("ts", rx_ts))

                    client = getattr(self, "metrics_client", None)
                    if client is not None and seq is not None:
                        item = client._inflight.pop(seq, None)
                        if item is not None:
                            #compare rx_ts with last_ts
                            rtt_ms = (rx_ts * 1000) - item.ts_last_ms
                        else:
                            #fallback
                            rtt_ms = (rx_ts - sent_ts) * 1000.0

                        m = client.metrics["reliable"]
                        m["ack"] += 1
                        m["rtt"].add(rtt_ms)
                        m["jitter"].add(rtt_ms)
                        client._update_rtt(rtt_ms)
                    else:
                        rtt_ms = (rx_ts - sent_ts) * 1000
                    print(f"[client] [Reliable] ACK RX: AppSeq={seq}, RTT={rtt_ms:.2f}ms")
                    return

                pkt = json.loads(data_str)
                if pkt.get("type") == "server_metrics":
                    client = getattr(self, "metrics_client", None)
                    if client:
                        client._server_unrel_rx = int(pkt.get("unrel_rx", 0))
                        client._server_rel_rx = int(pkt.get("rel_rx", 0))
                        client._print_metrics_summary()
                    return

            except Exception:
                # Handle initial client_hello ACK or malformed data
                if data_str and "client_hello" not in data_str:
                    print(f"[client] [Reliable] Data RX: {event.data!r}")


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
        endpoint.metrics_client = client
        await client.run()

if __name__ == "__main__":
    asyncio.run(main())
