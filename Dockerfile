# Docker 镜像：docker-run-backup 工具容器化版本
# 用途：在任意宿主机上挂载 /var/run/docker.sock 即可备份/还原该宿主机的容器配置，
#       并通过内置 Web 浏览器查看/对比/下载备份文件，每日 00:00 自动检查并备份。
#
# 基于 Docker 官方最小 CLI 镜像（自带 /usr/local/bin/docker 客户端），
# 再装入 python3 运行备份脚本与 Web 服务。容器内不运行 dockerd，完全复用宿主机的 docker.sock。
FROM docker:cli

# 安装 python3（脚本仅依赖标准库，无需额外 pip 包）
RUN apk add --no-cache python3

WORKDIR /app

COPY docker_run_backup.py /app/docker_run_backup.py
COPY app.py /app/app.py
COPY index.html /app/index.html
RUN chmod +x /app/docker_run_backup.py /app/app.py

ENV BACKUP_DIR=/backup \
    PORT=8080 \
    HOST=0.0.0.0

EXPOSE 8080

# 默认启动 Web 浏览器 + 每日 00:00 自动备份调度
# 如需直接执行 CLI（如仅预览还原结果），可覆盖命令:
#   docker run ... --rm totootao/docker-backup /app/docker_run_backup.py --check
ENTRYPOINT ["python3"]
CMD ["/app/app.py"]
