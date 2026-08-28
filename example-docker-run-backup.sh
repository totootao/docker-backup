#!/bin/bash
# ==================================================================
# Docker 容器 docker run 命令备份
# 生成时间 : 2026-08-28 17:15:16 +0800
# 主机     : 83a87b4ec946
# 容器数量 : 6 (运行中 5), 其余为停止状态
# 生成工具 : docker_run_backup.py v1.0.0
# ==================================================================
#
# 使用说明:
#   1. 执行前确保镜像已存在(必要时 docker pull)、宿主机挂载目录已创建
#   2. 同名容器已存在会冲突, 可先执行: docker rm -f <容器名>
#   3. 命令默认后台运行(-d); 需交互式请自行替换为 -dit
#   4. 带 'docker network connect' 的行需在容器启动后另行执行
#
set -e

#------------------------------------------------------------------
# 容器: test-exited                    状态: exited(0)
# 镜像: busybox
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
#------------------------------------------------------------------
docker run -d \
  --name test-exited \
  -p 7777:7777 \
  --oom-score-adj 1000 \
  busybox sleep 1

#------------------------------------------------------------------
# 容器: test-multi                     状态: running
# 镜像: busybox:latest
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
#------------------------------------------------------------------
docker run -d \
  --name test-multi \
  --network testnet \
  -e FOO=bar \
  --oom-score-adj 1000 \
  --ip 172.20.0.20 \
  --network testnet:m1 \
  busybox:latest sleep infinity

# 需在容器启动后执行:
docker network connect --alias m2 --ip 172.21.0.99 testnet2 test-multi

#------------------------------------------------------------------
# 容器: test-compose-web               状态: running
# 镜像: busybox:latest
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
# 说明: 此容器由 Docker Compose 管理 (project=demo, service=web, 工作目录=/opt/demo)。建议优先使用 compose 文件维护; 以下为等价的 docker run 命令。
#------------------------------------------------------------------
docker run -d \
  --name test-compose-web \
  -e MYSQL_HOST=db \
  --restart on-failure:5 \
  --oom-score-adj 1000 \
  busybox:latest sleep infinity

#------------------------------------------------------------------
# 容器: test-host                      状态: running
# 镜像: busybox:latest
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
#------------------------------------------------------------------
docker run -d \
  --name test-host \
  --network host \
  --oom-score-adj 1000 \
  --security-opt label=disable \
  --pid host \
  --ipc host \
  busybox:latest sleep infinity

#------------------------------------------------------------------
# 容器: test-netapp                    状态: running
# 镜像: busybox:latest
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
#------------------------------------------------------------------
docker run -d \
  --name test-netapp \
  --network testnet \
  --restart always \
  --oom-score-adj 1000 \
  --ip 172.20.0.10 \
  --network testnet:web1 \
  busybox:latest sleep infinity

#------------------------------------------------------------------
# 容器: test-web                       状态: running
# 镜像: busybox:latest
# 镜像摘要(精确恢复可用): busybox@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,
#         可将命令中的镜像名替换为上面的 摘要 引用
#------------------------------------------------------------------
docker run -d \
  --name test-web \
  -p 53:5353/udp \
  -p 127.0.0.1:8080:80 \
  -v /tmp/testdata:/data:rw \
  -v testvol:/var/lib/data \
  --mount type=bind,source=/tmp/testdata,target=/mnt/bind,readonly \
  --tmpfs /run:rw,size=32m \
  -e APP_ENV=prod \
  -e TZ=Asia/Shanghai \
  -l owner=ops \
  -l team=infra \
  --restart always \
  --hostname testweb \
  --user 1000:1000 \
  -w /data \
  --health-cmd true \
  --health-interval 30s \
  --health-retries 3 \
  --log-opt max-size=10m \
  --memory 256m \
  --memory-swap 256m \
  --cpus 0.5 \
  --shm-size 128m \
  --oom-score-adj 1000 \
  --cap-add NET_ADMIN \
  busybox:latest sleep infinity
