# BaiduPCS-Rust for fnOS (FPK)

把 [BaiduPCS-Rust](https://github.com/komorebiCarry/BaiduPCS-Rust) 2.4.1 封装为飞牛 fnOS 原生应用包。

- 上游项目 / 原作者：komorebiCarry（Apache-2.0）
- fnOS 封装、打包与维护：vinson023

## 目录结构

```
baidupcs/
├── app/
│   ├── bin/baidu-netdisk-rust          # Rust 后端（x86_64，动态链接 glibc）
│   ├── bin/frontend/dist/              # 前端构建产物（由 exe_dir 探测命中）
│   └── ui/{config,images}              # 桌面入口与图标
├── cmd/{main,install_*,upgrade_*,...}  # 生命周期脚本（逻辑集中在 cmd/main）
├── config/{privilege,resource}         # 运行身份 / 共享目录声明
├── wizard/{install,config,uninstall}   # 安装 / 设置 / 卸载表单
├── manifest
├── ICON.PNG / ICON_256.PNG
└── LICENSE
```

## 运行时布局

| 内容 | 位置 |
|---|---|
| 程序、前端 | `${TRIM_APPDEST}/bin` |
| 配置 app.toml | `${TRIM_PKGVAR}/config/app.toml`（并软链到 `${TRIM_PKGETC}/app.toml`） |
| SQLite / 备份 / 快照 | `${TRIM_PKGVAR}/config/` |
| WAL | `${TRIM_PKGVAR}/wal` |
| 日志 | `${TRIM_PKGVAR}/logs` 与 `${TRIM_PKGVAR}/app.log` |
| 默认下载目录 | `${TRIM_DATA_SHARE_PATHS}` → `/volN/@appshare/baidupcs/downloads` |

服务以 `setpriv` 降到 NAS 用户（默认 admin）运行；CWD 设为 `${TRIM_PKGVAR}`，因为上游所有相对路径都相对进程工作目录解析。

## 上传说明

本应用读取的是飞牛存储内的文件，没有浏览器直传通道：先把文件放进存储空间
（文件管理 / SMB / 共享目录），再在应用内「上传」里选择。详见 `USAGE.md`。

## 构建

见仓库根目录 `build.sh`。
