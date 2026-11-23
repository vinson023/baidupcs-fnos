# BaiduPCS-Rust 封装为飞牛 fnOS FPK 应用 — 可行性分析报告

- 分析对象：https://github.com/komorebiCarry/BaiduPCS-Rust （Apache-2.0，v2.4.1，最后提交 2026-11-10）
- 依据文档：`C:\飞牛开发文档\飞牛开发文档`（developer.fnnas.com 离线镜像，fnpack V1.2.3 / 开放 API 2026-07-31 版）
- 验证环境：fnOS 测试机 一台 x86_64 测试机，Debian 12 bookworm，x86_64，16 核 / 8 GB，glibc 2.36，Docker 28.5.2，fnpack 1.2.x，appcenter-cli 1.0.2
- 当前包版本：`baidupcs 2.4.1-6`；结论日期：2026-11-23

---

## 一、结论

**可行，并且已经跑通全链路。** 不是"理论可行"——已在本机产出可安装的 `.fpk`，并在测试机上完成安装、启动、访问、目录选择验证：

| 验证项 | 结果 |
|---|---|
| `fnpack build` 出包 | 通过，`baidupcs.fpk` 20.7 MB |
| `appcenter-cli install-fpk`（带向导参数） | 安装成功，桌面图标注册 1 条 |
| `cmd/main start` | 进程以 `admin` 身份运行，`status` 返回 running |
| HTTP `/`、`/health`、`/vite.svg` | 200 / `{"status":"ok"}` / 200 |
| `/api/v1/fs/roots`（选择器入口） | `code:0`，`defaultPath=/vol1/1000/downloads` |
| `/api/v1/fs/list?path=/vol1`（原报错点） | `code:0`，可正常列出目录 |
| WebSocket `/api/v1/ws` | 101 Switching Protocols |
| 下载目录写入 | 通过（用户目录属主 `admin`，可直接写） |
| 上传接口 `POST /api/v1/uploads` | 路由可达，返回 `401 无活跃账号`（未登录；路径校验在其之后，已被放开） |

推荐形态：**原生应用（非 Docker）+ 端口入口**。统一网关（`/app/{appname}`）不能直接使用，原因见 5.6。

---

## 二、被封装对象的技术画像

| 维度 | 事实 | 对 FPK 的影响 |
|---|---|---|
| 形态 | 单个 Rust 二进制 `baidu-netdisk-rust`（56 MB，未 strip） | 天然适合 Native 包，无需 `install_dep_apps` |
| Web 框架 | axum 0.7 + tokio，`ServeDir` 提供 SPA | 一个端口同时给前端与 API，入口只需一个 |
| 运行时依赖 | `ldd` 仅 libgcc_s / libm / libc（rustls 替代 OpenSSL，rusqlite bundled） | 无需声明运行时，无 OpenSSL 版本坑 |
| 网络模型 | 仅 `TcpListener`（默认 18888），**不支持 Unix Socket** | 想接统一网关必须改代码，见 5.6 |
| 前端 | Vue 3 + Vite，`base=/`，API 硬编码 `/api/v1`（13+ 处） | SPA 无法部署在子路径下 |
| 配置 | `config/app.toml`，**相对进程 CWD 解析**；缺失时自动生成 | 决定运行目录映射必须用 CWD 而非软链 |
| 数据 | SQLite + `wal/` + `logs/`，均相对 CWD | 全部可收敛到 `@appdata` |
| 目录权限 | `filesystem.allowed_paths` 非空即为白名单 | 会限制选择器可浏览范围，见 5.8 |
| 进程控制 | 显式处理 SIGTERM 并优雅关闭 | `stop` 干净收尾，不写坏 WAL |
| 品牌标识 | 无位图 LOGO；界面统一使用 Element Plus `FolderOpened` 图标 + `#409eff` | 图标按此字形还原，见 5.9 |
| 许可 | Apache-2.0 | 允许再分发，需保留 LICENSE（已随包） |

---

## 三、FPK 规格设计（逐项对应文档）

