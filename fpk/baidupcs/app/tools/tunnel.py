#!/usr/bin/env python3
"""BaiduPCS for fnOS - HTTP/HTTPS 自适应前置隧道。

为什么需要它
------------
飞牛在安装时把桌面入口地址固定为
    `${entry.protocol || location.protocol}://host:port/path`
因此同一个图标在内网（http 桌面）与远程（https 桌面 / DDNS 域名）都要能打开。
而 BaiduPCS 本身只说纯 HTTP，https 页面里的 http iframe 会被浏览器按
"混合内容"直接拦掉。

这个隧道监听对外端口，先窥探每条连接的首字节：
  * 0x16（TLS 握手）→ 用飞牛已托管的证书（按 ClientHello 的 SNI 选择）终止 TLS，
    再把明文转发给本地 BaiduPCS；
  * 其它字节 → 当作纯 HTTP 直接转发。
WebSocket 在完成握手后是原始字节双向透传，同样适用。

结果：内网 http://nas:PORT 与 https://域名:PORT 共用同一个桌面图标。

两种模式
--------
  tunnel.py --prepare <network_gateway_cert.conf> <cert_dir>
        以 root 运行：把飞牛托管的证书复制到应用数据目录并写入 certs.json
  tunnel.py
        以应用用户运行：提供服务

环境变量：TUNNEL_PORT（对外端口）、UPSTREAM_PORT（本地服务端口）、CERT_DIR
"""
import json
import os
import socket
import ssl
import sys
import threading
import time

CHUNK = 65536
IDLE_TIMEOUT = 600
HEAD_LIMIT = 65536


