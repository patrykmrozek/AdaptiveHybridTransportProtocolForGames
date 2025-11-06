import asyncio
import json
import time
from typing import Dict, Tuple, Optional
from metrics import RollingStats, Jitter

METRIC_SUMMARY_EVERY_S = 5.0 #how many seconds between esach metric summary

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except Exception:
    pass

from aioquic.asyncio import serve, QuicConnectionProtocol
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import (
    DatagramFrameReceived,
    StreamDataReceived,
    HandshakeCompleted,
)
from network_emulator import NetworkEmulator

def get_timestamp():
    """Return formatted timestamp for logging: HH:MM:SS.mmm"""
    now = time.time()
    ms = int((now * 1000) % 1000)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{ms:03d}"

ALPN = "game/1"
RELIABLE_TIMEOUT_MS = 200  # T milliseconds threshold

def make_server_cfg() -> QuicConfiguration:
    cfg = QuicConfiguration(is_client=False, alpn_protocols=[ALPN])
    cfg.max_datagram_frame_size = 1200
    # ⚠️ Make sure these files exist (see: openssl command I gave earlier)
    cfg.load_cert_chain("server.crt", "server.key")
    return cfg

class GameServer(QuicConnectionProtocol):
    def __init__(self, *args, emulator: Optional[NetworkEmulator] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # Application-layer buffer for reliable packets {app_seq: (timestamp_rx, data_bytes)}
        self.reliable_buffer: Dict[int, Tuple[float, bytes]] = {}
        self.next_expected_seq = 0
        # Track when we started waiting for each missing sequence {seq: wait_start_ts}
        self.missing_seq_wait_start: Dict[int, float] = {}
        self.flush_task: Optional[asyncio.Task] = None
        self._last_counters = {"rel_rx": 0, "unrel_rx": 0, "bytes": 0}

        # Network emulator (defaults to disabled if not provided)
        self.emulator = emulator if emulator is not None else NetworkEmulator(enabled=False)

        self._start_time = time.time()
        self.metrics_task: Optional[asyncio.Task] = None

        #set up metric storage
        self.metrics = {
            "reliable": {
                "owl": RollingStats(),
                "jitter": Jitter(),
                "rx": 0, "stale": 0, "ooo": 0, "dup": 0, "bytes": 0
            },
            "unreliable": {
                "owl": RollingStats(),
                "jitter": Jitter(),
                "rx": 0, "bytes": 0
            }
        }

    def _safe_transmit(self):
        """
        Helper to safely call emulator.transmit() in both sync and async contexts.
        Since quic_event_received is synchronous, we schedule the async transmit as a task.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.emulator.transmit(self.transmit))
        except RuntimeError:
            # No running loop, just call transmit directly (fallback)
            self.transmit()

    def _had_activity_since_last(self):
        r = self.metrics["reliable"]
        u = self.metrics["unreliable"]
        cur = {
            "rel_rx": r.get("rx", 0),
            "unrel_rx": u.get("rx", 0),
            "bytes": r.get("bytes", 0) + u.get("bytes", 0),
        }
        changed = any(cur[k] != self._last_counters[k] for k in cur)
        self._last_counters = cur
        return changed

    def _print_metrics_summary(self):
        now = time.time()
        print("\n[server] 📊 ---- METRIC SUMMARY ----")
        for pkt_type in ("reliable", "unreliable"):
            m = self.metrics[pkt_type]
            dur = max(1e-6, now - self._start_time)
            tput = m.get("bytes", 0) / 1024.0 / dur #kB/s

            header = f"[metrics][{pkt_type.upper()}] RX={m.get('rx', 0)} Bytes={m.get('bytes', 0)}"
            if pkt_type == "reliable":
                header += (
                    f" Stale={m.get('stale', 0)}"
                    f" OOO={m.get('ooo', 0)}"
                    f" Dup={m.get('dup', 0)}"
                )
            print(header)

            owl_stats = m.get("owl")
            if owl_stats and len(owl_stats.samples) > 0:
                p = owl_stats.percentiles()
                avg_owl = owl_stats.avg()
                jitter_val = m["jitter"].value()
                print(
                    f"    OWL(ms): avg={avg_owl:.2f}, "
                    f"p50={p.get(50, float('nan')):.2f}, "
                    f"p95={p.get(95, float('nan')):.2f}, "
                    f"jitter(RFC3550)={jitter_val:.2f}"
                )
            else:
                print("    OWL(ms): no samples")

            print(f"    Throughput ≈ {tput:.2f} kB/s")
        print("[server] --------------------------\n")

    def connection_made(self, transport):
        super().connection_made(transport)
        # Part e background task to flush the reliable packet buffer RDT
        self.flush_task = asyncio.create_task(self._flush_reliable_data())
        self.metrics_task = asyncio.create_task(self._periodic_metrics_print())

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if self.flush_task:
            self.flush_task.cancel()
        if self.metrics_task:
            self.metrics_task.cancel()

    async def _periodic_metrics_print(self):
        try:
            while True:
                await asyncio.sleep(METRIC_SUMMARY_EVERY_S)
                if self._had_activity_since_last():
                    self._print_metrics_summary()
                    #send heartbeat only when there was recent activity
                    try:
                        stats = {
                            "type": "server_metrics",
                            "unrel_rx": self.metrics["unreliable"]["rx"],
                            "rel_rx": self.metrics["reliable"]["rx"],
                        }
                        sid = self._quic.get_next_available_stream_id(is_unidirectional=False)
                        self._quic.send_stream_data(sid, json.dumps(stats).encode(), end_stream=False)
                        self._quic.send_stream_data(sid, json.dumps(stats).encode(), end_stream=False)
                        await self.emulator.transmit(self.transmit)
                    except Exception as e:
                        print(f"[server] Warning: failed to send metrics heartbeat: {e}")
        except asyncio.CancelledError:
            pass

    async def _flush_reliable_data(self):
        
        try:
            while True:
                await asyncio.sleep(0.01)  # Check frequently
                now = time.time()

                if self.next_expected_seq not in self.reliable_buffer:
                    next_available_seq = self.next_expected_seq + 1
                    
                    if next_available_seq in self.reliable_buffer:
                         (rx_ts, _) = self.reliable_buffer[next_available_seq]
                         time_waiting = (now - rx_ts) * 1000

                         if time_waiting > RELIABLE_TIMEOUT_MS:
                            self.metrics["reliable"]["stale"] += 1
                            print(f"[server] ⚠️ RELIABLE SKIP: Seq={self.next_expected_seq} skipped. Next Seq={next_available_seq} waited for {time_waiting:.2f}ms > {RELIABLE_TIMEOUT_MS}ms.")

                            try:
                                skip_msg = json.dumps({
                                    "type": "skip",
                                    "seq": self.next_expected_seq,
                                    "next_seq": next_available_seq
                                }).encode()
                                sid = self._quic.get_next_available_stream_id(is_unidirectional=False)
                                self._quic.send_stream_data(sid, skip_msg, end_stream=False)
                                await self.emulator.transmit(self.transmit)
                            except Exception as e:
                                print(f"[server] Warn: failed to send skip notice: {e}")

                            self.next_expected_seq += 1
                            continue

                
                while self.next_expected_seq in self.reliable_buffer:
                    rx_ts, data_bytes = self.reliable_buffer.pop(self.next_expected_seq)
                    try:
                        
                        packet = json.loads(data_bytes.decode())
                        
                        # one-way latency OWL
                        sent_ts = packet.get("ts", now)
                        owl_ms = (rx_ts - sent_ts) * 1000
                        
                        # Logging
                        print(f"[server] ✅ RELIABLE RX: Seq={self.next_expected_seq}, OWL={owl_ms:.2f}ms, Data={packet.get('data')}")

                        m_r = self.metrics["reliable"]
                        m_r["rx"] += 1
                        m_r["bytes"] += len(data_bytes)
                        m_r["owl"].add(owl_ms)
                        m_r["jitter"].add(owl_ms)


                        """
                        ### -- TESTING RETRANSMISSION SCHEDULER --
                        ### DROPPING + DELAYING ACKS
                        DROP_EVERY = 7
                        DELAY_MS = 400
                        DELAY_MOD = 3

                        seqn = self.next_expected_seq
                        if DROP_EVERY and (seqn % DROP_EVERY == 0):
                            print(f"[server][TEST] dropping ACK for Seq={seqn}")
                        else:
                            if DELAY_MS and DELAY_MOD and (seqn % DELAY_MOD == 0):
                                print(f"[server][TEST] delaying ACK {DELAY_MS}ms for Seq={seqn}")
                                await asyncio.sleep(DELAY_MS / 1000.0)

                            self._quic.send_stream_data(
                                self._quic.get_next_available_stream_id(is_unidirectional=False),
                                b"ack:" + data_bytes,
                                end_stream=False
                            )
                            await self.emulator.transmit(self.transmit)
                        #END OF RETRANSMISSION TEST
                        """

                        ##COMMENT OUT THE REST OF THE BLOCK WHEN TESTING RETRANSMISSION

                        # Echo ACK for RTT calculation on client side
                        self._quic.send_stream_data(self._quic.get_next_available_stream_id(is_unidirectional=False), b"ack:" + data_bytes, end_stream=False)
                        await self.emulator.transmit(self.transmit)


                    except Exception as e:
                        print(f"[server] Error processing reliable packet: {e}")
                    
                    self.next_expected_seq += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[server] Flush task error: {e}")


    def quic_event_received(self, event) -> None:
        if isinstance(event, HandshakeCompleted):
            peer = self._quic._peer_address if hasattr(self._quic, "_peer_address") else "<peer>"
            print(f"[server] Handshake completed with {peer}")
            # Send initial reliable hello on a fresh bidirectional stream
            stream_id = self._quic.get_next_available_stream_id(is_unidirectional=False)
            self._quic.send_stream_data(stream_id, json.dumps({"type": "server_hello"}).encode(), end_stream=False)
            self._safe_transmit()

        elif isinstance(event, StreamDataReceived):
            # buffer here
            rx_ts = time.time()
            r_str = "reliable"
            
            try:
                data_str = event.data.decode()
                if data_str.startswith('{"type": "client_hello"}'):
                    print(f"[server] [Reliable] Initial client_hello received.")
                    return

                packet = json.loads(data_str)
                app_seq = packet.get("seq")
                pkt_ts = packet.get("ts")
                
                if app_seq is None or pkt_ts is None:
                    print(f"[server] [Reliable]  RC: missing seq/ts: {packet}.")
                    return

                if app_seq < self.next_expected_seq:
                    self.metrics[r_str]["dup"] = self.metrics[r_str].get("dup", 0) + 1
                    print(f"[server] 🔄 [Reliable] RX: Seq={app_seq}, current expected={self.next_expected_seq}")
                    return

                # part e buffer
                self.reliable_buffer[app_seq] = (rx_ts, event.data)

                if app_seq > self.next_expected_seq:
                    # part g: Packet arrived out-of-order
                    self.metrics[r_str]["ooo"] += 1
                    print(f"[server] ⏳ [Reliable] RX: Seq={app_seq} (O.O.O), Expected={self.next_expected_seq}. Buffering...")

                else:
                    print(f"[server] [Reliable] RX: Unsequenced data: {event.data!r}")
            
            except json.JSONDecodeError:
                print(f"[server] [Reliable] RX: Non-JSON data on stream {event.stream_id}")


        elif isinstance(event, DatagramFrameReceived):
            # Unreliable from client (Movement data) Part g
            rx_ts = time.time()
            
            # header parse
            seq = int.from_bytes(event.data[2:4], "big")
            payload = event.data[4:]
            
            try:
                
                payload_str = payload.decode()
                sent_ts_str = payload_str.split("ts:")[1]
                sent_ts = float(sent_ts_str)
                
                owl_ms = (rx_ts - sent_ts) * 1000

                m_u = self.metrics["unreliable"]
                m_u["rx"] += 1
                m_u["bytes"] += len(event.data)
                m_u["owl"].add(owl_ms)
                m_u["jitter"].add(owl_ms)
                
                # Logging
                print(f"[server] ⏩ [Unreliable] RX: Seq={seq}, OWL={owl_ms:.2f}ms, Data={payload_str.split(',ts:')[0]}")
            except Exception:
                print(f"[server] ⏩ [Unreliable] RX: Seq={seq}, Data={payload!r}")


async def main(host="0.0.0.0", port=4433,
               emulation_enabled: bool = False, delay_ms: float = 0,
               jitter_ms: float = 0, packet_loss_rate: float = 0.0,
               drop_sequences: set = None):
    cfg = make_server_cfg()

    # Create network emulator
    emulator = NetworkEmulator(
        enabled=emulation_enabled,
        delay_ms=delay_ms,
        jitter_ms=jitter_ms,
        packet_loss_rate=packet_loss_rate,
        drop_sequences=drop_sequences or set()
    )

    # Create protocol factory that passes emulator to each connection
    def create_protocol(*args, **kwargs):
        return GameServer(*args, emulator=emulator, **kwargs)

    await serve(host, port, configuration=cfg, create_protocol=create_protocol)
    print(f"[server] QUIC listening on {host}:{port} (ALPN={ALPN})")
    print(f"[server] Reliable data skip threshold (T): {RELIABLE_TIMEOUT_MS} ms.")
    if emulation_enabled:
        print(f"[server] Network emulation: delay={delay_ms}ms, jitter={jitter_ms}ms, loss={packet_loss_rate:.2%}")
    # ⛔️ Keep the server running forever
    await asyncio.Event().wait()

if __name__ == "__main__":
    # Example configurations (uncomment one):

    # 1. No emulation (normal operation)
    asyncio.run(main())

    # 2. Server-side emulation (affects ACKs back to client)
    # asyncio.run(main(
    #     emulation_enabled=True,
    #     delay_ms=5,
    #     jitter_ms=10,
    #     packet_loss_rate=0.0
    # ))
