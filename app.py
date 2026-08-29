#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Docker 备份 Web 浏览器 + 定时调度器 (标准库实现, 零外部依赖)

功能:
  1. 提供 Web 页面浏览备份目录下的所有文件 (快照 / latest.sh / 变更日志)
     - 查看文件内容 / 下载 / 两个快照间差异对比 / 变更日志
  2. 内置后台调度线程: 每天 00:00 自动执行一次备份 (有变化才落盘)
  3. 提供手动"立即备份"接口

环境变量:
  BACKUP_DIR   备份目录 (默认 /backup)
  PORT         Web 端口 (默认 8080)
  HOST         监听地址 (默认 0.0.0.0)
  SCHEDULER    设为 off 关闭每日定时调度
  RUN_ON_START 设为 0 关闭启动时首次播种备份 (默认开启)

用法:
  python3 app.py            # 启动 Web + 调度
  SCHEDULER=off python3 app.py   # 只启动 Web, 不做定时调度
"""

import datetime
import difflib
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# docker_run_backup.py 与本文件同目录
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import docker_run_backup as drb  # noqa: E402

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backup")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")

# 调度/运行态 (进程内共享)
state = {
    "next_run": None,      # datetime: 下次定时备份时间
    "last_run": None,      # str: 上次备份时间
    "last_result": None,   # dict: 上次备份结果
    "running": False,      # bool: 是否正在执行备份
}


# ---------------------------------------------------------------- 备份操作

def do_backup(running_only=False, keep=0):
    """执行一次备份, 更新 state, 返回结果字典"""
    state["running"] = True
    try:
        res = drb.run_backup(backup_dir=BACKUP_DIR, running_only=running_only,
                             keep=keep)
    except Exception as exc:  # 兜底, 不让调度线程崩溃
        res = {"ok": False, "reason": "备份异常: %s" % exc,
               "changed": False, "snapshot": None,
               "total": 0, "running": 0}
    state["running"] = False
    state["last_run"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_result"] = res
    return res


def compute_next_run():
    """下一个 00:00 (若今天 00:00 已过则取明天)"""
    now = datetime.datetime.now()
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += datetime.timedelta(days=1)
    return nxt


def scheduler_loop():
    """后台线程: 每天 00:00 执行一次备份"""
    while True:
        nxt = state["next_run"] or compute_next_run()
        state["next_run"] = nxt
        now = datetime.datetime.now()
        secs = (nxt - now).total_seconds()
        if secs > 0:
            time.sleep(min(secs, 30))  # 周期性唤醒, 防止错过
            continue
        try:
            do_backup(running_only=False)
        except Exception as exc:
            state["last_result"] = {"ok": False,
                                    "reason": "调度备份失败: %s" % exc}
        state["next_run"] = compute_next_run()


# ---------------------------------------------------------------- 文件工具

def safe_path(name):
    """将文件名安全解析到备份目录内, 禁止路径穿越; 返回绝对路径或 None"""
    base = os.path.abspath(BACKUP_DIR)
    fp = os.path.abspath(os.path.join(base, name))
    if fp != base and not fp.startswith(base + os.sep):
        return None
    if not os.path.isfile(fp):
        return None
    return fp


def list_files():
    out = []
    if not os.path.isdir(BACKUP_DIR):
        return out
    for fn in sorted(os.listdir(BACKUP_DIR)):
        fp = os.path.join(BACKUP_DIR, fn)
        if not os.path.isfile(fp):
            continue
        if fn == "latest.sh":
            ftype = "latest"
        elif fn.startswith("docker-run-backup-") and fn.endswith(".sh"):
            ftype = "snapshot"
        elif fn == "backup-history.log":
            ftype = "log"
        else:
            ftype = "other"
        st = os.stat(fp)
        out.append({
            "name": fn,
            "type": ftype,
            "size": st.st_size,
            "mtime": datetime.datetime.fromtimestamp(st.st_mtime)
            .strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


def file_content(name):
    fp = safe_path(name)
    if fp is None:
        return None
    with open(fp, "r", encoding="utf-8") as f:
        return f.read()


def diff_files(a, b):
    ca, cb = file_content(a), file_content(b)
    if ca is None or cb is None:
        return None
    return list(difflib.unified_diff(
        ca.splitlines(), cb.splitlines(),
        fromfile=a, tofile=b, lineterm=""))


def parse_containers(text):
    """从备份脚本文本中拆分为每个容器的 docker run 命令。

    返回 list[dict]: {idx, name, status, image, multi, single}
      multi  = 多行友好格式 (带续行符 \\)
      single = 单行紧凑格式 (去掉续行符, 可直接粘贴执行)
    """
    blocks = []
    cur = None
    for line in (text or "").split("\n"):
        if line.startswith("# 容器:"):
            rest = line[len("# 容器:"):].strip()
            name = rest.split("状态:")[0].strip()
            status = ""
            if "状态:" in rest:
                status = rest.split("状态:", 1)[1].strip()
            cur = {"name": name, "status": status, "meta": [], "cmd": []}
            blocks.append(cur)
        elif cur is not None:
            if line.startswith("#"):
                cur["meta"].append(line)  # 容器内注释
            else:
                cur["cmd"].append(line)

    out = []
    for idx, b in enumerate(blocks):
        multi = "\n".join(b["cmd"]).strip("\n")
        # 单行: 去掉缩进与续行符 \\, 用空格连接为一个可粘贴命令
        parts = []
        for ln in b["cmd"]:
            ln = ln.strip()
            if not ln:
                continue
            if ln.endswith("\\"):
                ln = ln[:-1].rstrip()  # 去掉反斜杠及其前的空白
            parts.append(ln)
        single = " ".join(parts)

        image = ""
        for m in b["meta"]:
            if m.startswith("# 镜像:"):
                image = m[len("# 镜像:"):].strip()
        out.append({
            "idx": idx,
            "name": b["name"],
            "status": b["status"],
            "image": image,
            "multi": multi,
            "single": single,
        })
    return out


def api_state():
    docker_ok = bool(drbb_version())
    nxt = state["next_run"]
    return {
        "backup_dir": BACKUP_DIR,
        "docker_ok": docker_ok,
        "next_run": nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else None,
        "last_run": state["last_run"],
        "running": state["running"],
        "last_result": state["last_result"],
        "files": list_files(),
    }


def drbb_version():
    return drb.run_docker(["version", "--format", "{{.Server.Version}}"],
                          none_on_fail=True)


# ---------------------------------------------------------------- HTTP 处理

class Handler(BaseHTTPRequestHandler):
    server_version = "DockerBackupWeb/1.0"

    def _send_json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text, ctype="text/plain; charset=utf-8", code=200):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, fp, name):
        with open(fp, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % name)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = urlparse(self.path)
        path, qs = p.path, parse_qs(p.query)
        if path in ("/", "/index.html"):
            self._send_text(INDEX_HTML, ctype="text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._send_json(api_state())
            return
        if path == "/api/files":
            self._send_json({"dir": BACKUP_DIR, "files": list_files()})
            return
        if path == "/api/history":
            self._send_json({"content": file_content("backup-history.log") or ""})
            return
        if path == "/api/file":
            name = qs.get("name", [""])[0]
            c = file_content(name)
            if c is None:
                self._send_json({"error": "文件不存在或越权访问"}, 404)
                return
            self._send_json({"name":  name, "content": c})
            return
        if path == "/api/containers":
            name = qs.get("name", ["latest.sh"])[0]
            c = file_content(name)
            if c is None:
                self._send_json({"error": "文件不存在或越权访问"}, 404)
                return
            self._send_json({"name": name,
                             "containers": parse_containers(c)})
            return
        if path == "/api/diff":
            a, b = qs.get("a", [""])[0], qs.get("b", [""])[0]
            d = diff_files(a, b)
            if d is None:
                self._send_json({"error": "文件不存在"}, 404)
                return
            self._send_json({"a": a, "b": b, "diff": d})
            return
        if path == "/api/download":
            name = qs.get("name", [""])[0]
            fp = safe_path(name)
            if fp is None:
                self._send_json({"error": "文件不存在或越权访问"}, 404)
                return
            self._send_file(fp, name)
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        p = urlparse(self.path)
        if p.path == "/api/backup":
            qs = parse_qs(p.query)
            running_only = qs.get("running_only", ["0"])[0] == "1"
            try:
                keep = int(qs.get("keep", ["0"])[0])
            except ValueError:
                keep = 0
            try:
                res = do_backup(running_only=running_only, keep=keep)
            except Exception as exc:
                res = {"ok": False, "reason": str(exc)}
            self._send_json(res)
            return
        self._send_json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass  # 静默访问日志


# ---------------------------------------------------------------- 启动

def main():
    # 载入 index.html (与本文件同目录)
    global INDEX_HTML
    try:
        with open(os.path.join(_HERE, "index.html"), encoding="utf-8") as f:
            INDEX_HTML = f.read()
    except FileNotFoundError:
        INDEX_HTML = "<h1>index.html 未找到, 请确认其与 app.py 同目录</h1>"

    # 启动时播种一次备份 (若关闭定时, 仍可由手动触发)
    if os.environ.get("RUN_ON_START", "1") != "0":
        try:
            do_backup(running_only=False)
        except Exception as exc:
            sys.stderr.write("[警告] 启动备份失败: %s\n" % exc)

    # 启动每日 00:00 调度线程
    if os.environ.get("SCHEDULER", "on") != "off":
        state["next_run"] = compute_next_run()
        threading.Thread(target=scheduler_loop, daemon=True).start()
        print("[调度] 每日 00:00 自动备份已启用, 下次执行: %s"
              % state["next_run"].strftime("%Y-%m-%d %H:%M:%S"))

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("[Web] Docker 备份浏览器已启动: http://%s:%d" % (HOST, PORT))
    print("[Web] 备份目录: %s" % BACKUP_DIR)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[Web] 已停止")


INDEX_HTML = ""


if __name__ == "__main__":
    main()