def log(*args):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 证书准备
def prepare(conf_path, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    try:
        with open(conf_path, encoding="utf-8") as f:
            entries = json.load(f)
    except Exception as exc:
        log("prepare: 无法读取 %s (%s)" % (conf_path, exc))

    mapping = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        host = (entry.get("host") or "").strip().lower()
        crt = entry.get("cert") or ""
        key = entry.get("key") or ""
        if not host or not crt or not key:
            continue
        if not (os.path.isfile(crt) and os.path.isfile(key)):
            log("prepare: 跳过 %s（证书文件不存在）" % host)
            continue
        try:
            dst_crt = os.path.join(out_dir, host + ".crt")
            dst_key = os.path.join(out_dir, host + ".key")
            for src, dst in ((crt, dst_crt), (key, dst_key)):
                with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                    fdst.write(fsrc.read())
                os.chmod(dst, 0o600)
            mapping[host] = {"cert": os.path.basename(dst_crt),
                             "key": os.path.basename(dst_key)}
        except Exception as exc:
            log("prepare: 复制 %s 失败 (%s)" % (host, exc))

    with open(os.path.join(out_dir, "certs.json"), "w", encoding="utf-8") as f:
        json.dump(mapping, f)
    log("prepare: 可用证书 %d 张：%s" % (len(mapping), ", ".join(sorted(mapping)) or "-"))


def load_contexts(cert_dir):
    """host -> SSLContext，以及兜底 context（飞牛自签证书）。"""
    contexts = {}
    try:
        with open(os.path.join(cert_dir, "certs.json"), encoding="utf-8") as f:
            mapping = json.load(f)
    except Exception as exc:
        log("没有 certs.json（%s），TLS 关闭" % exc)
        return None, None

    for host, pair in mapping.items():
        crt = os.path.join(cert_dir, pair["cert"])
        key = os.path.join(cert_dir, pair["key"])
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(crt, key)
            ctx.set_alpn_protocols(["http/1.1"])
            ctx.options |= ssl.OP_NO_COMPRESSION
            contexts[host] = ctx
        except Exception as exc:
            log("证书加载失败 %s (%s)" % (host, exc))

    fallback = contexts.get("fallback")
    if fallback is None:
        for ctx in contexts.values():
            fallback = ctx
            break
    return contexts, fallback


def read_client_hello_sni(conn):
    """窥探 ClientHello 取出 SNI（MSG_PEEK 不消费数据）。"""
    buf = b""
    for _ in range(300):
        try:
            data = conn.recv(16384, socket.MSG_PEEK)
        except Exception:
            return None
        if not data:
            return None
        if len(data) >= 5:
            need = 5 + int.from_bytes(data[3:5], "big")
            if len(data) >= need:
                buf = data[:need]
                break
        time.sleep(0.01)
    if len(buf) < 5 or buf[0] != 0x16:
        return None
    try:
        pos = 5
        if buf[pos] != 0x01:
            return None
        pos += 4
        pos += 2 + 32
        sid_len = buf[pos]
        pos += 1 + sid_len
        cs_len = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2 + cs_len
        comp_len = buf[pos]
        pos += 1 + comp_len
        if pos + 2 > len(buf):
            return None
        ext_total = int.from_bytes(buf[pos:pos + 2], "big")
        pos += 2
        end = min(len(buf), pos + ext_total)
        while pos + 4 <= end:
            etype = int.from_bytes(buf[pos:pos + 2], "big")
            elen = int.from_bytes(buf[pos + 2:pos + 4], "big")
            body = buf[pos + 4:pos + 4 + elen]
            if etype == 0x0000 and len(body) >= 5:
                name_len = int.from_bytes(body[3:5], "big")
                return body[5:5 + name_len].decode("ascii", "ignore").lower().strip(".")
            pos += 4 + elen
    except Exception:
        return None
    return None


def read_http_head(conn, limit=HEAD_LIMIT, timeout=15):
    """读到请求头结束；非 HTTP/1.x 时返回 (None, buf)。"""
    try:
        old = conn.gettimeout()
    except Exception:
        old = None
    buf = b""
    try:
        conn.settimeout(timeout)
        while b"\r\n\r\n" not in buf:
            if len(buf) > limit:
                return None, buf
            try:
                chunk = conn.recv(4096)
            except (socket.timeout, ssl.SSLWantReadError):
                return None, buf
            if not chunk:
                return None, buf
            buf += chunk
    except Exception:
        return None, buf
    finally:
        try:
            if old is not None:
                conn.settimeout(old)
        except Exception:
            pass
    if not buf.startswith((b"GET ", b"POST ", b"PUT ", b"PATCH ", b"DELETE ",
                           b"HEAD ", b"OPTIONS ")):
        return None, buf
    idx = buf.index(b"\r\n\r\n") + 4
    return buf[:idx], buf[idx:]


# ---------------------------------------------------------------- 转发
def pump(src, dst, on_close):
    try:
        while True:
            data = src.recv(CHUNK)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        on_close()


def handle(conn, contexts, fallback, upstream):
    peer = None
    try:
        conn.settimeout(IDLE_TIMEOUT)
        try:
            peer = conn.getpeername()
        except Exception:
            pass
        first = conn.recv(1, socket.MSG_PEEK)
        if not first:
            return

        sni = None
        if first == b"\x16":                     # TLS
            sni = read_client_hello_sni(conn)
            ctx = contexts.get(sni) if (contexts and sni) else None
            ctx = ctx or fallback
            if ctx is None:
                log("收到 TLS 请求但没有可用证书，放弃该连接")
                return
            conn = ctx.wrap_socket(conn, server_side=True)

        head, surplus = read_http_head(conn)

        up = socket.create_connection(upstream, timeout=15)
        up.settimeout(IDLE_TIMEOUT)
        closed = threading.Event()

        def close_both():
            if closed.is_set():
                return
            closed.set()
            for s in (up, conn):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass

        payload = (head or b"") + surplus
        if payload:
            up.sendall(payload)

        thread = threading.Thread(target=pump, args=(up, conn, close_both), daemon=True)
        thread.start()
        pump(conn, up, close_both)
        thread.join(5)
    except ssl.SSLError as exc:
        log("TLS 握手失败 %s: %s" % (peer, exc))
    except Exception as exc:
        log("连接异常 %s: %s" % (peer, exc))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def bind_sockets(port):
    """同时监听 IPv4 与 IPv6：飞牛 DDNS 域名常常只有 AAAA 记录，
    只绑 IPv4 会让桌面 iframe 直接 ECONNREFUSED。"""
    socks = []
    try:
        v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v4.bind(("0.0.0.0", port))
        v4.listen(128)
        socks.append(("0.0.0.0", v4))
    except Exception as exc:
        log("IPv4 绑定失败: %s" % exc)
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except Exception:
            pass
        v6.bind(("::", port))
        v6.listen(128)
        socks.append(("::", v6))
    except Exception as exc:
        log("IPv6 绑定失败: %s" % exc)
    return socks


def accept_loop(srv, contexts, fallback, upstream):
    while True:
        try:
            conn, _ = srv.accept()
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log("accept 异常: %s" % exc)
            continue
        threading.Thread(target=handle, args=(conn, contexts, fallback, upstream),
                         daemon=True).start()


def serve(listen_port, upstream_port, cert_dir):
    contexts, fallback = load_contexts(cert_dir)
    upstream = ("127.0.0.1", upstream_port)
    sockets = bind_sockets(listen_port)
    if not sockets:
        raise RuntimeError("无法绑定端口 %d" % listen_port)
    log("隧道监听 %s → 127.0.0.1:%d（TLS: %s）"
        % (", ".join("%s:%d" % (h, listen_port) for h, _ in sockets),
           upstream_port, "开" if fallback else "关"))
    for idx, (_, srv) in enumerate(sockets):
        if idx == len(sockets) - 1:
            accept_loop(srv, contexts, fallback, upstream)
        else:
            threading.Thread(target=accept_loop,
                             args=(srv, contexts, fallback, upstream),
                             daemon=True).start()


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--prepare":
        prepare(sys.argv[2], sys.argv[3])
        return 0
    listen_port = int(os.environ.get("TUNNEL_PORT", "18888"))
    upstream_port = int(os.environ.get("UPSTREAM_PORT", "18889"))
    cert_dir = os.environ.get("CERT_DIR", "")
    try:
        serve(listen_port, upstream_port, cert_dir)
    except Exception as exc:
        log("致命错误: %s" % exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
