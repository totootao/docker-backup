# Docker 镜像：docker-run-backup 工具容器化版本
# 用途：在任意宿主机上挂载 /var/run/docker.sock 即可备份/还原该宿主机的容器配置
#
# 基于 Docker 官方最小 CLI 镜像（自带 /usr/local/bin/docker 客户端），
# 再装入 python3 运行备份脚本。容器内不运行 dockerd，完全复用宿主机的 docker.sock。
FROM docker:cli

# 安装 python3（脚本仅依赖标准库）
RUN apk add --no-cache python3

WORKDIR /app

COPY docker_run_backup.py /app/docker_run_backup.py
RUN chmod +x /app/docker_run_backup.py

# 默认行为：还原并输出当前宿主机的 docker run 命令（不写文件，--check 等效）
# 用法示例见 README
ENTRYPOINT ["python3", "/app/docker_run_backup.py"]