| 文件 | 设计 | 文档依据 |
|---|---|---|
| `manifest` | `platform=x86`；`service_port=18888`；`checkport=false`；`ctl_stop=true`；`desktop_uidir=ui`；`os_min_version=1.1.3100`；`developer/publisher/maintainer/distributor=vinson023` | Manifest |
| `config/privilege` | `run-as: root` + `username: baidupcs`（受控偏离，见 5.4） | 应用权限 |
| `config/resource` | `data-share: baidupcs/downloads` | 应用资源 |
| `app/ui/config` | `type=iframe`、`protocol=""`、`port=18888`、`url="/"`、`accessPerm=readonly` | 应用入口 |
| `cmd/main` | 单一控制器：`start/stop/restart/status` + 6 个生命周期钩子 | 应用框架 |
| `wizard/install` | **下载目录必填**（绝对路径校验）+ 运行用户，两步骤 | 用户向导 |
| `wizard/config` | 下载目录与运行用户均可"留空 = 保持当前值" | 用户向导 |
| `wizard/uninstall` | 「是否保留配置文件」两个选项；不保留时彻底清理 | 用户向导 |
| `app/bin/frontend/dist` | 放在 `exe_dir` 下，命中上游 `detect_frontend_dir()` 第 8 号候选 | 上游 `main.rs` |

运行时目录映射（全部落在 fnOS 约定位置，升级不丢数据）：

| 内容 | 路径 | 环境变量 |
|---|---|---|
| 程序 / 前端 | `/volN/@appcenter/baidupcs/bin` | `TRIM_APPDEST` |
| `app.toml` | `…/@appdata/baidupcs/config/app.toml`，软链到 `@appconf/baidupcs/app.toml` | `TRIM_PKGVAR` / `TRIM_PKGETC` |
| DB / 备份 / 快照 | `…/@appdata/baidupcs/config/` | `TRIM_PKGVAR` |
| WAL / 日志 | `…/@appdata/baidupcs/{wal,logs}` | `TRIM_PKGVAR` |
| 声明共享目录 | `/volN/@appshare/baidupcs/downloads` | `TRIM_DATA_SHARE_PATHS` |
| 下载目录（向导指定） | 用户填写的绝对路径，写入 `download_dir` / `default_path` | 向导变量 |

---

## 四、实测记录（可复现）

| 阶段 | 命令 | 结果 |
|---|---|---|
| 前端构建 | `npm ci && npm run build`（Node 22） | 1m23s + 30s，产物 2.4 MB |
| 后端构建 | `docker build`（`rust:1.87-slim`，16 核，`CARGO_BUILD_JOBS=8`） | 4m12s（依赖层缓存后纯编译 2m52s） |
| 二进制 | `docker cp` | 56 MB，`ldd` = libc/libm/libgcc_s |
| 打包 | `fnpack build --directory baidupcs` | 秒级，20.7 MB |
| 安装 | `appcenter-cli install-fpk … --env config.env` | 约 10 s |
| 启动 | `appcenter-cli start baidupcs` | `/health` 在 ~2 s 内 200 |
| 桌面入口 | 查 `app_service` 表 | `baidupcs.main`，`iframe`，`://${host}:18888/` |
| 选择器 | `/fs/roots`、`/fs/list?path=/vol1` | 均 `code:0` |

构建不需要在开发机装 Rust：直接在测试机用 Docker 编译，4 分钟可重复出包（`build.sh` 已固化）。

---

## 五、必须处理的问题，以及本次采用的解法

**5.1 路径全部相对 CWD**
上游 `APP_CONFIG_PATH = "config/app.toml"`、`wal_dir`、`log_dir` 都是相对路径。若把 `@appconf` 软链进安装目录，会因为安装目录里已存在 `config/`，`ln -s` 变成在目录内建链（实测踩到），且运行时产物会写进 `@appcenter`（升级即丢）。
→ **解法**：CWD 设为 `TRIM_PKGVAR`；前端放 `exe_dir/frontend/dist` 供探测命中；`@appconf/app.toml` 仅作软链。

