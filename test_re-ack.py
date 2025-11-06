# test_ack_loss.py
import asyncio
import server
import client

async def run():
    server_task = asyncio.create_task(
        server.main(
            host="0.0.0.0",
            port=4433,
            emulation_enabled=True,
            delay_ms=0,
            jitter_ms=0,
            packet_loss_rate=0.2,
        )
    )
    await asyncio.sleep(0.2)

    await client.main(
        emulation_enabled=False,
    )

    server_task.cancel()

if __name__ == "__main__":
    asyncio.run(run())
