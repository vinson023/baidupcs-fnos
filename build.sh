#!/bin/bash
# BaiduPCS-Rust -> 飞牛 fnOS FPK 构建脚本
#
# 在任意可运行 Docker 的 x86_64 Linux 上执行（开发机或飞牛设备本身都可以）。
# 需要：docker、fnpack（可选 node/npm，没有则用容器构建前端）
#
# 用法：
#   bash build.sh              # 构建 fpk
#   bash build.sh --install    # 构建并安装到本机应用中心（仅飞牛设备上可用）
#
# 依赖说明：
#   - 后端在 rust:1.87-slim 容器内交叉/本地编译，不污染宿主环境
#   - 前端优先用宿主的 node，缺失时回退到 node:22-alpine 容器
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
UPSTREAM_REPO="https://github.com/komorebiCarry/BaiduPCS-Rust.git"
UPSTREAM_REF="${UPSTREAM_REF:-v2.4.1}"          # 固定上游版本，保证可复现
SRC="${ROOT}/.build/upstream"
PKG="${ROOT}/fpk/baidupcs"
OUT="${ROOT}/build"
APPNAME="$(sed -n 's/^appname *= *//p' "${PKG}/manifest" | tr -d ' \r')"
VERSION="$(sed -n 's/^version *= *//p' "${PKG}/manifest" | tr -d ' \r')"

mkdir -p "${ROOT}/.build" "${OUT}"

echo "==> 1/5 准备上游源码 (${UPSTREAM_REF})"
if [ ! -d "${SRC}/backend" ]; then
    rm -rf "${SRC}"
    git clone --depth 1 --branch "${UPSTREAM_REF}" "${UPSTREAM_REPO}" "${SRC}"
fi

echo "==> 2/5 给上游打补丁（登录客户端超时）"
if ! command -v python3 >/dev/null 2>&1; then
    echo "需要 python3 来应用补丁" >&2
    exit 1
fi
python3 "${ROOT}/tools/patch_upstream.py" "${SRC}"

echo "==> 2/5 构建前端"
if command -v npm >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
    ( cd "${SRC}/frontend" && npm ci --no-audit --no-fund && npm run build )
else
    docker run --rm -v "${SRC}/frontend:/app" -w /app node:22-alpine \
        sh -c "npm ci --no-audit --no-fund && npm run build"
fi
rm -rf "${PKG}/app/bin/frontend"
mkdir -p "${PKG}/app/bin/frontend"
cp -r "${SRC}/frontend/dist" "${PKG}/app/bin/frontend/dist"
# 上游 index.html 引用了不存在的 /vite.svg，补一个与桌面图标同源的 favicon
cp "${ROOT}/assets/vite.svg" "${PKG}/app/bin/frontend/dist/vite.svg"

echo "==> 3/5 构建 Rust 后端（容器内）"
rm -rf "${ROOT}/.build/ctx"
mkdir -p "${ROOT}/.build/ctx"
cp -r "${SRC}/backend" "${ROOT}/.build/ctx/backend"
rm -rf "${ROOT}/.build/ctx/backend/target" "${ROOT}/.build/ctx/backend/rust-toolchain.toml"
cat > "${ROOT}/.build/ctx/Dockerfile" <<'EOF'
FROM rust:1.87-slim
WORKDIR /src
COPY backend/Cargo.toml backend/Cargo.lock ./
RUN mkdir -p src && echo 'fn main(){}' > src/main.rs && CARGO_BUILD_JOBS=8 cargo build --release || true
COPY backend/ ./
RUN CARGO_BUILD_JOBS=8 cargo build --release
EOF
docker build -f "${ROOT}/.build/ctx/Dockerfile" -t baidupcs-build:latest "${ROOT}/.build/ctx"
cid="$(docker create baidupcs-build:latest)"
docker cp "${cid}:/src/target/release/baidu-netdisk-rust" "${PKG}/app/bin/baidu-netdisk-rust"
docker rm "$cid" >/dev/null
chmod 755 "${PKG}/app/bin/baidu-netdisk-rust"

echo "==> 4/5 组装应用目录"
# 图标：默认百度网盘官方云标（assets/icon）；换回中性字形请覆盖 assets/icon
cp "${ROOT}/assets/icon/icon_32.png"  "${PKG}/app/ui/images/icon_32.png"
cp "${ROOT}/assets/icon/icon_64.png"  "${PKG}/app/ui/images/icon_64.png"
cp "${ROOT}/assets/icon/icon_128.png" "${PKG}/app/ui/images/icon_128.png"
cp "${ROOT}/assets/icon/icon_256.png" "${PKG}/app/ui/images/icon_256.png"
cp "${ROOT}/assets/icon/ICON.PNG"     "${PKG}/ICON.PNG"
cp "${ROOT}/assets/icon/ICON_256.PNG" "${PKG}/ICON_256.PNG"
cp "${ROOT}/LICENSE" "${PKG}/LICENSE"
chmod +x "${PKG}"/cmd/* "${PKG}/app/bin/baidu-netdisk-rust"

echo "==> 5/5 打包"
( cd "${PKG}" && fnpack build )
mv -f "${PKG}/${APPNAME}.fpk" "${OUT}/${APPNAME}_${VERSION}_x86.fpk"
ls -lh "${OUT}/${APPNAME}_${VERSION}_x86.fpk"

if [ "${1:-}" = "--install" ]; then
    echo "==> 安装到本机应用中心"
    # 带卸载表单时 CLI 无法卸载，需在 Web UI 操作；此处仅为开发迭代便利
    rm -f "/var/apps/${APPNAME}/wizard/uninstall" 2>/dev/null || true
    appcenter-cli stop "${APPNAME}" >/dev/null 2>&1 || true
    appcenter-cli uninstall "${APPNAME}" >/dev/null 2>&1 || true
    appcenter-cli install-fpk "${OUT}/${APPNAME}_${VERSION}_x86.fpk" || true
    appcenter-cli start "${APPNAME}" || true   # 可能打印 10500，请用 status 复核
    sleep 5
    appcenter-cli status "${APPNAME}"
    curl -s -o /dev/null -w 'http %{http_code}\n' http://127.0.0.1:18888/ || true
fi
