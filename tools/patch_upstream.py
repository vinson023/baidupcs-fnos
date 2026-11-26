#!/usr/bin/env python3
"""给上游 BaiduPCS-Rust 打 fnOS 封装补丁（可重复执行）。

补丁内容
--------
登录相关的 HTTP 客户端只设了 cookie / UA，没有任何超时。一旦出站连接被吞
（路由抖动、IPv6 黑洞），内核 SYN 重试会拖到约 130 秒，扫码轮询整体卡住，
界面表现就是「首次扫码无反应，刷新一次二维码才好」。

这里给 `auth/qrcode.rs` 与 `auth/cookie_login.rs` 的客户端补上：
  * connect_timeout 10s —— 连不上就快速失败，前端每 3 秒轮询会自动重试
  * timeout 30s         —— 单个登录请求的总超时

注意：只作用于登录客户端。下载/上传的客户端不能加总超时，否则大文件传输会被
中途掐断，所以那里保持原样。

用法：python3 patch_upstream.py <上游源码根目录>
"""
import sys
import pathlib

MARK = "fnOS 封装补丁"

TIMEOUTS = """
            // fnOS 封装补丁：登录客户端原本没有超时，出站连接被吞时会卡满内核
            // SYN 重试窗口（约 130s），表现为「首次扫码无反应，刷新后才正常」。
            .connect_timeout(std::time::Duration::from_secs(10))
            .timeout(std::time::Duration::from_secs(30));"""

TARGETS = {
    "backend/src/auth/qrcode.rs": ".user_agent(USER_AGENT);",
    "backend/src/auth/cookie_login.rs": ".user_agent(USER_AGENT);",
}

# 处理器层面兜底：不管卡在 DNS、连接、TLS 还是锁上，都要在 10 秒内给前端一个结果，
# 这样前端 3 秒一次的轮询才能继续推进，而不是整个界面停在"等待扫码"。
HANDLER = "backend/src/server/handlers/auth.rs"
HANDLER_ANCHOR = "    match state.qrcode_auth.read().await.poll_status(&params.sign).await {"
HANDLER_REPLACEMENT = """    // fnOS 封装补丁：百度的 /channel/unicast 是长轮询，没有事件时会挂住约 30 秒。
    // 前端本身每 3 秒查一次，所以这里把单次等待压到 6 秒，超时就按"仍在等待"返回，
    // 让前端立刻进入下一轮；否则首次打开时多个 30 秒长轮询叠在一起，界面会长时间
    // 停在"等待扫码"，表现为「首次扫码无反应，刷新一次才好」。
    let poll_result = match tokio::time::timeout(
        std::time::Duration::from_secs(6),
        state.qrcode_auth.read().await.poll_status(&params.sign),
    )
    .await
    {
        Ok(result) => result,
        Err(_) => {
            tracing::debug!("扫码长轮询超时，按等待中返回");
            Ok(crate::auth::QRCodeStatus::Waiting)
        }
    };
    match poll_result {"""


def patch_file(path: pathlib.Path, anchor: str) -> str:
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "already"
    if text.count(anchor) != 1:
        raise SystemExit(
            "补丁失败：%s 中锚点 %r 出现 %d 次（上游可能已改动），请人工核对"
            % (path, anchor, text.count(anchor))
        )
    new = text.replace(anchor, anchor[: -len(";")] + TIMEOUTS)
    path.write_text(new, encoding="utf-8")
    return "patched"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    root = pathlib.Path(sys.argv[1])
    if not (root / "backend" / "Cargo.toml").is_file():
        raise SystemExit("不是上游源码目录：%s" % root)

    for rel, anchor in TARGETS.items():
        target = root / rel
        if not target.is_file():
            raise SystemExit("缺少文件：%s" % target)
        print("%-40s %s" % (rel, patch_file(target, anchor)))

    handler = root / HANDLER
    text = handler.read_text(encoding="utf-8")
    if MARK in text:
        print("%-40s already" % HANDLER)
    elif text.count(HANDLER_ANCHOR) != 1:
        raise SystemExit("补丁失败：%s 中锚点出现 %d 次，请人工核对"
                         % (HANDLER, text.count(HANDLER_ANCHOR)))
    else:
        handler.write_text(text.replace(HANDLER_ANCHOR, HANDLER_REPLACEMENT), encoding="utf-8")
        print("%-40s patched" % HANDLER)
    return 0


if __name__ == "__main__":
    sys.exit(main())