**5.2 配置模板必须补必填字段**
`[download]` 下 `max_global_threads`、`max_concurrent_tasks`、`chunk_size_mb`、`max_retries` 无 `serde(default)`，缺一项即启动失败（实测 `missing field max_global_threads`）。
→ 生成器已含全部必填键；策略为"首次生成、之后尊重用户手改"。

**5.3 共享目录属主是包用户**
`data-share` 创建的目录属主为 `baidupcs:baidupcs`、权限 `0700`，运行用户无法写入，上游会抛"无法创建下载目录"后退出。
→ 启动时 `chown` 给运行用户并 `chmod 775`；对向导指定的任意路径，仅在"属主为 root（即本脚本创建）"时才移交，绝不改动 NAS 用户自己的目录。

**5.4 `/volN` 的 ACL 模型决定运行身份**
fnOS 存储卷是 Windows ACL 语义，非属主的写权限由 ACL 层裁决，`setfacl` 不可靠。下载类应用要写用户目录，可靠做法是**以 NAS 用户身份运行**。
→ `privilege` 用 `run-as: root` 让脚本能 `chown`/切换身份，服务用 `setpriv --reuid --init-groups` 降权到 `admin`（缺 `setpriv` 回退 `runuser`），`exec` 保证 PID 即业务进程，SIGTERM 直达。属**受控偏离**：服务本身不以 root 运行，运行用户可在向导中修改。

**5.5 端口固定与入口一致性**
fnOS 安装时把入口 URL 固化进应用记录，事后改端口会导致图标打开空页。
→ 端口在 `manifest`、`ui/config`、生成配置三处同源（18888）；`checkport=false` 避免端口被占时安装失败；启动前用 `ss` 检测占用并写入 `TRIM_TEMP_LOGFILE` 供用户看到原因。

**5.6 统一网关不可直接用（本次不采用）**
网关要求服务监听 `${TRIM_APPDEST}/app.sock` 并挂在 `/app/{appname}`。上游两点不满足：只有 `TcpListener`；前端 `base=/`、API 全绝对路径。
→ 走网关需二次开发（unix socket + base path/前缀重写），不建议放进 v1。

**5.7 生命周期脚本必须快速返回**
fnOS 的启动 RPC 会在脚本返回后立即校验状态，脚本若阻塞等端口就绪会被判为启动失败。
→ `start` 只负责拉起进程并写 PID，不做阻塞式健康等待。

**5.8 `allowed_paths` 白名单会让选择器失效（已修）**
v1 把 `allowed_paths` 写成"共享目录 + 授权目录"，导致内置目录选择器连 `/vol1` 都进不去，报 `路径不在允许访问的范围内: /vol1`（用户截图）。
→ 改为 `allowed_paths = []`（不限制），实际读写权限交给 fnOS ACL 裁决；并用配置版本号（schema 2）在升级时自动清理旧白名单。实测 `/fs/roots`、`/fs/list?path=/vol1` 均 `code:0`。

**5.9 图标与 favicon（已改）**
第一版用 Element Plus `FolderOpened` 字形（蓝色圆角底），因为上游没有位图 LOGO。
→ 现改为**百度网盘官方云标**：从 App Store 公开的 512×512 官方应用图标中裁出云标，
居中合成白底圆角图块（32/64/128/256），并以内嵌 PNG 的 SVG 替换缺失的 `/vite.svg`。
中性文件夹字形保留在 `assets/icon-neutral/`，需要时覆盖 `assets/icon/` 即可换回。

> ⚠️ 商标提示：百度网盘云标属百度公司注册商标。第三方客户端使用官方标识容易被误解为官方发布，
> 若提交飞牛应用中心上架，审核可能要求更换为不含官方商标的图标。自用安装无此顾虑。

**5.10 设置页保存不能覆盖应用内改动（已修）**
应用会把用户在界面里改的参数（并发数、Web 密码等）写回 `app.toml`；若设置页保存时整份重写，会把这些改动冲掉。
→ `config_callback` 改为**增量更新**：只替换 `download_dir` / `default_path`，其余键原样保留（实测 `chunk_size_mb=7` 被保留）；留空表示"保持当前值"。

