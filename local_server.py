"""
本地 MCP Server + TCP 服务端
AI 通过 MCP 协议调用工具,工具通过 TCP 转发到远程工作台执行

安装依赖:
    pip install fastmcp

使用:
    python local_server.py --port 8765 --secret mysecret123              # 测试 TCP 服务
    python local_server.py --port 8765 --secret mysecret123 --mcp-only   # MCP 模式

AI 客户端 MCP 配置 (stdio 传输):
    command: python
    args: ["/path/to/local_server.py", "--port", "8765", "--secret", "mysecret123", "--mcp-only"]
"""

import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import uuid

from mcp.server.fastmcp import FastMCP

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ========== TCP 连接管理 ==========

connected_clients: dict[str, dict] = {}
pending_requests: dict[str, asyncio.Future] = {}

HEARTBEAT_INTERVAL = 20
HEARTBEAT_TIMEOUT = 60


async def send_json(writer, obj):
    writer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    await writer.drain()


async def handle_client(client_id, reader, writer):
    try:
        auth_line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not auth_line:
            return
        auth = json.loads(auth_line)
        if auth.get("secret") != expected_secret:
            await send_json(writer, {"type": "auth_fail", "message": "鉴权失败"})
            return

        info = auth.get("info", {})
        connected_clients[client_id] = {
            "reader": reader,
            "writer": writer,
            "info": info,
            "last_pong": time.monotonic(),
        }
        await send_json(writer, {"type": "auth_ok"})
        print(f"[远程] 客户端 {client_id} 已连接 - {info.get('hostname', 'unknown')}")

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "pong":
                connected_clients[client_id]["last_pong"] = time.monotonic()
                continue
            req_id = data.get("req_id")
            if req_id and req_id in pending_requests:
                fut = pending_requests.pop(req_id)
                if not fut.done():
                    fut.set_result(data)
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    except Exception as e:
        print(f"[远程] 客户端 {client_id} 异常: {e}")
    finally:
        connected_clients.pop(client_id, None)
        for req_id, fut in list(pending_requests.items()):
            if not fut.done():
                fut.set_result({"type": "error", "success": False, "error": "连接断开"})
        try:
            writer.close()
        except Exception:
            pass
        print(f"[远程] 客户端 {client_id} 已断开")


async def client_acceptor(reader, writer):
    await handle_client(str(uuid.uuid4())[:8], reader, writer)


async def heartbeat_loop():
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        now = time.monotonic()
        for cid, client in list(connected_clients.items()):
            try:
                await send_json(client["writer"], {"type": "ping"})
            except Exception:
                client["writer"].close()
                connected_clients.pop(cid, None)
                continue
            if now - client["last_pong"] > HEARTBEAT_TIMEOUT:
                print(f"[远程] 客户端 {cid} 心跳超时,断开")
                client["writer"].close()


async def send_to_remote(command_type: str, req_id: str = None, **kwargs) -> dict:
    if not connected_clients:
        return {"success": False, "error": "没有远程客户端连接,请先在工作台运行 remote_client.py"}
    client_id, client = next(iter(connected_clients.items()))
    req_id = req_id or str(uuid.uuid4())
    fut = asyncio.get_running_loop().create_future()
    pending_requests[req_id] = fut
    try:
        await send_json(client["writer"], {"type": command_type, "req_id": req_id, **kwargs})
        result = await asyncio.wait_for(fut, timeout=kwargs.get("timeout", 300))
        return result
    except asyncio.TimeoutError:
        pending_requests.pop(req_id, None)
        return {"success": False, "error": "命令执行超时"}
    except Exception as e:
        pending_requests.pop(req_id, None)
        return {"success": False, "error": str(e)}


# ========== MCP 工具定义 ==========

mcp = FastMCP("remote-terminal")


@mcp.tool()
def remote_exec(command: str, timeout: int = 300) -> str:
    """在远程工作台执行 shell 命令并返回输出。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时时间(秒),默认 300
    """
    result = call_tcp("exec", command=command, timeout=timeout)
    if not result.get("success"):
        return f"错误: {result.get('error', '未知错误')}"
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    exit_code = result.get("exit_code", -1)
    output = stdout
    if stderr:
        output += f"\n[stderr]\n{stderr}"
    if exit_code != 0:
        output += f"\n[exit code: {exit_code}]"
    return output.strip() or "(无输出)"


@mcp.tool()
def remote_status() -> str:
    """查看远程连接状态。"""
    if not connected_clients:
        return "❌ 没有远程客户端连接"
    lines = [f"✅ 已连接 {len(connected_clients)} 个远程客户端:"]
    for cid in connected_clients:
        lines.append(f"  - {cid}")
    return "\n".join(lines)


DEFAULT_CHUNK_SIZE = 1024 * 1024


