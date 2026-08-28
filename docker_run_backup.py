#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docker_run_backup.py — 从运行中的 Docker 容器还原 docker run 命令并智能备份

功能:
  1. 遍历本机 Docker 容器(默认仅运行中的, --all 包含已停止的)
  2. 通过 docker inspect 逆向还原每个容器的完整 docker run 命令
     (端口/挂载/环境变量/重启策略/网络/IP/别名/资源限制/健康检查/安全选项等)
  3. 与上次备份对比: 无变化则不产生新备份; 有变化(或首次运行)则生成
     带时间戳的快照文件, 并维护 latest.sh 与变更日志

用法:
  python3 docker_run_backup.py                 # 备份运行中的容器到 ./docker-run-backup
  python3 docker_run_backup.py --all           # 包含已停止的容器
  python3 docker_run_backup.py -o /opt/backup  # 指定备份目录
  python3 docker_run_backup.py --check         # 仅打印还原结果, 不写任何文件
  python3 docker_run_backup.py --no-env-filter # 环境变量不做镜像默认值过滤(全量输出)

依赖: python3 + docker CLI (无需 jq / docker sdk)
"""

import argparse
import datetime
import json
import os
import shlex
import socket
import subprocess
import sys

VERSION = "1.0.0"
DEFAULT_OUTDIR = "docker-run-backup"

# 运行时由 Docker 编排工具自动注入的 label 前缀, 手动 docker run 不需要
AUTO_LABEL_PREFIXES = ("com.docker.compose.", "io.k8s.", "k8s.io/")


# ---------------------------------------------------------------- 基础工具

def q(s):
    """POSIX shell 安全引用"""
    return shlex.quote(str(s))


def run_docker(args, none_on_fail=False):
    p = subprocess.run(["docker"] + args, capture_output=True, text=True)
    if p.returncode != 0:
        if none_on_fail:
            return None
        sys.stderr.write("[错误] `docker %s` 执行失败:\n%s\n"
                         % (" ".join(args), p.stderr.strip()))
        sys.exit(1)
    return p.stdout


def docker_json(args, none_on_fail=False):
    out = run_docker(args, none_on_fail=none_on_fail)
    if out is None:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def fmt_bytes(n):
    """字节数转 docker 可读单位 (整除时), 否则原样"""
    if not n or n <= 0:
        return None
    for div, suf in ((1024 ** 3, "g"), (1024 ** 2, "m"), (1024, "k")):
        if n >= div and n % div == 0:
            return "%d%s" % (n // div, suf)
    return str(n)


def fmt_dur(ns):
    """纳秒转 Go duration 字符串 (30s / 5m / 1h2m3s)"""
    if not ns:
        return None
    total = int(ns) // 1_000_000_000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    out = ""
    if h:
        out += "%dh" % h
    if m:
        out += "%dm" % m
    if s or not out:
        out += "%ds" % s
    return out


def looks_like_id(s):
    return len(s) >= 32 and all(c in "0123456789abcdef" for c in s.lower())


# ---------------------------------------------------------------- 容器/镜像信息采集

def list_containers(include_stopped=False):
    cmd = ["ps", "-q"]
    if include_stopped:
        cmd.append("-a")
    ids = [l.strip() for l in run_docker(cmd).splitlines() if l.strip()]
    result = []
    for cid in ids:
        info = docker_json(["inspect", cid])
        if info:
            result.append(info[0])
    return result


def inspect_image(ref):
    if not ref:
        return None
    out = docker_json(["image", "inspect", ref], none_on_fail=True)
    return out[0] if out else None


def get_daemon_log_driver():
    out = run_docker(["info", "--format", "{{.LoggingDriver}}"], none_on_fail=True)
    return (out or "").strip() or "json-file"


# ---------------------------------------------------------------- docker run 命令还原

def build_run_command(cinfo, img, daemon_log_driver="json-file", no_env_filter=False):
    """返回 (命令字符串, 附属connect命令列表, 注释列表)"""
    name = (cinfo.get("Name") or "?").lstrip("/")
    cfg = cinfo.get("Config") or {}
    hc = cinfo.get("HostConfig") or {}
    nets = (cinfo.get("NetworkSettings") or {}).get("Networks") or {}
    state = cinfo.get("State") or {}
    comments, connects = [], []

    img_cfg = (img or {}).get("Config") or {}

    # 镜像引用: Config.Image 保留创建时的名字; 若是纯 ID 且能找到 tag 则用 tag
    image_ref = cfg.get("Image") or ""
    if img and looks_like_id(image_ref.replace("sha256:", "")):
        tags = [t for t in (img.get("RepoTags") or []) if "<none>" not in t]
        if tags:
            comments.append("原始镜像引用为 ID %s, 已替换为当前标签 %s"
                            % (image_ref[:19] + "…", tags[0]))
            image_ref = tags[0]

    opts = ["docker run -d"]

    if cfg.get("Tty"):
        opts.append("-t")
    if cfg.get("OpenStdin"):
        opts.append("-i")
    opts.append("--name %s" % q(name))

    # ---- compose 检测 ----
    labels = cfg.get("Labels") or {}
    compose_project = labels.get("com.docker.compose.project")
    if compose_project:
        comments.append(
            "此容器由 Docker Compose 管理 (project=%s, service=%s, 工作目录=%s)。"
            "建议优先使用 compose 文件维护; 以下为等价的 docker run 命令。"
            % (compose_project,
               labels.get("com.docker.compose.service", "?"),
               labels.get("com.docker.compose.project.working_dir", "?")))

    # ---- 网络 ----
    nm = hc.get("NetworkMode") or "default"
    if nm not in ("default", "", "bridge"):
        opts.append("--network %s" % q(nm))

    # ---- 端口映射 ----
    pb = hc.get("PortBindings") or {}
    for cport, bindings in sorted(pb.items()):
        cnum, _, proto = cport.partition("/")
        for b in bindings or []:
            hip = b.get("HostIp") or ""
            hport = b.get("HostPort") or ""
            # 形态: [HostIp:]HostPort:容器端口[/协议]; 宿主端口留空=随机
            spec = ""
            if hip:
                spec += hip + ":"
            spec += hport + ":" + cnum
            if proto and proto != "tcp":
                spec += "/%s" % proto
            opts.append("-p %s" % spec)

    # ---- --expose (容器声明且未被 -p 覆盖、镜像未声明的端口) ----
    img_exposed = set((img_cfg.get("ExposedPorts") or {}).keys())
    for cport in sorted(set((cfg.get("ExposedPorts") or {}).keys()) - set(pb.keys()) - img_exposed):
        opts.append("--expose %s" % cport)

    # ---- 挂载: -v (Binds) ----
    for b in hc.get("Binds") or []:
        opts.append("-v %s" % q(b))

    # ---- 挂载: --mount ----
    for m in hc.get("Mounts") or []:
        t = m.get("Type")
        seg = "type=%s" % t
        src = m.get("Source")
        if src and t != "tmpfs":
            seg += ",source=%s" % src
        if m.get("Target"):
            seg += ",target=%s" % m["Target"]
        if m.get("ReadOnly"):
            seg += ",readonly"
        bind_opts = m.get("BindOptions") or {}
        if bind_opts.get("Propagation"):
            seg += ",bind-propagation=%s" % bind_opts["Propagation"]
        vol_opts = m.get("VolumeOptions") or {}
        if vol_opts.get("NoCopy"):
            seg += ",volume-nocopy"
        opts.append("--mount %s" % q(seg))

    # ---- tmpfs ----
    for dst, opt in (hc.get("Tmpfs") or {}).items():
        opts.append("--tmpfs %s" % q("%s:%s" % (dst, opt) if opt else dst))

    # ---- --volumes-from ----
    for vf in hc.get("VolumesFrom") or []:
        opts.append("--volumes-from %s" % q(vf))

    # ---- 环境变量 (与镜像 ENV 差异比对, 只输出运行时注入/覆盖的) ----
    img_env = {}
    for e in img_cfg.get("Env") or []:
        k, _, v = e.partition("=")
        img_env[k] = v
    for e in cfg.get("Env") or []:
        k, _, v = e.partition("=")
        if no_env_filter or img is None:
            opts.append("-e %s" % q(e))
        elif k not in img_env or img_env[k] != v:
            opts.append("-e %s" % q(e))

    # ---- labels (过滤编排工具注入 + 与镜像差异比对) ----
    img_labels = img_cfg.get("Labels") or {}
    for k, v in sorted(labels.items()):
        if any(k.startswith(p) for p in AUTO_LABEL_PREFIXES):
            continue
        if img is None or k not in img_labels or str(img_labels.get(k)) != str(v):
            opts.append("-l %s" % q("%s=%s" % (k, v) if v else k))

    # ---- 重启策略 ----
    rp = hc.get("RestartPolicy") or {}
    rpn = rp.get("Name") or "no"
    if rpn not in ("no", ""):
        retry = rp.get("MaximumRetryCount") or 0
        opts.append("--restart %s" % (rpn if rpn != "on-failure" or not retry
                                      else "on-failure:%d" % retry))

    # ---- 主机名/域名/MAC ----
    hostname = cfg.get("Hostname") or ""
    if hostname and hostname != cinfo["Id"][:12] and nm != "host":
        opts.append("--hostname %s" % q(hostname))
    if cfg.get("Domainname"):
        opts.append("--domainname %s" % q(cfg["Domainname"]))

    # ---- entrypoint ----
    centry = cfg.get("Entrypoint")
    ientry = img_cfg.get("Entrypoint") if img is not None else None
    if centry:
        if not ientry or list(centry) != list(ientry):
            if len(centry) == 1:
                opts.append("--entrypoint %s" % q(centry[0]))
            else:
                opts.append("--entrypoint %s" % q(" ".join(centry)))
                comments.append("注意: 容器 entrypoint 为多元素 %s, docker run --entrypoint 仅接受"
                                "单值, 已按空格拼接, 必要时请手工核对。" % json.dumps(centry))
    elif ientry:
        opts.append("--entrypoint ''")

    # ---- user / workdir / stop-signal / stop-timeout ----
    if cfg.get("User") and cfg["User"] != (img_cfg.get("User") or ""):
        opts.append("--user %s" % q(cfg["User"]))
    if cfg.get("WorkingDir") and cfg["WorkingDir"] != (img_cfg.get("WorkingDir") or ""):
        opts.append("-w %s" % q(cfg["WorkingDir"]))
    if cfg.get("StopSignal") and cfg["StopSignal"] != (img_cfg.get("StopSignal") or ""):
        opts.append("--stop-signal %s" % q(cfg["StopSignal"]))
    if cfg.get("StopTimeout") is not None:
        opts.append("--stop-timeout %d" % cfg["StopTimeout"])

    # ---- 健康检查 ----
    hchk = cfg.get("Healthcheck") or {}
    test = hchk.get("Test")
    if test:
        if test[0] == "NONE":
            opts.append("--no-healthcheck")
        else:
            cmd_body = " ".join(test[1:]) if len(test) > 1 else ""
            if cmd_body:
                opts.append("--health-cmd %s" % q(cmd_body))
            for key, flag in (("Interval", "--health-interval"),
                              ("Timeout", "--health-timeout"),
                              ("StartPeriod", "--health-start-period")):
                d = fmt_dur(hchk.get(key))
                if d:
                    opts.append("%s %s" % (flag, d))
            if hchk.get("Retries"):
                opts.append("--health-retries %d" % hchk["Retries"])

    # ---- 日志 ----
    log = hc.get("LogConfig") or {}
    log_type = log.get("Type") or ""
    log_opts = log.get("Config") or {}
    if log_type and log_type != daemon_log_driver or log_opts:
        if log_type and log_type != daemon_log_driver:
            opts.append("--log-driver %s" % q(log_type))
        for k, v in sorted(log_opts.items()):
            opts.append("--log-opt %s" % q("%s=%s" % (k, v)))

    # ---- 资源限制 ----
    if hc.get("Memory"):
        opts.append("--memory %s" % fmt_bytes(hc["Memory"]))
    if hc.get("MemoryReservation"):
        opts.append("--memory-reservation %s" % fmt_bytes(hc["MemoryReservation"]))
    ms = hc.get("MemorySwap")
    if ms == -1:
        opts.append("--memory-swap -1")
    elif ms and ms > 0 and ms != hc.get("Memory", 0) * 2:
        # 未指定时 docker 默认 MemorySwap = 2×Memory, 此种情况跳过
        opts.append("--memory-swap %s" % fmt_bytes(ms))
    msw = hc.get("MemorySwappiness")
    if msw is not None and msw >= 0:
        opts.append("--memory-swappiness %d" % msw)
    if hc.get("KernelMemory"):
        opts.append("--kernel-memory %s" % fmt_bytes(hc["KernelMemory"]))
    nano = hc.get("NanoCpus") or 0
    if nano:
        cpus = nano / 1_000_000_000
        opts.append("--cpus %s" % (int(cpus) if cpus == int(cpus) else cpus))
    if hc.get("CpuShares"):
        opts.append("--cpu-shares %d" % hc["CpuShares"])
    if hc.get("CpuPeriod"):
        opts.append("--cpu-period %d" % hc["CpuPeriod"])
    if hc.get("CpuQuota"):
        opts.append("--cpu-quota %d" % hc["CpuQuota"])
    if hc.get("CpusetCpus"):
        opts.append("--cpuset-cpus %s" % q(hc["CpusetCpus"]))
    if hc.get("CpusetMems"):
        opts.append("--cpuset-mems %s" % q(hc["CpusetMems"]))
    if hc.get("BlkioWeight"):
        opts.append("--blkio-weight %d" % hc["BlkioWeight"])
    if hc.get("PidsLimit"):
        opts.append("--pids-limit %d" % hc["PidsLimit"])
    shm = hc.get("ShmSize") or 0
    if shm and shm != 67108864:
        opts.append("--shm-size %s" % fmt_bytes(shm))
    if hc.get("OomScoreAdj"):
        opts.append("--oom-score-adj %d" % hc["OomScoreAdj"])
    if hc.get("OomKillDisable"):
        opts.append("--oom-kill-disable")

    # ---- 安全/权限 ----
    if hc.get("Privileged"):
        opts.append("--privileged")
    if hc.get("ReadonlyRootfs"):
        opts.append("--read-only")
    if hc.get("Init"):
        opts.append("--init")
    for c in hc.get("CapAdd") or []:
        opts.append("--cap-add %s" % q(c))
    for c in hc.get("CapDrop") or []:
        opts.append("--cap-drop %s" % q(c))
    for s in hc.get("SecurityOpt") or []:
        opts.append("--security-opt %s" % q(s))
    for s in hc.get("AppArmorProfile") or "":
        if s and s != "default":
            opts.append("--security-opt %s" % q("apparmor=%s" % s))
    for u in hc.get("Ulimits") or []:
        opts.append("--ulimit %s" % q("%s=%s:%s" % (u.get("Name"),
                                                    u.get("Soft", ""),
                                                    u.get("Hard", ""))))
    for k, v in sorted((hc.get("Sysctls") or {}).items()):
        opts.append("--sysctl %s" % q("%s=%s" % (k, v)))
    for g in hc.get("GroupAdd") or []:
        opts.append("--group-add %s" % q(g))

    # ---- 设备 / GPU ----
    for d in hc.get("Devices") or []:
        s = d.get("PathOnHost", "")
        if d.get("PathInContainer") and d["PathInContainer"] != d["PathOnHost"]:
            s += ":%s" % d["PathInContainer"]
        if d.get("CgroupPermissions") and d["CgroupPermissions"] != "rwm":
            s += ":%s" % d["CgroupPermissions"]
        opts.append("--device %s" % q(s))
    for r in hc.get("DeviceCgroupRules") or []:
        opts.append("--device-cgroup-rule %s" % q(r))
    for dr in hc.get("DeviceRequests") or []:
        seg = ""
        if dr.get("DeviceIDs"):
            seg = "device=" + ",".join(dr["DeviceIDs"])
        elif dr.get("Count", 0) and dr["Count"] > 0:
            seg = str(dr["Count"])
        elif dr.get("Count", 0) == -1:
            seg = "all"
        caps = [c for grp in (dr.get("Capabilities") or []) for c in grp if c != "gpu"]
        if caps:
            seg += ("," if seg else "") + "capabilities=" + ",".join(caps)
        if dr.get("Driver"):
            seg += ("," if seg else "") + "driver=%s" % dr["Driver"]
        if seg:
            opts.append("--gpus %s" % q(seg))

    # ---- DNS / hosts / 链接 ----
    for d in hc.get("Dns") or []:
        opts.append("--dns %s" % q(d))
    for d in hc.get("DnsSearch") or []:
        opts.append("--dns-search %s" % q(d))
    for d in hc.get("DnsOptions") or []:
        opts.append("--dns-option %s" % q(d))
    for h in hc.get("ExtraHosts") or []:
        opts.append("--add-host %s" % q(h))
    for l in hc.get("Links") or []:
        opts.append("--link %s" % q(l))

    # ---- namespace 隔离 ----
    if hc.get("PidMode") and hc["PidMode"] not in ("", "private"):
        opts.append("--pid %s" % q(hc["PidMode"]))
    if hc.get("IpcMode") and hc["IpcMode"] not in ("", "private", "shareable"):
        opts.append("--ipc %s" % q(hc["IpcMode"]))
    if hc.get("UTSMode") and hc["UTSMode"] not in ("", "private"):
        opts.append("--uts %s" % q(hc["UTSMode"]))
    if hc.get("CgroupnsMode") and hc["CgroupnsMode"] not in ("", "private"):
        opts.append("--cgroupns %s" % q(hc["CgroupnsMode"]))
    if hc.get("CgroupParent"):
        opts.append("--cgroup-parent %s" % q(hc["CgroupParent"]))
    if hc.get("Runtime") and hc["Runtime"] != "runc":
        opts.append("--runtime %s" % q(hc["Runtime"]))
    if hc.get("Isolation") and hc["Isolation"] != "default":
        opts.append("--isolation %s" % q(hc["Isolation"]))

    # ---- 自动删除 ----
    if hc.get("AutoRemove"):
        opts.append("--rm")
        comments.append("容器带 --rm, 停止后会被自动删除。")

    # ---- 静态 IP / MAC / 别名 (主网络) ----
    for net_name, net in nets.items():
        if net_name == nm or (nm in ("default", "bridge") and net_name == "bridge"):
            ipam = net.get("IPAMConfig") or {}
            mac = net.get("MacAddress") or ""
            if ipam.get("IPv4Address"):
                opts.append("--ip %s" % q(ipam["IPv4Address"]))
            if ipam.get("IPv6Address"):
                opts.append("--ip6 %s" % q(ipam["IPv6Address"]))
            aliases = [a for a in (net.get("Aliases") or []) if a]
            if aliases:
                opts.append("--network %s" % q("%s:%s" % (net_name, ",".join(aliases))))
            if ipam and mac and not mac.lower().startswith("02:42:"):
                # 02:42 开头是 docker 按 IP 自动生成的 MAC, 非用户指定
                opts.append("--mac-address %s" % q(mac))
            break

    # ---- 附加网络 → docker network connect ----
    for net_name, net in nets.items():
        if net_name == nm or (nm in ("default", "bridge") and net_name == "bridge"):
            continue
        cargs = []
        aliases = [a for a in (net.get("Aliases") or []) if a]
        for a in aliases:
            cargs.append("--alias %s" % q(a))
        ipam = net.get("IPAMConfig") or {}
        if ipam.get("IPv4Address"):
            cargs.append("--ip %s" % q(ipam["IPv4Address"]))
        if ipam.get("IPv6Address"):
            cargs.append("--ip6 %s" % q(ipam["IPv6Address"]))
        connects.append("docker network connect %s %s %s"
                        % (" ".join(cargs), q(net_name), q(name)))

    # ---- 镜像 + CMD (合并为最后一行) ----
    cmd = cfg.get("Cmd")
    tail = q(image_ref)
    if cmd:
        tail += " " + " ".join(q(x) for x in cmd)
    opts.append(tail)

    # 多行格式
    lines = []
    for o in opts[:-1]:
        lines.append(("  " if lines else "") + o + " \\")
    lines.append("  " + opts[-1])
    return "\n".join(lines), connects, comments, image_ref


# ---------------------------------------------------------------- 备份文件生成

def render_backup(containers, args, daemon_log_driver, now, stats):
    """生成备份脚本全文"""
    body = []      # 每容器一个 section
    total = len(containers)
    running = sum(1 for c in containers
                  if (c.get("State") or {}).get("Running"))
    for c in containers:
        name = (c.get("Name") or "?").lstrip("/")
        state = c.get("State") or {}
        status = "running" if state.get("Running") else (
            "paused" if state.get("Paused") else "exited(%s)" % state.get("ExitCode", "?"))
        img = inspect_image((c.get("Config") or {}).get("Image"))
        run_cmd, connects, comments, image_ref = build_run_command(
            c, img, daemon_log_driver, args.no_env_filter)

        body.append("#" + "-" * 66)
        body.append("# 容器: %-30s 状态: %s" % (name, status))
        body.append("# 镜像: %s" % image_ref)
        if img:
            digests = [d for d in (img.get("RepoDigests") or [])
                       if "@" in d and "<none>" not in d]
            if digests:
                body.append("# 镜像摘要(精确恢复可用): %s" % digests[0])
                if "@" not in image_ref:
                    body.append("#   提示: 镜像引用为浮动标签, 若需精确还原当前版本,")
                    body.append("#         可将命令中的镜像名替换为上面的 摘要 引用")
        for cm in comments:
            body.append("# 说明: %s" % cm)
        body.append("#" + "-" * 66)
        body.append(run_cmd)
        if connects:
            body.append("")
            for cn in connects:
                body.append("# 需在容器启动后执行:")
                body.append(cn)
        body.append("")

    header = []
    header.append("#!/bin/bash")
    header.append("# " + "=" * 66)
    header.append("# Docker 容器 docker run 命令备份")
    header.append("# 生成时间 : %s" % now.strftime("%Y-%m-%d %H:%M:%S %z"))
    header.append("# 主机     : %s" % socket.gethostname())
    header.append("# 容器数量 : %d (运行中 %d)%s"
                  % (total, running, "" if running == total else ", 其余为停止状态"))
    header.append("# 生成工具 : docker_run_backup.py v%s" % VERSION)
    header.append("# " + "=" * 66)
    header.append("#")
    header.append("# 使用说明:")
    header.append("#   1. 执行前确保镜像已存在(必要时 docker pull)、宿主机挂载目录已创建")
    header.append("#   2. 同名容器已存在会冲突, 可先执行: docker rm -f <容器名>")
    header.append("#   3. 命令默认后台运行(-d); 需交互式请自行替换为 -dit")
    header.append("#   4. 带 'docker network connect' 的行需在容器启动后另行执行")
    header.append("#")
    header.append("set -e")
    header.append("")

    stats.update(total=total, running=running)
    return "\n".join(header) + "\n" + "\n".join(body)


def strip_volatile(text):
    """去掉时间戳等易变行, 用于内容对比"""
    return "\n".join(l for l in text.splitlines()
                     if not l.startswith("# 生成时间"))


def main():
    ap = argparse.ArgumentParser(
        description="从 Docker 容器逆向还原 docker run 命令并智能备份")
    ap.add_argument("-o", "--output", default=DEFAULT_OUTDIR,
                    help="备份目录 (默认 ./%s)" % DEFAULT_OUTDIR)
    ap.add_argument("--running", action="store_true",
                    help="仅包含运行中的容器 (默认包含已停止的)")
    ap.add_argument("--check", action="store_true",
                    help="仅打印还原结果, 不写入备份")
    ap.add_argument("--no-env-filter", action="store_true",
                    help="环境变量全量输出(不做镜像默认值过滤)")
    ap.add_argument("--keep", type=int, default=0, metavar="N",
                    help="仅保留最近 N 份历史快照, 0=全部保留")
    args = ap.parse_args()

    if not run_docker(["version", "--format", "{{.Server.Version}}"],
                      none_on_fail=True):
        sys.stderr.write("[错误] 无法连接 Docker 守护进程\n")
        sys.exit(1)

    containers = list_containers(include_stopped=not args.running)
    daemon_log_driver = get_daemon_log_driver()
    now = datetime.datetime.now().astimezone()
    stats = {}
    content = render_backup(containers, args, daemon_log_driver, now, stats)

    if args.check:
        print(content)
        print("# [check 模式] 共 %d 个容器, 未写入任何文件" % stats.get("total", 0))
        return

    outdir = os.path.abspath(args.output)
    os.makedirs(outdir, exist_ok=True)
    latest_path = os.path.join(outdir, "latest.sh")
    log_path = os.path.join(outdir, "backup-history.log")

    if stats.get("total", 0) == 0:
        print("[提示] 当前没有%s容器, 不生成备份。"
              % ("运行中的" if args.running else "任何"))
        return

    old = None
    if os.path.exists(latest_path):
        with open(latest_path, "r", encoding="utf-8") as f:
            old = f.read()

    if old is not None and strip_volatile(old) == strip_volatile(content):
        print("[OK] %d 个容器, 与最近一次备份(%s)一致, 无变化, 未生成新备份。"
              % (stats["total"],
                 datetime.datetime.fromtimestamp(
                     os.path.getmtime(latest_path)).strftime("%Y-%m-%d %H:%M:%S")))
        return

    ts = now.strftime("%Y%m%d-%H%M%S")
    snap_path = os.path.join(outdir, "docker-run-backup-%s.sh" % ts)
    with open(snap_path, "w", encoding="utf-8") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")
    os.chmod(snap_path, 0o755)
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(content)
        if not content.endswith("\n"):
            f.write("\n")
    os.chmod(latest_path, 0o755)

    reason = "首次备份" if old is None else "检测到变化"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("%s\t%s\t容器数=%d\t文件=%s\n"
                % (now.strftime("%Y-%m-%d %H:%M:%S"), reason,
                   stats["total"], os.path.basename(snap_path)))

    # 历史快照数量控制
    if args.keep and args.keep > 0:
        snaps = sorted(s for s in os.listdir(outdir)
                       if s.startswith("docker-run-backup-") and s.endswith(".sh"))
        for s in snaps[:-args.keep] if len(snaps) > args.keep else []:
            os.remove(os.path.join(outdir, s))

    print("[备份] %s (%d 个容器, 运行中 %d)" % (snap_path, stats["total"], stats["running"]))
    if old is not None:
        print("[对比] 本次与上次内容存在差异, 已保存新快照并更新 latest.sh")
    print("      最新备份始终同步于: %s" % latest_path)
    print("      变更日志: %s" % log_path)


if __name__ == "__main__":
    main()
