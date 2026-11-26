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

MARK = "fnOS 封装补丁"          # 客户端超时补丁的标记
MARK_SEND = "扫码长轮询 6s 无事件"   # 长轮询节流补丁的标记
MARK_HANDLER = "扫码状态请求超过 90s"  # 处理器兜底的标记

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
POLL = "backend/src/auth/qrcode.rs"
POLL_ANCHOR = """        let resp = self
            .client
            .get(&url)
            .send()
            .await
            .context("Failed to poll qrcode status")?;"""
POLL_REPLACEMENT = """        // fnOS 封装补丁：/channel/unicast 是长轮询，没有事件时会挂住约 30 秒。
        // 这里只约束"这一次网络等待"，超过 6 秒就当作"仍在等待"返回，让前端
        // 立刻进入下一轮；确权与预热在拿到 v 之后才发生，不受此限制。
        let resp = match tokio::time::timeout(
            std::time::Duration::from_secs(6),
            self.client.get(&url).send(),
        )
        .await
        {
            Ok(result) => result.context("Failed to poll qrcode status")?,
            Err(_) => {
                debug!("扫码长轮询 6s 无事件，按等待中返回");
                return Ok(QRCodeStatus::Waiting);
            }
        };"""

HANDLER = "backend/src/server/handlers/auth.rs"
HANDLER_ANCHOR = "    match state.qrcode_auth.read().await.poll_status(&params.sign).await {"
HANDLER_REPLACEMENT = """    // fnOS 封装补丁：仅作为兜底（90s），防止锁或网络病态卡死。
    // 真正的长轮询节流在 poll_status 内部对单次 send() 做（6s），
    // 这里绝不能用小值——用户确认登录后的取凭证与预热要花十几秒，
    // 被这里掐断就会出现「手机点了登录，网页没反应」。
    let poll_result = match tokio::time::timeout(
        std::time::Duration::from_secs(90),
        state.qrcode_auth.read().await.poll_status(&params.sign),
    )
    .await
    {
        Ok(result) => result,
        Err(_) => {
            tracing::warn!("扫码状态请求超过 90s，按等待中返回");
            Ok(crate::auth::QRCodeStatus::Waiting)
        }
    };
    match poll_result {"""


def patch_file(path: pathlib.Path, anchor: str, mark: str = MARK) -> str:
    text = path.read_text(encoding="utf-8")
    if mark in text:
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

    poll = root / POLL
    text = poll.read_text(encoding="utf-8")
    if MARK_SEND in text:
        print("%-40s already" % (POLL + " (send)"))
    elif text.count(POLL_ANCHOR) != 1:
        raise SystemExit("补丁失败：%s 中 send() 锚点出现 %d 次，请人工核对"
                         % (POLL, text.count(POLL_ANCHOR)))
    else:
        poll.write_text(text.replace(POLL_ANCHOR, POLL_REPLACEMENT), encoding="utf-8")
        print("%-40s patched" % (POLL + " (send)"))

    handler = root / HANDLER
    text = handler.read_text(encoding="utf-8")
    if MARK_HANDLER in text:
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