@mcp.tool()
def remote_upload(local_path: str, remote_path: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """上传本地文件到远程工作台(自动分块,支持大文件)。

    Args:
        local_path: 本地文件路径(Windows/Linux 均可)
        remote_path: 远程保存路径(建议绝对路径)
        chunk_size: 分块大小(字节),默认 1MB
    """
    if not os.path.isfile(local_path):
        return f"错误: 本地文件不存在: {local_path}"
    size = os.path.getsize(local_path)
    total = max(1, (size + chunk_size - 1) // chunk_size)

    session_id = str(uuid.uuid4())
    r = call_tcp("upload_start", req_id=session_id, path=remote_path, size=size, total_chunks=total)
    if not r.get("success"):
        return f"错误: 启动上传失败: {r.get('error', '未知错误')}"

    try:
        with open(local_path, "rb") as f:
            for seq in range(total):
                raw = f.read(chunk_size)
                r = call_tcp("upload_chunk", req_id=session_id, seq=seq, data_b64=base64.b64encode(raw).decode("ascii"))
                if not r.get("success"):
                    return f"错误: 第 {seq + 1}/{total} 块传输失败: {r.get('error', '未知错误')}"
    except OSError as e:
        return f"错误: 读取本地文件失败: {e}"

    r = call_tcp("upload_end", req_id=session_id)
    if not r.get("success"):
        return f"错误: 上传收尾失败: {r.get('error', '未知错误')}"
    return f"✅ 上传成功: {local_path} → {remote_path} ({size:,} 字节, {total} 块)"


@mcp.tool()
def remote_download(remote_path: str, local_path: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """下载远程工作台文件到本地(自动分块,支持大文件)。

    Args:
        remote_path: 远程文件路径
        local_path: 本地保存路径(自动创建父目录)
        chunk_size: 分块大小(字节),默认 1MB
    """
    session_id = str(uuid.uuid4())
    r = call_tcp("download_start", req_id=session_id, path=remote_path, chunk_size=chunk_size)
    if not r.get("success"):
        return f"错误: 启动下载失败: {r.get('error', '未知错误')}"
    size = int(r.get("size", 0))
    total = int(r.get("total_chunks", 0))

    try:
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        with open(local_path, "wb") as f:
            for seq in range(total):
                r = call_tcp("download_chunk", req_id=session_id, seq=seq)
                if not r.get("success"):
                    return f"错误: 第 {seq + 1}/{total} 块下载失败: {r.get('error', '未知错误')}"
                f.write(base64.b64decode(r.get("data_b64", "")))
    except OSError as e:
        return f"错误: 写入本地文件失败: {e}"

    call_tcp("download_end", req_id=session_id)
    return f"✅ 下载成功: {remote_path} → {local_path} ({size:,} 字节, {total} 块)"


# ========== 启动 ==========

tcp_loop: asyncio.AbstractEventLoop = None


async def start_tcp_server(host, port):
    server = await asyncio.start_server(client_acceptor, host, port, limit=256 * 1024 * 1024)
    print(f"[TCP] 服务已启动: {host}:{port} (limit=256MB)")
    async with server:
        await server.serve_forever()


def run_tcp_loop(host, port):
    global tcp_loop
    tcp_loop = asyncio.new_event_loop()

    async def run_all():
        await asyncio.gather(start_tcp_server(host, port), heartbeat_loop())

    tcp_loop.run_until_complete(run_all())


def call_tcp(command_type: str, **kwargs) -> dict:
    """提交请求到 TCP 事件循环并同步等待结果(供 MCP 工具调用)"""
    fut = asyncio.run_coroutine_threadsafe(send_to_remote(command_type, **kwargs), tcp_loop)
    return fut.result(timeout=kwargs.get("timeout", 300) + 30)

def main():
    global expected_secret
    parser = argparse.ArgumentParser(description="远程终端 MCP Server")
    parser.add_argument("--port", type=int, default=8765, help="TCP 端口")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="监听地址")
    parser.add_argument("--secret", type=str, default="mysecret123", help="连接密钥")
    parser.add_argument("--mcp-only", action="store_true", help="只启动 MCP (stdio),不跑前台 TCP")
    args = parser.parse_args()
    expected_secret = args.secret

    if args.mcp_only:
        t = threading.Thread(target=run_tcp_loop, args=(args.host, args.port), daemon=True)
        t.start()
        print(f"[TCP] TCP 服务在后台线程运行: {args.host}:{args.port}")
        mcp.run()
    else:
        print("[本地] 只启动 TCP 服务端 (测试模式)")
        print("[本地] MCP 模式请加 --mcp-only 参数")
        asyncio.run(asyncio.gather(start_tcp_server(args.host, args.port), heartbeat_loop()))


if __name__ == "__main__":
    main()
