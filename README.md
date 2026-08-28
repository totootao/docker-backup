# Docker Run 命令备份工具

从服务器上运行中的 Docker 容器**逆向还原完整的 `docker run` 命令**，并在配置发生变化时自动生成带时间戳的备份快照。

## 快速开始

把 `docker_run_backup.py` 拷贝到服务器任意目录，执行：

```bash
python3 docker_run_backup.py
```

默认备份运行中的容器到 `./docker-run-backup/` 目录。**无变化时不产生新文件，有变化才生成新快照**。

## 常用参数

| 命令 | 说明 |
|---|---|
| `python3 docker_run_backup.py` | 备份运行中的容器，有变化才生成新快照 |
| `python3 docker_run_backup.py --all` | 包含已停止的容器 |
| `python3 docker_run_backup.py -o /opt/backup` | 指定备份目录 |
| `python3 docker_run_backup.py --check` | 仅打印还原结果，不写任何文件 |
| `python3 docker_run_backup.py --keep 20` | 仅保留最近 20 份历史快照 |
| `python3 docker_run_backup.py --no-env-filter` | 环境变量全量输出（默认会过滤掉镜像自带的 ENV） |

依赖：仅 `python3` + `docker` CLI，无需安装任何第三方库。

## 备份目录结构

```
docker-run-backup/
├── docker-run-backup-20260828-171212.sh   # 历史快照（每次变化一份）
├── docker-run-backup-20260828-171219.sh
├── latest.sh                              # 始终等于最新内容
└── backup-history.log                     # 变更日志（时间/原因/容器数/文件）
```

## 还原能力覆盖

- **基础**：`--name`、`--restart`（含 `on-failure:N`）、`-d`、`--rm`、`-t`/`-i`
- **网络**：`--network`（bridge/host/none/custom/container:xx）、静态 `--ip`/`--ip6`、`--network-alias`（还原为 `--network <net>:<alias>`）、附加网络自动生成 `docker network connect` 命令、`--mac-address`
- **端口**：`-p [HostIp:]HostPort:容器端口[/udp|sctp]`、`--expose`
- **存储**：`-v`（bind/named volume 及读写模式）、`--mount`（bind/volume，含 readonly、propagation）、`--tmpfs`、`--volumes-from`
- **配置**：`-e`（与镜像 ENV 差异比对，仅输出运行时注入的）、`-l`（过滤 compose 自动注入的 label）、`--hostname`、`--user`、`-w`、`--entrypoint`、`--stop-signal`、`--stop-timeout`
- **健康检查**：`--health-cmd/interval/timeout/retries/start-period`、`--no-healthcheck`
- **资源**：`--memory`、`--memory-swap`、`--memory-reservation`、`--cpus`、`--cpu-shares/quota/period`、`--cpuset-cpus/mems`、`--shm-size`、`--pids-limit`、`--blkio-weight`、`--ulimit`、`--oom-score-adj`
- **安全/权限**：`--privileged`、`--cap-add/drop`、`--security-opt`、`--sysctl`、`--device`、`--gpus`、`--read-only`、`--init`、`--pid/--ipc/--uts/--cgroupns`、`--group-add`
- **其他**：`--dns*`、`--add-host`、`--log-driver/--log-opt`、`--runtime`、`--isolation`、`--link`

## 智能细节

- **环境变量差异比对**：只输出 `-e` 注入/覆盖的变量，镜像自带的 `ENV`（如 `PATH`）不输出
- **Compose 容器识别**：检测到 `com.docker.compose.*` 标签时，会注释提示项目名/服务名/工作目录，建议优先用 compose 文件维护（等价的 run 命令仍会给出，且过滤掉 compose 自动注入的 label）
- **镜像摘要**：每个容器注释中记录镜像 digest（如 `busybox@sha256:...`），浮动标签（`latest`/缺省 tag）可在恢复时替换为摘要引用，确保精确还原
- **随机 MAC 过滤**：docker 按 IP 自动生成的 `02:42:` 开头 MAC 不会误还原为 `--mac-address`
- **MemorySwap 语义**：区分默认行为（=2×memory，跳过）与显式设置（如禁用 swap 时 =memory，保留 `--memory-swap`）
- **忽略运行时注入值**：hostname 默认等于容器 ID 前缀时不输出 `--hostname`

## 恢复方法

```bash
# 1. 查看最新备份
cat docker-run-backup/latest.sh

# 2. 恢复单个容器（先删旧容器）
docker rm -f <容器名>
bash -c "$(sed -n '/<容器名>/,/^$/p' docker-run-backup/latest.sh | grep '^docker run' -A20)"

# 或直接手动复制备份文件中对应的 docker run 命令执行
```

> 注意：备份文件头部 `set -e` 意味着整脚本执行时遇错即停；通常建议按容器单独取命令执行，避免一次性全量重建。

## 已验证

在本沙箱用 6 类复杂容器（多端口/协议映射、bind+named volume+`--mount`、tmpfs、自定义网络静态 IP+别名、多网络 connect、host 网络模式、compose 模拟容器、健康检查、资源限制、cap-add 等）测试：

- ✅ 用还原命令**实际重建容器**后与原容器 `docker inspect` 逐字段对比**完全等价**
- ✅ 重复执行时正确识别「无变化」不产生新备份
- ✅ `docker update` 修改重启策略后正确检测变化并生成新快照，diff 精确反映差异
- ✅ `--all` 正确包含 exited 容器并标注退出码

## 建议配合 crontab 定期备份

```bash
# 每小时检查一次, 有变化才落盘; 保留最近 30 份
0 * * * * /usr/bin/python3 /opt/docker_run_backup.py -o /opt/docker-run-backup --keep 30 >> /var/log/docker-run-backup.log 2>&1
```

## Docker 镜像（容器化运行）

工具已打包为 Docker 镜像，由本仓库的 GitHub Actions 工作流（`.github/workflows/docker-image.yml`）在 push 到 `master` 时**自动构建并推送**到 Docker Hub：

- `totootao/docker-backup:latest`
- `totootao/docker-backup:<commit-sha>`

拉取与运行（挂载宿主机 `docker.sock` 即可备份该宿主机的容器配置）：

```bash
docker pull totootao/docker-backup:latest

# 仅预览还原结果（等价于 --check）
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock totootao/docker-backup:latest --check

# 备份到当前目录的 docker-run-backup/（有变化才生成新快照）
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$PWD":/backup totootao/docker-backup:latest -o /backup/docker-run-backup
```

> 容器内通过宿主机的 `docker.sock` 操作 Docker，因此无需在容器内运行 dockerd。
> 镜像自动构建依赖仓库 Secrets：`DOCKERHUB_USERNAME` 与 `DOCKERHUB_PASSWORD`，需在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中配置后，工作流才能登录并推送。
