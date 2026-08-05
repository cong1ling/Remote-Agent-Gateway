"""
远程工作台客户端 - 主动连接本地 MCP Server (通过 cpolar 内网穿透)
接收并执行本地 AI 下发的 shell 命令

使用:
    python remote_client.py --url 31.tcp.cpolar.top:14645 --secret mysecret123 --workdir /mnt/workspace
    python remote_client.py --host 127.0.0.1 --port 8765 --secret mysecret123   # 本地联调
"""

import argparse
import asyncio
import base64
import json
import os
import socket
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RECONNECT_DELAY = 3


def parse_url(url: str) -> tuple:
    for prefix in ("tcp://", "http://", "https://", "ws://", "wss://"):
        if url.startswith(prefix):
            url = url[len(prefix):]
    url = url.split("/", 1)[0]
    host, _, port = url.rpartition(":")
    if not port.isdigit():
        raise ValueError(f"无法解析地址: {url}")
    return host, int(port)


async def send_json(writer, obj):
    writer.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
    await writer.drain()


async def _exec_command(data):
    req_id = data.get("req_id")
    command = data.get("command", "")
    timeout = float(data.get("timeout", 300))
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:
        return {"type": "exec_result", "req_id": req_id, "success": False, "error": str(e), "stdout": "", "stderr": "", "exit_code": -1}
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5)
        except Exception:
            pass
        return {"type": "exec_result", "req_id": req_id, "success": False, "error": f"命令执行超时 ({timeout}s)", "stdout": "", "stderr": "", "exit_code": -1}
    return {
        "type": "exec_result", "req_id": req_id, "success": True,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
        "exit_code": proc.returncode,
    }


# ========== 文件传输状态 ==========

_uploads: dict[str, dict] = {}
_downloads: dict[str, dict] = {}

DEFAULT_CHUNK_SIZE = 1024 * 1024


def _ensure_parent(path: str):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


async def _upload_start(data: dict) -> dict:
    req_id = data.get("req_id")
    path = data.get("path", "")
    if not path:
        return {"type": "upload_ready", "req_id": req_id, "success": False, "error": "缺少路径"}
    try:
        _ensure_parent(path)
        f = open(path, "wb")
    except Exception as e:
        return {"type": "upload_ready", "req_id": req_id, "success": False, "error": str(e)}
    _uploads[req_id] = {"file": f, "total": int(data.get("total_chunks", 0)), "received": 0, "path": path, "size": int(data.get("size", 0))}
    return {"type": "upload_ready", "req_id": req_id, "success": True}


async def _upload_chunk(data: dict) -> dict:
    req_id = data.get("req_id")
    up = _uploads.get(req_id)
    if not up:
        return {"type": "upload_ack", "req_id": req_id, "success": False, "error": "未知上传会话"}
    try:
        raw = base64.b64decode(data.get("data_b64", ""))
        up["file"].write(raw)
        up["received"] += 1
    except Exception as e:
        return {"type": "upload_ack", "req_id": req_id, "success": False, "error": str(e)}
    return {"type": "upload_ack", "req_id": req_id, "seq": data.get("seq"), "success": True}


async def _upload_end(data: dict) -> dict:
    req_id = data.get("req_id")
    up = _uploads.pop(req_id, None)
    if not up:
        return {"type": "upload_result", "req_id": req_id, "success": False, "error": "未知上传会话"}
    try:
        up["file"].close()
    except Exception as e:
        return {"type": "upload_result", "req_id": req_id, "success": False, "error": str(e)}
    return {"type": "upload_result", "req_id": req_id, "success": True, "path": up["path"], "size": up["size"]}