**5.11 上传的真实工作方式（易误解点）**
上游的上传是**服务端路径上传**：`POST /api/v1/uploads` 收的是 `local_path`（NAS 上的文件路径），
前端由内置文件浏览器选文件，没有任何 multipart / 浏览器直传通道。
→ 因此"无法上传"通常不是缺少"上传目录"配置，而是：
① 路径白名单把选择器挡住了（同 5.8，已修）；
② 用户以为能直接选电脑上的文件（浏览器无法提供本机路径给服务端读）。
正确用法：先用 fnOS「文件管理」/SMB 把文件放进存储空间，再在应用内选择。向导与 `USAGE.md` 已写明。
上传源文件必须对运行用户（默认 admin）可读，否则报"上传源文件不存在或不是文件"。

**5.12 卸载时的"保留 / 彻底清理"（已按两个选项实现）**
`wizard/uninstall` 提供 `wizard_data_action` 单选：**保留配置文件**（默认）/ **不保留，全部删除**。
`uninstall_callback` 读取该变量：
- `keep`：仅停服务，数据原样保留，重装即可继续；
- `purge`：按卷根清空 `@appcenter / @appconf / @appdata / @apphome / @appmeta / @apptemp` 下属于本应用的目录，
  删除安装目录残留软链，回收应用专用系统账号与用户组，最后逐项校验并把残留路径写回 `TRIM_TEMP_LOGFILE`。
- 共享目录 `@appshare/{app}` **只在其中没有任何文件/软链时**才移除，已下载内容永不受影响。

实现细节（踩过的坑）：卸载回调执行时 fnOS 可能已删掉部分目录，`readlink -f` 会返回空导致找不到卷根，
因此卷根推导做了三级回退（`readlink -f` → 单层 `readlink` → 字面路径），并追加 `/vol*` 全量扫描兜底；
清理过程同时写入 `/tmp/baidupcs-uninstall.log` 便于事后核对。

实测（直接以向导变量调用 `cmd/uninstall_callback`，与 fnOS 传入方式一致）：
| 场景 | 结果 |
|---|---|
| `keep` | 7 个目录 + 系统账号全部保留 |
| `purge`，共享目录内有文件 | 6 个托管目录清空，共享目录保留并记录 "contains user files"，账号回收 |
| `purge`，共享目录为空 | 全部目录消失，共享目录一并移除，日志 `purge complete` |

> 说明：`appcenter-cli uninstall` 无法传导向导变量，会提示"请从 Web UI 卸载"，因此"卸载时选择不保留"这一交互
> 只能在 Web UI 里点选；发布前建议在 Web UI 完整走一次。

**5.13 外网访问：一份端口要同时伺候 http 与 https（已解决）**
飞牛按 `${entry.protocol || location.protocol}` 生成 iframe 地址，所以内网（http 桌面）与外网
（https 桌面/DDNS 域名）共用同一个图标，但协议不同。上游只说纯 HTTP，https 桌面里会被浏览器按
混合内容拦掉；直连 `https://域名:18888` 则连 TLS 握手都失败。
→ 采用已被同作者 `gopeed-fnos` 验证过的方案：应用改为只监听 `127.0.0.1:18889`，
`tunnel.py` 在 18888 上窥探首字节——`0x16` 走 TLS（按 ClientHello 的 SNI 从
`/usr/trim/etc/network_gateway_cert.conf` 里选证书），否则按 HTTP 透传；WebSocket 握手后双向透传。
同时监听 IPv4/IPv6（DDNS 常只有 AAAA 记录）。证书由 root 在启动前复制到 `@appdata/tls`（0600）后降权读取。

实测（局域网视角，非回环）：

| 链路 | 结果 |
|---|---|
| `http://IP:18888/` | 200 |
| `https://fnos.vinson023.eu.org:18888/` | 200，证书 `CN=fnos.vinson023.eu.org`（Let's Encrypt） |
| `wss://…/api/v1/ws` 升级 | 101 Switching Protocols |
| 内部端口 `18889` 从外部探测 | ConnectionRefused（仅回环，符合预期） |

