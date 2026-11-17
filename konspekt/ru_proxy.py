"""Мини HTTP-CONNECT прокси для туннеля сервер→Mac→edu.vsu.ru."""
import asyncio
import sys

UPSTREAM_ALLOW = (b"edu.vsu.ru", b"bbb.edu.vsu.ru")  # только ВГУ

async def handle(client: asyncio.StreamWriter, first_line: str, data: bytes):
    try:
        parts = first_line.split()
        if parts[0] == b"CONNECT":
            host, port = parts[1].decode().rsplit(":", 1)
            if not any(host.encode().endswith(h) for h in UPSTREAM_ALLOW):
                client.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                await client.drain()
                return
            upstream = await asyncio.open_connection(host, int(port))
            client.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await client.drain()
            await asyncio.gather(pipe(client, upstream[1]), pipe(upstream[1], client))
        else:
            # обычный HTTP-запрос: /path HTTP/1.1 + Host: header
            host = None
            for line in data.split(b"\r\n"):
                if line.lower().startswith(b"host:"):
                    host = line.split(b":", 1)[1].strip().decode()
                    break
            if not host:
                return
            upstream = await asyncio.open_connection(host, 80)
            upstream[1].write(data)
            await upstream[1].drain()
            await asyncio.gather(pipe(client, upstream[1]), pipe(upstream[1], client))
    except Exception as e:
        print(f"handle err: {e}", file=sys.stderr, flush=True)
    finally:
        try:
            client.close()
        except Exception:
            pass


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def server_main(port: int = 3128):
    async def on_conn(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(65536), timeout=15)
            if not data:
                writer.close()
                return
            first_line = data.split(b"\r\n")[0].decode(errors="replace")
            await handle(writer, first_line.encode() if isinstance(first_line, str) else first_line, data)
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    srv = await asyncio.start_server(on_conn, "127.0.0.1", port)
    print(f"proxy on 127.0.0.1:{port}", flush=True)
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(server_main(int(sys.argv[1]) if len(sys.argv) > 1 else 3128))
