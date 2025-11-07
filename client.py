import asyncio
import json
import random
import time
from typing import Optional, Dict
from metrics import RollingStats, Jitter
from dataclasses import dataclass

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from aioquic.asyncio import connect, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import (
    StreamDataReceived,
    HandshakeCompleted,
)

def get_timestamp():
    """Return formatted timestamp for logging: HH:MM:SS.mmm"""
    now = time.time()
    ms = int((now * 1000) % 1000)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{ms:03d}"
from network_emulator import NetworkEmulator

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

# Part A : Packet Header
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

    def __init__(self, endpoint, emulator: Optional[NetworkEmulator] = None):
        self.endpoint = endpoint
        self.quic: QuicConnection = endpoint._quic
        self.seq_by_channel = {RELIABLE_CHANNEL: -1, UNRELIABLE_CHANNEL: -1}
        self.ctrl_stream_id: Optional[int] = None

        # Network emulator (defaults to disabled if not provided)
        self.emulator = emulator if emulator is not None else NetworkEmulator(enabled=False)

        self._start_time = time.time()
        self._metrics_task: Optional[asyncio.Task] = None
        self._retransmit_task: Optional[asyncio.Task] = None

        self._inflight: Dict[int, Inflight] = {} #seq: Inflight
        self.srtt_ms: float | None = None  # smooth rtt - estimate
        self.rttvar_ms = 0.0  # variation in RTT
        self.RTO_min_ms = 100  # lowerbound RTO
        self.RTO_max_ms = 3000  # upperbound RTO
        self.next_seq = 0  # increments 1 each send
        self.MAX_RETRIES = 5

        #storage for server rx counters
        self._server_unrel_rx_total = 0
        self._client_unrel_tx_total = 0
        self._client_unrel_tx_prev = 0
        self._server_unrel_rx_prev = 0
        self._last_pdr_interval = float("nan")

        # Retransmission settings
        self._retransmit_timeout_ms = 100  # Initial timeout (will be updated based on RTT)
        self._max_rtt_estimate_ms = 200  # Maximum RTT estimate for timeout calculation

        self.metrics = {
            "reliable": {
                "tx": 0, "ack": 0, "bytes_tx": 0, "retransmit": 0,
                "rtt": RollingStats(), "jitter": Jitter()
            },
            "unreliable": {
                "tx": 0, "bytes_tx": 0,
            }
        }

    #helpers for rtt and rto calculations
    # returns RTO between min and max defined above, scaled by latency and variability
    def RTO_ms(self):
        if self.srtt_ms is None:
            return 300
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

    def _print_metrics_summary(self, pdr_total_pct: float, pdr_interval_pct: float):
        now = time.time()
        dur = max(1e-6, now - self._start_time)

        r = self.metrics["reliable"]
        r_p = r["rtt"].percentiles()
        r_tput = r["bytes_tx"] / 1024.0 / dur
        pr = 100.0 * r["ack"] / max(1, r["tx"])

        print("\n[client] 📊 ---- METRIC SUMMARY ----")
        print(
            f"[metrics][RELIABLE] TX={r['tx']} ACK={r['ack']} RETX={r.get('retransmit', 0)} PDR={pr:.1f}% BytesTX={r['bytes_tx']}")
        if r["rtt"].samples:
            print(
                f"    RTT(ms): avg={r['rtt'].avg():.2f} p50={r_p.get(50, float('nan')):.2f} p95={r_p.get(95, float('nan')):.2f} jitter(RFC3550)={r['jitter'].value():.2f}")
        else:
            print("    RTT(ms): No samples yet")
        print(f"    Throughput ≈ {r_tput} kB/s")

        u = self.metrics["unreliable"]
        u_tput = u["bytes_tx"] / 1024.0 / dur
        print(f"[metrics][UNRELIABLE] TX={u['tx']} BytesTX={u['bytes_tx']} "
              f"PDR(total)={pdr_total_pct:.1f}%"
              + (f" PDR(interval)={pdr_interval_pct:.1f}%" if pdr_interval_pct is not None and not (
                    pdr_interval_pct != pdr_interval_pct) else ""))
        print(f"    Throughput ≈ {u_tput:.2f} kB/s")
        print("[client] --------------------------\n")



    def alloc_seq(self, channel: int) -> int:
        s = self.seq_by_channel.get(channel, -1) + 1
        self.seq_by_channel[channel] = s
        return s
    
    # --- Client Methods (API) ---
    # gameNetAPI : Unified Send Method
    async def send(self, data=None, reliable=True, msg_type=1):
        if reliable:
            return await self.send_reliable_state(data=data)
        else:
            return await self.send_unreliable_movement(data=data, msg_type=msg_type)

    async def send_reliable_state(self, data=None):
        """Send a reliable, sequenced game state update.
        
        Args:
            data: Optional dict payload. If None, uses default game state.
        """
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            
        seq = self.alloc_seq(RELIABLE_CHANNEL)

        # Use provided data or generate default
        if data is None:
            payload = {"player_id": 1, "score": random.randint(0, 100)}
        else:
            payload = data
        

        critical_state = make_reliable_data(
            seq=seq,
            payload=payload
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
        await self.emulator.transmit(self.endpoint.transmit, packet_info={"seq": seq, "type": "reliable"})
        print(f"[client] [Reliable] SENT: Seq={seq}, Size={len(critical_state)}, TS={time.time():.4f}")
        return seq


    async def send_unreliable_movement(self, data=None, msg_type=1):
        """Send an unreliable, sequenced movement update.
        
        Args:
            data: Optional payload data (currently not used, generates random position)
            msg_type: Message type identifier for the datagram header
        """
        x = random.uniform(-10, 10)
        y = random.uniform(-10, 10)
        # Include timestamp in payload for One-Way Latency (OWL) calculation
        payload = f"pos:{x:.2f},{y:.2f},ts:{time.time():.4f}".encode()
        seq = self.alloc_seq(UNRELIABLE_CHANNEL)
        datagram = make_dgram(msg_type=msg_type, channel=UNRELIABLE_CHANNEL, seq=seq, payload=payload)

        self.metrics["unreliable"]["tx"] += 1
        self._client_unrel_tx_total = self.metrics["unreliable"]["tx"]
        self.metrics["unreliable"]["bytes_tx"] += len(datagram)

        self.quic.send_datagram_frame(datagram)
        await self.emulator.transmit(self.endpoint.transmit)
        print(f"[client] [Unreliable] SENT: Seq={seq}, Size={len(datagram)}")
        return seq

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
                        self.metrics["reliable"]["retransmit"] += 1
                        self.metrics["reliable"]["bytes_tx"] += len(item.payload_bytes)
                        self.quic.send_stream_data(item.ctrl_stream_id, item.payload_bytes, end_stream=False)
                        await self.emulator.transmit(self.endpoint.transmit, packet_info={"seq": item.seq, "type": "reliable"})
                        item.retries += 1
                        item.ts_last_ms = now_ms
                        print(f"[client] RETRANSMIT seq={seq} (try {item.retries})")
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            pass

    async def run(self):
        # Initial reliable hello for connection setup
        if self.ctrl_stream_id is None:
            self.ctrl_stream_id = self.quic.get_next_available_stream_id(is_unidirectional=False)
            hello = json.dumps({"type": "client_hello"}).encode()
            self.quic.send_stream_data(self.ctrl_stream_id, hello, end_stream=False)
            self.quic.send_stream_data(self.ctrl_stream_id, b'{"type":"metrics_reset"}', end_stream=False)
            await self.emulator.transmit(self.endpoint.transmit)


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
        await self.emulator.transmit(self.endpoint.transmit)

    async def _recv_loop(self):
        """Keeps the connection alive and flushes QUIC events."""
        try:
            while True:
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    """
    async def _retransmit_loop(self):
        #Periodically checks for unACKed packets and retransmits them.
        try:
            while True:
                await asyncio.sleep(0.05)  # Check every 50ms
                now = time.time()

                # Calculate timeout based on current RTT estimate
                rtt_samples = self.metrics["reliable"]["rtt"].samples
                if len(rtt_samples) > 0:
                    avg_rtt = sum(rtt_samples) / len(rtt_samples)
                    timeout_ms = max(100, avg_rtt * 2)  # 2x RTT, minimum 100ms
                else:
                    timeout_ms = self._retransmit_timeout_ms

                # Check each unACKed packet
                to_retransmit = []
                for seq, pkt_info in list(self._inflight.items()):
                    elapsed_ms = (now - pkt_info["last_retry_ts"]) * 1000

                    if elapsed_ms >= timeout_ms:
                        to_retransmit.append((seq, pkt_info))

                # Retransmit packets that timed out
                for seq, pkt_info in to_retransmit:
                    pkt_info["retry_count"] += 1
                    pkt_info["last_retry_ts"] = now

                    self.metrics["reliable"]["retransmit"] += 1
                    self.metrics["reliable"]["bytes_tx"] += len(pkt_info["packet_data"])

                    self.quic.send_stream_data(self.ctrl_stream_id, pkt_info["packet_data"], end_stream=False)
                    await self.emulator.transmit(
                        self.endpoint.transmit,
                        packet_info={"seq": seq, "type": "reliable"}
                    )
                    print(f"[{get_timestamp()}] [client] [Reliable] RETRANSMIT: Seq={seq}, Retry={pkt_info['retry_count']}, Timeout={timeout_ms:.1f}ms")

        except asyncio.CancelledError:
            pass
    """

    async def _mixed_loop(self, reliable_hz: int = 5, unreliable_hz: int = 30):
        """
        Sends data randomly across both channels using the unified gameNetAPI.
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
                        # Use unified API with reliable=True
                        await self.send(reliable=True)
                        last_reliable_send = now
                
                # Check for unreliable send opportunity (higher rate)
                if now - last_unreliable_send >= unreliable_period:
                    # Use unified API with reliable=False
                    await self.send(reliable=False)
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
            # Reliable data from server
            rx_ts = time.time()
            data = event.data.decode(errors="ignore")

            for data_str in data.splitlines():
                if not data_str.strip():
                    continue
                try:
                    obj = None
                    try:
                        obj = json.loads(data_str)
                    except Exception:
                        obj = None

                    if obj and obj.get("type") == "metrics_reset_ack":
                        client = getattr(self, "metrics_client", None)
                        if client:
                            client._start_time = time.time()
                            m = client.metrics
                            m["reliable"]["tx"] = m["reliable"]["ack"] = m["reliable"]["bytes_tx"] = 0
                            m["unreliable"]["tx"] = m["unreliable"]["bytes_tx"] = 0
                            client._server_unrel_rx_prev = 0
                            client._client_unrel_tx_prev = 0
                            client._server_unrel_rx_total = 0
                            client._client_unrel_tx_total = 0
                            client._last_pdr_interval = float("nan")
                        continue

                    ack_seq = None
                    if obj and isinstance(obj, dict) and "ack" in obj:
                        ack_seq = obj["ack"]
                    if ack_seq is None and data_str.startswith("ack:"):
                        try:
                            original_packet = json.loads(data_str[4:])
                            ack_seq = original_packet.get("seq")
                        except Exception:
                            pass
                    if ack_seq is not None:
                        client = getattr(self, "metrics_client", None)
                        if not client:
                            continue
                        if not hasattr(client, "_acked_seqs"):
                            client._acked_seqs = set()
                        if ack_seq in client._acked_seqs:
                            continue
                        item = client._inflight.pop(ack_seq, None)
                        client._acked_seqs.add(ack_seq)
                        if item is None:
                            continue
                        sent_ts = json.loads(item.payload_bytes.decode()).get("ts", rx_ts)
                        rtt_ms = (rx_ts - sent_ts) * 1000.0
                        m = client.metrics["reliable"]
                        m["ack"] += 1
                        m["rtt"].add(rtt_ms)
                        m["jitter"].add(rtt_ms)
                        client._update_rtt(rtt_ms)
                        print(f"[client] [Reliable] ACK RX: AppSeq={ack_seq}, RTT={rtt_ms:.2f}ms")
                        continue

                    if obj and obj.get("type") == "skip":
                        client = getattr(self, "metrics_client", None)
                        if client:
                            skipped_seq = obj.get("seq")
                            next_seq = obj.get("next_seq")
                            client._inflight.pop(skipped_seq, None)
                            print(
                                f"[{get_timestamp()}] [client] ⚠️ EXPLICIT SKIP: Seq={skipped_seq} (next_seq={next_seq})")
                        continue

                    if obj and obj.get("type") == "server_metrics":
                        client = getattr(self, "metrics_client", None)
                        if client:
                            server_unrel_rx_now = int(obj.get("unrel_rx", 0))
                            client_unrel_tx_now = client.metrics["unreliable"]["tx"]
                            d_rx = server_unrel_rx_now - client._server_unrel_rx_prev
                            d_tx = client_unrel_tx_now - client._client_unrel_tx_prev
                            pdr_interval = 100.0 if d_tx <= 0 else max(0.0, min(100.0, 100.0 * d_rx / d_tx))
                            client._server_unrel_rx_prev = server_unrel_rx_now
                            client._client_unrel_tx_prev = client_unrel_tx_now
                            client._last_pdr_interval = pdr_interval
                            client._server_unrel_rx_total = server_unrel_rx_now
                            client._client_unrel_tx_total = client_unrel_tx_now
                            pdr_total = 100.0 * server_unrel_rx_now / max(1, client_unrel_tx_now)

                            d_rx = server_unrel_rx_now - client._server_unrel_rx_prev
                            d_tx = client_unrel_tx_now - client._client_unrel_tx_prev
                            pdr_interval = 100.0 if d_tx <= 0 else max(0.0, min(100.0, 100.0 * d_rx / d_tx))

                            client._server_unrel_rx_prev = server_unrel_rx_now
                            client._client_unrel_tx_prev = client_unrel_tx_now

                            client._print_metrics_summary(pdr_total, pdr_interval)

                        continue

                    if "client_hello" not in data_str:
                        print(f"[{get_timestamp()}] [client] [Reliable] Data RX (non-JSON): {data_str[:100]!r}")

                except Exception as e:
                    print(f"[client] handler error: {type(e).__name__}: {e}")


async def main(host: str = "127.0.0.1", port: int = 4433,
               emulation_enabled: bool = False, delay_ms: float = 0,
               jitter_ms: float = 0, packet_loss_rate: float = 0.0,
               drop_sequences: set = None):
    """
    Main client function.

    Args:
        host: Server hostname
        port: Server port
        emulation_enabled: Enable network emulation
        delay_ms: Base delay in milliseconds
        jitter_ms: Jitter variation in milliseconds
        packet_loss_rate: Packet loss rate (0.0 to 1.0)
        drop_sequences: Set of sequence numbers to selectively drop (for testing retransmission)
    """
    cfg = QuicConfiguration(
        is_client=True,
        alpn_protocols=[ALPN],
    )
    # In dev, skip certificate validation (don't do this in prod)
    cfg.verify_mode = False
    cfg.max_datagram_frame_size = 1200

    # Create network emulator
    emulator = NetworkEmulator(
        enabled=emulation_enabled,
        delay_ms=delay_ms,
        jitter_ms=jitter_ms,
        packet_loss_rate=packet_loss_rate,
        drop_sequences=drop_sequences or set()
    )

    async with connect(host, port, configuration=cfg, create_protocol=ClientEvents) as endpoint:
        client = GameClientProtocol(endpoint, emulator=emulator)
        endpoint.metrics_client = client
        await client.run()

        # Print emulator stats if enabled
        if emulation_enabled:
            stats = emulator.get_stats()
            print(f"\n[client] 📊 Network Emulator Stats:")
            print(f"  Total packets: {stats['total_packets']}")
            print(f"  Dropped packets: {stats['dropped_packets']}")
            print(f"  Drop rate: {stats['drop_rate']:.2%}")

if __name__ == "__main__":
    # Example configurations (uncomment one):

    # 1. No emulation (normal operation)
    # asyncio.run(main())

    # 2. Test retransmission with selective drops
    # asyncio.run(main(
    #     emulation_enabled=True,
    #     delay_ms=0,
    #     jitter_ms=0,
    #     packet_loss_rate=0.0,
    #     drop_sequences={3, 7, 12}  # Drop specific packets to test retransmission
    # ))

    """
    # 3. Test reordering with jitter (no drops)
    asyncio.run(main(
        emulation_enabled=True,
        delay_ms=0,
        jitter_ms=0,
        packet_loss_rate=0.0,      # No random loss - ACKs won't be dropped
        drop_sequences={3, 7, 12}   # Drop these specific packets to test retransmission
    ))
    """


    # 4. Test combined: retransmission + reordering
    # asyncio.run(main(
    #     emulation_enabled=True,
    #     delay_ms=10,
    #     jitter_ms=30,
    #     packet_loss_rate=0.0,
    #     drop_sequences={5, 15}
    # ))

    """
    #demo test configs
    ## 1) baseline - TEST_DISTURB_ACKS = False
    print(f"=========== DEMO TEST 1 - BASELINE ===========")
    asyncio.run(main(
        emulation_enabled=False,
    ))


        ## 2) retransmissions - TEST_DISTURB_ACKS = True
    asyncio.run(main(
    emulation_enabled=True,
        packet_loss_rate=0.0,
        jitter_ms=0,
        delay_ms=0,
        drop_sequences={3,7,12},
    ))

    ## 3) reordering - TEST_DISTURB_ACKS = False
    ## RELIABLE_TIMEOUT_MS = 1000
asyncio.run(main(
    emulation_enabled=True,
    packet_loss_rate=0.0,
    drop_sequences=set(),
    delay_ms=0,
    jitter_ms=80,
))

## 4 a)) low loss - TEST_DISTURB_ACKS = False
    asyncio.run(main(
        emulation_enabled=True,
        packet_loss_rate=0.01,
        delay_ms=5,
        jitter_ms=5,
        drop_sequences=set(),
    ))

"""
# 4 b)) high loss - TEST_DISTURB_ACKS = False
asyncio.run(main(
    emulation_enabled=True,
    packet_loss_rate=0.15,
    delay_ms=10,
    jitter_ms=20,
    drop_sequences=set(),
))