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
MARK_HANDLER = "同一个二维码只允许一个请求真正去打百度"  # 单飞补丁的标记

TIMEOUTS = """
            // fnOS 封装补丁：登录客户端原本没有超时，出站连接被吞时会卡满内核
            // SYN 重试窗口（约 130s），表现为「首次扫码无反应，刷新后才正常」。
            .connect_timeout(std::time::Duration::from_secs(10))
            .timeout(std::time::Duration::from_secs(60));"""

TARGETS = {
    "backend/src/auth/qrcode.rs": ".user_agent(USER_AGENT);",
    "backend/src/auth/cookie_login.rs": ".user_agent(USER_AGENT);",
}

# 处理器层面兜底：不管卡在 DNS、连接、TLS 还是锁上，都要在 10 秒内给前端一个结果，
# 这样前端 3 秒一次的轮询才能继续推进，而不是整个界面停在"等待扫码"。
HANDLER = "backend/src/server/handlers/auth.rs"
HANDLER_ANCHOR = "    match state.qrcode_auth.read().await.poll_status(&params.sign).await {"
HANDLER_REPLACEMENT = """    // fnOS 封装补丁：百度的 /channel/unicast 是长轮询，没有事件时会挂住约 30 秒，
    // 而前端每 3 秒查一次，首屏就会叠出多个并发长轮询，界面长时间拿不到结果。
    //
    // 注意：**不能**用"短超时"来解决——把长轮询提前中断会把扫码事件丢掉，
    // 表现就是「首次扫码没反应，刷新后才好」。
    //
    // 这里按 sign 做"单飞"：同一个二维码只允许一个请求真正去打百度，其余立刻
    // 返回"等待中"；换了新二维码（sign 不同）立刻放行，所以刷新后不会被旧请求挡住。
    // 另加 60 秒看门狗，仅防止病态卡死。
    static QR_POLL_SIGN: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);
    struct PollGuard<'a>(&'a str);
    impl Drop for PollGuard<'_> {
        fn drop(&mut self) {
            if let Ok(mut guard) = QR_POLL_SIGN.lock() {
                if guard.as_deref() == Some(self.0) {
                    *guard = None;
                }
            }
        }
    }
    {
        let mut busy = match QR_POLL_SIGN.lock() {
            Ok(guard) => guard,
            Err(poisoned) => poisoned.into_inner(),
        };
        if busy.as_deref() == Some(params.sign.as_str()) {
            return Ok(Json(ApiResponse::success(QRCodeStatusResponse::Waiting)));
        }
        *busy = Some(params.sign.clone());
    }
    let _poll_guard = PollGuard(params.sign.as_str());
    let poll_result = match tokio::time::timeout(
        std::time::Duration::from_secs(60),
        state.qrcode_auth.read().await.poll_status(&params.sign),
    )
    .await
    {
        Ok(result) => result,
        Err(_) => {
            tracing::warn!("扫码状态请求超过 60s，按等待中返回");
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

    handler = root / HANDLER
    text = handler.read_text(encoding="utf-8")
    if "同一个二维码只允许一个请求" in text:
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