async def _download_start(data: dict) -> dict:
    req_id = data.get("req_id")
    path = data.get("path", "")
    if not path or not os.path.isfile(path):
        return {"type": "download_error", "req_id": req_id, "success": False, "error": f"文件不存在: {path}"}
    chunk_size = int(data.get("chunk_size", DEFAULT_CHUNK_SIZE))
    try:
        f = open(path, "rb")
    except Exception as e:
        return {"type": "download_error", "req_id": req_id, "success": False, "error": str(e)}
    size = os.path.getsize(path)
    total = max(1, (size + chunk_size - 1) // chunk_size)
    _downloads[req_id] = {"file": f, "seq": 0, "total": total, "chunk_size": chunk_size}
    return {"type": "download_started", "req_id": req_id, "success": True, "path": path, "size": size, "total_chunks": total}


async def _download_chunk(data: dict) -> dict:
    req_id = data.get("req_id")
    dl = _downloads.get(req_id)
    if not dl:
        return {"type": "download_error", "req_id": req_id, "success": False, "error": "未知下载会话"}
    try:
        raw = dl["file"].read(dl["chunk_size"])
        dl["seq"] += 1
    except Exception as e:
        return {"type": "download_error", "req_id": req_id, "success": False, "error": str(e)}
    return {
        "type": "download_chunk_data", "req_id": req_id,
        "seq": data.get("seq"), "done": dl["seq"] >= dl["total"],
        "success": True, "data_b64": base64.b64encode(raw).decode("ascii"),
    }


async def _download_end(data: dict) -> dict:
    req_id = data.get("req_id")
    dl = _downloads.pop(req_id, None)
    if dl:
        try:
            dl["file"].close()
        except Exception:
            pass
    return {"type": "download_result", "req_id": req_id, "success": True}


async def handle_command(data):
    cmd_type = data.get("type")
    if cmd_type == "ping":
        return {"type": "pong"}
    if cmd_type == "exec":
        return await _exec_command(data)
    if cmd_type == "upload_start":
        return await _upload_start(data)
    if cmd_type == "upload_chunk":
        return await _upload_chunk(data)
    if cmd_type == "upload_end":
        return await _upload_end(data)
    if cmd_type == "download_start":
        return await _download_start(data)
    if cmd_type == "download_chunk":
        return await _download_chunk(data)
    if cmd_type == "download_end":
        return await _download_end(data)
    return {"type": "error", "req_id": data.get("req_id"), "success": False, "error": f"未知命令: {cmd_type}"}


async def main_loop(reader, writer, secret):
    await send_json(writer, {"type": "auth", "secret": secret, "info": {"hostname": socket.gethostname(), "pid": os.getpid()}})
    line = await reader.readline()
    if not line:
        return
    resp = json.loads(line)
    if resp.get("type") != "auth_ok":
        print(f"鉴权失败: {resp.get('message', resp)}")
        return
    print(f"✅ 连接成功,鉴权通过 (工作目录: {os.getcwd()})")
    while True:
        line = await reader.readline()
        if not line:
            break
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = await handle_command(data)
        if result:
            await send_json(writer, result)


async def run(host, port, secret, workdir):
    if workdir:
        os.chdir(workdir)
    print(f"连接 {host}:{port} (密钥: {secret})")
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port, limit=256 * 1024 * 1024)
            await main_loop(reader, writer, secret)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, json.JSONDecodeError) as e:
            print(f"⚠️ 连接断开: {e}")
        except Exception as e:
            print(f"⚠️ 异常: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        print(f"⏳ {RECONNECT_DELAY} 秒后重连...")
        await asyncio.sleep(RECONNECT_DELAY)


def main():
    parser = argparse.ArgumentParser(description="远程工作台客户端")
    parser.add_argument("--url", help="本地服务公网地址,如 31.tcp.cpolar.top:14645")
    parser.add_argument("--host", default="127.0.0.1", help="本地服务地址(未指定 --url 时使用)")
    parser.add_argument("--port", type=int, default=8765, help="本地服务端口")
    parser.add_argument("--secret", required=True, help="连接密钥")
    parser.add_argument("--workdir", help="工作目录,默认保持当前目录")
    args = parser.parse_args()

    if args.url:
        host, port = parse_url(args.url)
    else:
        host, port = args.host, args.port

    try:
        asyncio.run(run(host, port, args.secret, args.workdir))
    except KeyboardInterrupt:
        print("\n已退出")


if __name__ == "__main__":
    main()
