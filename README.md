Step 1 — Clone & enter the directory
git clone <your_repo_url>
cd Assignment\ 4

Step 2 — Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate  # On macOS/Linux
# .venv\Scripts\activate   # On Windows

Step 3 — Install dependencies
pip install aioquic uvloop


uvloop is optional but improves performance.

Step 4 — Generate self-signed TLS certificates

QUIC requires TLS 1.3 encryption.
Run the following command in the project directory:

openssl req -newkey rsa:2048 -nodes -keyout server.key \
    -x509 -days 365 -out server.crt \
    -subj "/CN=localhost"


This creates:

server.crt — the public certificate

server.key — the private key

Keep them in the same directory as server.py.

🚀 3. Running the Application
Terminal 1 — Start the QUIC Server
python3 server.py


Expected output:

[server] QUIC listening on 0.0.0.0:4433 (ALPN=game/1)


The server will remain running, waiting for QUIC clients to connect.

Terminal 2 — Start the Client
python3 client.py

## Network Emulation

The application includes a built-in Python network emulator (`network_emulator.py`) that simulates real-world network conditions without requiring external tools.

### Why Python Network Emulator?

- **Easiest to use**: No system-level configuration or admin privileges required
- **Cross-platform**: Works on macOS, Linux, and Windows
- **Reproducible**: Same configuration produces identical results
- **Integrated**: No need for external tools like tc-netem or clumsy
- **Easy to toggle**: Simple parameter to enable/disable

### Enabling Network Emulation

Modify `client.py` and `server.py` to pass emulation parameters to `main()`:

**Example: Enable 50ms delay, 10ms jitter, 5% packet loss**

```python
# In client.py or server.py
if __name__ == "__main__":
    asyncio.run(main(
        emulation_enabled=True,
        delay_ms=50,           # 50ms base delay
        jitter_ms=10,          # ±10ms jitter variation
        packet_loss_rate=0.05  # 5% packet loss
    ))
```

### Network Emulation Parameters

- `emulation_enabled` (bool): Enable/disable emulation (default: False)
- `delay_ms` (float): Base delay in milliseconds (default: 0)
- `jitter_ms` (float): Jitter variation in milliseconds (default: 0)
- `packet_loss_rate` (float): Probability of packet loss, 0.0 to 1.0 (default: 0.0)

### Example Configurations

**No emulation (default):**
```python
asyncio.run(main())  # Emulation disabled by default
```

**High latency network:**
```python
asyncio.run(main(emulation_enabled=True, delay_ms=100, jitter_ms=20))
```

**Lossy network:**
```python
asyncio.run(main(emulation_enabled=True, packet_loss_rate=0.1))  # 10% loss
```

**Realistic network conditions:**
```python
asyncio.run(main(
    emulation_enabled=True,
    delay_ms=50,
    jitter_ms=10,
    packet_loss_rate=0.05
))
```

### How It Works

The network emulator intercepts all `transmit()` calls in both client and server:
- **Packet Loss**: Randomly drops packets before transmission based on `packet_loss_rate`
- **Delay**: Adds a base delay before transmission using `asyncio.sleep()`
- **Jitter**: Adds random variation to delay: `delay = base_delay + random.uniform(-jitter/2, +jitter/2)`

When emulation is disabled (`enabled=False`), the emulator immediately forwards to the original `transmit()` function with zero overhead.
