# Docker / Linux 部署 Spike 结论（2.6）

> 结论先行：**Linux 部署当前被 fxcorepy Python 绑定缺失阻塞**，其余全部就绪。
> 阻塞解除路径见下「三条出路」。

## 已验证事实

| # | 事实 | 来源 |
|---|---|---|
| 1 | forexconnect 1.6.43 PyPI：py3.10 仅 macOS-arm64 wheel，**无 sdist**；py3.5-3.7 有 manylinux1/win wheel（ABI 锁定旧 Python，无法承载本项目 3.10 语法） | pypi.org/pypi/forexconnect/1.6.43/json（2026-09-09 核实） |
| 2 | FXCM 官方 **Linux-x86_64 SDK 存在**（ForexConnectAPI-1.6.5-Linux-x86_64.tar.gz，含 bin/*.so、include/ 头文件）——C++ 层在 Linux 完整可用 | github.com/fxcm/ForexConnectAPI 仓库 |
| 3 | SDK 内**不含 Python 绑定**（bin/include/lib 三目录，无 python/fxcorepy 条目） | tar 清单核验 |
| 4 | fxcorepy（Boost.Python 编译扩展）绑定源码未公开；PyPI wheel 由 FXCM 私有构建 | 综合调研（librarian 会话） |
| 5 | 本项目应用代码（fxcm_api/ 全部 + static/）为纯 Python + pathlib + SQLite + FastAPI，**100% 跨平台**；唯一平台专属物是 `scripts/patch_forexconnect_mac.sh`（otool/codesign，Linux 无需 dylib 修补，仅需正确 rpath） | 本仓库代码审查 |

## 三条出路（按推荐排序）

### A. 向 FXCM 索取（零成本，首选）
联系 FXCM API 支持（devel@fxcm.com），说明已在使用 forexconnect Python API，
索取 **Linux x86_64 / py3.10 的 forexconnect wheel** 或 fxcorepy 绑定构建脚本。
SDK 的 C++ 层他们已公开，补一个绑定构建产物对其是低成本动作。

### B. 基于 SDK 自写 ctypes 绑定（3-5 天工程量）
SDK 提供完整 `.so`（libForexConnect.so 等 11 个）+ 全量头文件。
`docs/forexconnect_api_reference.md` 已沉淀的接口知识可直接映射到 ctypes 签名。
产出后永久解决 Linux（含 Docker/服务器 7×24 部署）。

### C. 降级 Python 3.7 复用 manylinux wheel（不推荐）
需将全代码库降至 3.7 语法（放弃 `X | Y`、match 等），数据模块/daemon 改动面大，
且 3.7 已 EOL。仅作为临时过渡选项。

## Dockerfile（结构就绪）

`Dockerfile` 已按「Python 3.10 + Linux SDK」结构写好，唯一缺口是 fxcorepy Linux
二进制的获取步骤（TODO 标注）。出路 A/B 落地后即可 `docker build` 直接使用。

## macOS 与 Linux 的差异清单（出路落地后）
- 无需 patch 脚本（Linux .so 用 RUNPATH，SDK tar 包内已带正确布局）
- daemon 从仓库根目录启动的约定不变（static/、data/、History/ 相对路径）
- 建议生产形态：docker compose + 本监督脚本同款退避策略由容器重启策略替代