**仍需用户侧动作**：路由器把 TCP 18888 转发到飞牛（与 5666/5667 同理）。若坚持不开放第二个端口，
唯一替代是统一网关模式，需改造上游（见 5.6），暂不提供。

---

## 六、风险清单

| 级别 | 风险 | 说明与对策 |
|---|---|---|
| ~~高~~ → 已解决 | ~~HTTPS 访问下的混合内容~~ | 2.4.1-6 起内置 HTTP/HTTPS 自适应隧道，https 桌面与外网域名均可用（见 5.13）。剩余前提：路由器需转发 TCP 18888。 |
| 中 | 无 NAS 登录态透传 | 端口入口不受 NAS 会话保护，同网段可直接访问。对策：首次强制设置 Web 密码（上游 `web_auth` 支持），或后续改网关模式。 |
| 中 | 上游"Linux 不做主动测试" | README 明示主要测试 Windows 与 Docker。本次在 Debian 12 上运行正常，长期需跟随上游回归；建议锁定版本并做冒烟（登录页 200 + WS 101 + 目录可写）。 |
| 中 | 版本跟随成本 | 上游迭代快（v2.4.1，PR #194）。`build.sh` 幂等，CI 化后单次约 5 分钟。 |
| 低 | 包体积 | 56 MB 未 strip；`strip` 后可降至约 40 MB。 |
| 低 | `appcenter-cli start` 报 10500 | 实测 CLI 打印 `Failed to launch app. error code 10500`，但 `/rpc/v1/start/check`、`/rpc/v1/start/task` 均 200，`APP_STARTED` 事件正常，`status` 为 running，服务 2 s 内可访问——属 CLI 侧假失败。以 Web UI 启停或 `status` 复核为准。 |
| 低 | 卸载表单阻断 CLI 卸载 | 存在 `wizard/uninstall` 时 `appcenter-cli uninstall` 提示"请从 Web UI 卸载"（正常产品行为，仅影响开发迭代）。开发时临时移除该文件即可。 |
| 低 | 仅 x86 包 | ARM 需另出 `platform=arm` 包并交叉编译（上游 CI 已有 `aarch64-unknown-linux-musl` 目标可借用）。 |

---

## 七、工作量与建议路线

| 阶段 | 内容 | 预估 |
|---|---|---|
| v1（已完成） | 原生包、端口入口、向导（下载目录必填）、目录选择、启停状态、卸载策略、图标 | 已完成并验证 |
| v1.1 | TLS 自适应隧道（解决 HTTPS 桌面）+ 首次强制设置 Web 密码 | 0.5–1 人日 |
| v1.2 | 多架构出包（x86 / arm64）+ CI 自动出包 | 1 人日 |
| v2（可选） | 统一网关改造：上游加 Unix Socket 监听 + 前端 base path + 网关 Header 用户识别 | 2–3 人日（建议提 PR 而非私改） |

**发布前仍需验证**（需真实百度账号）：扫码登录、单文件/文件夹下载落盘、上传与秒传、断点续传恢复、重启后自启。

---

## 八、交付物

| 路径 | 说明 |
|---|---|
| `D:\BaiduPCS\build\baidupcs_2.4.1-2_x86.fpk` | 已安装验证的成品包（20.7 MB，md5 `fcc05c92e24492ce58c652e10263bdc7`） |
| `D:\BaiduPCS\fpk\baidupcs\` | FPK 源目录（manifest / cmd / config / wizard / ui / 图标） |
| `D:\BaiduPCS\assets\vite.svg` | 与桌面图标同源的 favicon（构建时注入前端产物） |
| `D:\BaiduPCS\build.sh` | 一键构建（前端 + 容器内编译 + 打包 + 可选安装） |
| `D:\BaiduPCS\src\BaiduPCS-Rust\` | 上游源码（v2.4.1，未改动） |

测试机当前状态：应用 `baidupcs 2.4.1-5` 已安装并运行，访问 `http://<飞牛地址>:18888/`，默认下载目录 `/vol1/1000/downloads`。
