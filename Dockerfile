# Docker 镜像：docker-run-backup 工具容器化版本
# 用途：在任意宿主机上挂载 /var/run/docker.sock 即可备份/还原该宿主机的容器配置
FROM python:3.11-slim

# 安装 docker CLI（容器内通过宿主机的 docker.sock 操作宿主机 Docker）
# GitHub Actions runner 在公网，apt 源可直连；无需走镜像加速器
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY docker_run_backup.py /app/docker_run_backup.py
RUN chmod +x /app/docker_run_backup.py

# 默认行为：还原并输出当前宿主机的 docker run 命令（不写文件，--check 等效）
# 用法示例见 README
ENTRYPOINT ["python3", "/app/docker_run_backup.py"]
