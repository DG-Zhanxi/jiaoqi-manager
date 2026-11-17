#!/usr/bin/env python3
"""
交期管家部署管理工具 (v2 — 修正代码审查发现的 7 个问题).

支持 preflight（只读检查）、backup（SQLite + 代码备份）、deploy（安全重启 + 健康检查 + 故障恢复）.
安全约束: 固定根目录 /root/JiaoqiManager, 端口 8788, PID 验证后才停止, 绝不 pkill,
绝不影响 /root/RenCheng, 日志无凭据, 部署锁防并发, 不覆盖运行中数据库.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ─── 常量 ───────────────────────────────────────────────────────────────

DEFAULT_ROOT = Path("/root/JiaoqiManager")
DEFAULT_PORT = 8788
HEALTH_PATH = "/api/health"
HEALTH_TIMEOUT = 10
START_WAIT = 90
STOP_GRACE = 10
MIN_DISK_GB = 1.0
DEPLOY_LOCK_NAME = "deploy.lock"
EXPECTED_APP_NAME = "交期管家"

# 日志脱敏: key 名关键词
SENSITIVE_KEYWORDS = (
    "key", "secret", "password", "token", "cookie",
    "credential", "passwd", "apikey", "api_key",
)
# 日志脱敏: 值中的路径片段
SENSITIVE_PATH_FRAGMENTS = ("secrets/", ".key", "cloud.env", "assistant-threads/")
# 日志脱敏: 值中的真实凭据样式
SENSITIVE_VALUE_PATTERNS = ("sk-", "Bearer ", "token=", "apikey=", "secret=")


@dataclass
class DeployConfig:
    """部署配置 — 所有路径由此派生，便于测试时注入临时目录."""

    root: Path = field(default_factory=lambda: DEFAULT_ROOT)
    port: int = DEFAULT_PORT

    @property
    def app_dir(self) -> Path:
        return self.root / "app"

    @property
    def runtime_python(self) -> Path:
        return self.root / "runtime" / "linux" / "bin" / "python"

    @property
    def start_script(self) -> Path:
        return self.app_dir / "deploy" / "start-cloud.sh"

    @property
    def start_script_local(self) -> Path:
        return self.app_dir / "deploy" / "start-linux.sh"

    @property
    def db_path(self) -> Path:
        return self.app_dir / "var" / "jiaoqi.db"

    @property
    def pid_file(self) -> Path:
        return self.root / "runtime" / "cloud.pid"

    @property
    def log_dir(self) -> Path:
        return self.root / "runtime" / "logs"

    @property
    def backup_dir(self) -> Path:
        return self.root / "runtime" / "backups"

    @property
    def secrets_dir(self) -> Path:
        return self.root / "secrets"

    @property
    def cloud_env(self) -> Path:
        return self.root / "runtime" / "cloud.env"

    @property
    def assistant_threads(self) -> Path:
        return self.root / "runtime" / "assistant-threads"

    @property
    def lock_file(self) -> Path:
        return self.root / "runtime" / DEPLOY_LOCK_NAME

    @property
    def health_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{HEALTH_PATH}"


# ─── 日志 (递归脱敏) ───────────────────────────────────────────────────

def _redact_value(value: object) -> object:
    """递归脱敏: 处理嵌套 dict/list/str."""
    if isinstance(value, dict):
        return {k: _redact_field(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    if isinstance(value, str):
        # 路径片段
        for frag in SENSITIVE_PATH_FRAGMENTS:
            if frag in value:
                return "[REDACTED]"
        # 真实凭据样式
        for pat in SENSITIVE_VALUE_PATTERNS:
            if pat in value:
                return "[REDACTED]"
    return value


def _redact_field(key: str, value: object) -> object:
    """字段级脱敏: key 名匹配则直接脱敏, 否则递归脱敏值."""
    k_lower = key.lower()
    if any(kw in k_lower for kw in SENSITIVE_KEYWORDS):
        return "[REDACTED]"
    return _redact_value(value)


def log_event(level: str, event: str, **fields: object) -> dict:
    """
    输出结构化 JSON 日志行 (递归脱敏).

    - 敏感 key 名 -> [REDACTED]
    - 值中含 secrets/ .key cloud.env assistant-threads/ -> [REDACTED]
    - 值中含 sk- Bearer token= apikey= secret= -> [REDACTED]
    - 嵌套 dict/list 递归脱敏
    - 不输出文件内容、密钥、会话内容
    """
    record: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event": event,
    }
    for k, v in fields.items():
        record[k] = _redact_field(k, v)
    line = json.dumps(record, ensure_ascii=False, default=str)
    print(line, flush=True)
    return record


# ─── PID 验证 (argv 精确匹配) ─────────────────────────────────────────

def validate_cmdline(args: list[str], expected_root: Path, expected_port: int) -> tuple[bool, dict | str]:
    """
    精确验证 cmdline argv 列表.

    检查项:
      1. args[0] 匹配 expected_root/runtime/linux/bin/python
      2. '-m' 后跟 'uvicorn'
      3. 'backend.server:app' 作为独立参数存在
      4. '--port' 后跟的值精确等于 str(expected_port) — 不会把 18788 当 8788
      5. 任何参数都不含 'RenCheng'

    返回 (True, info_dict) 或 (False, reason_str).
    """
    if not args:
        return False, "empty cmdline"

    # 1. 可执行文件路径
    expected_python = str(Path(expected_root) / "runtime" / "linux" / "bin" / "python")
    exe = args[0]
    try:
        exe_match = os.path.samefile(exe, expected_python)
    except OSError:
        exe_match = (exe == expected_python)
    if not exe_match and not exe.endswith("runtime/linux/bin/python"):
        return False, f"executable mismatch: {exe}"

    # 2. -m uvicorn
    try:
        m_idx = args.index("-m")
    except ValueError:
        return False, "missing -m flag"
    if m_idx + 1 >= len(args) or args[m_idx + 1] != "uvicorn":
        return False, "-m target is not uvicorn"

    # 3. backend.server:app 作为独立参数
    if "backend.server:app" not in args:
        return False, "missing backend.server:app"

    # 4. --port 精确值匹配
    try:
        port_idx = args.index("--port")
    except ValueError:
        return False, "missing --port flag"
    if port_idx + 1 >= len(args):
        return False, "--port has no value"
    port_val = args[port_idx + 1]
    if port_val != str(expected_port):
        return False, f"--port value is {port_val}, expected {expected_port}"

    # 5. 任何参数都不含 RenCheng
    for arg in args:
        if "RenCheng" in arg:
            return False, "cmdline references RenCheng"

    info = {
        "executable": exe,
        "module": "uvicorn",
        "app": "backend.server:app",
        "port": port_val,
    }
    return True, info


def verify_pid(pid: int, expected_root: Path, expected_port: int) -> tuple[bool, dict | str]:
    """
    严格验证 PID 属于本项目的 uvicorn 服务.

    检查 /proc/PID/cwd 和 /proc/PID/cmdline, 然后调用 validate_cmdline 精确验证.
    """
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return False, f"PID {pid} does not exist in /proc"

    # cwd 检查
    cwd_link = proc_dir / "cwd"
    try:
        cwd_resolved = cwd_link.resolve()
    except OSError as exc:
        return False, f"Cannot resolve /proc/{pid}/cwd: {exc}"

    expected_cwd = Path(expected_root) / "app"
    try:
        if not os.path.samefile(str(cwd_resolved), str(expected_cwd)):
            return False, f"PID {pid} cwd={cwd_resolved} != expected {expected_cwd}"
    except OSError:
        return False, f"Cannot compare cwd: {cwd_resolved} vs {expected_cwd}"

    # cmdline 检查
    cmdline_file = proc_dir / "cmdline"
    try:
        raw = cmdline_file.read_bytes()
    except OSError as exc:
        return False, f"Cannot read /proc/{pid}/cmdline: {exc}"

    args = [a.decode("utf-8", errors="replace") for a in raw.split(b"\x00") if a]
    ok, info = validate_cmdline(args, expected_root, expected_port)
    if not ok:
        return False, f"PID {pid}: {info}"

    if isinstance(info, dict):
        info["pid"] = pid
        info["cwd"] = str(cwd_resolved)
    return True, info


# ─── 健康检查 (HTTP 200 + JSON status=ok + app=交期管家) ──────────────

def validate_health_response(body_str: str, http_code: str) -> tuple[bool, dict | str]:
    """
    验证健康检查响应.

    要求: HTTP 200 + JSON status=ok + app=交期管家.
    非 JSON 响应体绝不写入日志 — 只返回通用错误消息.
    """
    if http_code != "200":
        return False, f"http_status={http_code}"

    try:
        body = json.loads(body_str)
    except json.JSONDecodeError:
        return False, "non-JSON response (body suppressed)"

    if body.get("status") != "ok":
        return False, "status is not ok"
    if body.get("app") != EXPECTED_APP_NAME:
        return False, "app name mismatch"

    return True, {"status": "ok", "app": EXPECTED_APP_NAME}


def health_check(url: str, timeout: int = HEALTH_TIMEOUT) -> tuple[bool, dict | str]:
    """请求健康端点, 验证 HTTP 200 + JSON. 非响应体不写入日志."""
    try:
        result = subprocess.run(
            ["curl", "-sS", "--connect-timeout", "5",
             "--max-time", str(timeout),
             "-w", "\n%{http_code}", url],
            capture_output=True, text=True, timeout=timeout + 5,
        )
    except FileNotFoundError:
        return False, "curl not found"
    except subprocess.TimeoutExpired:
        return False, f"curl timed out after {timeout}s"

    if result.returncode != 0:
        return False, f"curl exit={result.returncode}"

    # 最后一行是 http_code
    output = result.stdout
    nl_idx = output.rfind("\n")
    if nl_idx < 0:
        return False, "unexpected curl output format"

    http_code = output[nl_idx + 1:].strip()
    body_str = output[:nl_idx]

    return validate_health_response(body_str, http_code)


def check_port_listening(port: int) -> bool:
    """检查端口是否在监听 (只读)."""
    try:
        result = subprocess.run(
            ["ss", "-tln"],
            capture_output=True, text=True, timeout=5,
        )
        return f":{port}" in result.stdout
    except Exception:
        return False


def disk_free_gb(path: Path) -> float:
    """返回指定路径所在分区的可用空间 (GB)."""
    try:
        usage = shutil.disk_usage(str(path))
        return round(usage.free / (1024 ** 3), 2)
    except OSError:
        return -1.0


# ─── SQLite 备份 ───────────────────────────────────────────────────────

def backup_database(db_path: Path, dest_path: Path) -> dict:
    """
    使用 sqlite3 backup API 备份数据库, 保证 WAL 一致性.
    backup API 在一个事务中拷贝所有页面（含 WAL 中已提交的内容）.
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if dest_path.exists():
        dest_path.unlink()

    source = sqlite3.connect(str(db_path))
    dest = sqlite3.connect(str(dest_path))

    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()

    size = dest_path.stat().st_size
    return {"dest": str(dest_path), "size_bytes": size}


def verify_backup_integrity(db_path: Path, backup_path: Path) -> tuple[bool, dict]:
    """验证备份完整性: 对比两库的表名和每张表的行数."""
    def _get_tables_and_counts(path: Path) -> dict[str, int]:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row[0] for row in cur.fetchall()]
            counts = {}
            for t in tables:
                cur = conn.execute(f"SELECT count(*) FROM [{t}]")
                counts[t] = cur.fetchone()[0]
            return counts
        finally:
            conn.close()

    orig = _get_tables_and_counts(db_path)
    bak = _get_tables_and_counts(backup_path)
    match = orig == bak
    return match, {"original": orig, "backup": bak, "match": match}


# ─── 代码备份 (不删除既有备份) ─────────────────────────────────────────

CODE_BACKUP_EXCLUDE = {"var", "__pycache__", ".pytest_cache", ".git"}

def backup_code(app_dir: Path, dest_dir: Path) -> dict:
    """
    备份 app/ 代码到 dest_dir.
    不删除既有备份 — dest_dir 必须不存在, 否则报错.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=False)  # 不覆盖既有备份

    copied = 0
    for item in Path(app_dir).iterdir():
        if item.name in CODE_BACKUP_EXCLUDE:
            continue
        if item.is_dir():
            shutil.copytree(item, dest_dir / item.name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, dest_dir / item.name)
        copied += 1

    return {"dest": str(dest_dir), "items_copied": copied}


# ─── 部署锁 ────────────────────────────────────────────────────────────

def acquire_lock(lock_path: Path) -> tuple[bool, str]:
    """
    获取部署锁 (原子创建). 处理过期锁: 如果锁持有进程已不存在则接管.
    返回 (True, lock_path_str) 或 (False, reason).
    """
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True, str(lock_path)
    except FileExistsError:
        try:
            existing_pid_str = lock_path.read_text().strip()
            existing_pid = int(existing_pid_str)
            if Path(f"/proc/{existing_pid}").exists():
                return False, f"lock held by active PID {existing_pid}"
            # 过期锁 — 接管
            lock_path.unlink()
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True, str(lock_path)
        except (ValueError, OSError):
            return False, "lock exists but cannot read"


def release_lock(lock_path: Path) -> None:
    """释放部署锁."""
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


# ─── 部署器 ────────────────────────────────────────────────────────────

class Deployer:
    """部署管理器 — 所有操作通过此类执行."""

    def __init__(self, config: DeployConfig | None = None):
        self.cfg = config or DeployConfig()

    # ── preflight: 只读检查 ──

    def preflight(self) -> dict:
        """只读预检: 不修改任何文件, 不重启服务."""
        cfg = self.cfg
        results: dict = {}

        # 0. 固定根目录和端口验证
        results["root_is_fixed"] = (cfg.root == DEFAULT_ROOT)
        results["port_is_fixed"] = (cfg.port == DEFAULT_PORT)
        log_event("INFO", "preflight.fixed_config",
                   root_ok=results["root_is_fixed"],
                   port_ok=results["port_is_fixed"])

        # 1. 根目录
        results["root_exists"] = cfg.root.exists() and cfg.root.is_dir()
        log_event("INFO", "preflight.root", exists=results["root_exists"])

        # 2. app 目录
        results["app_exists"] = cfg.app_dir.exists() and cfg.app_dir.is_dir()

        # 3. runtime python
        results["python_exists"] = cfg.runtime_python.exists()

        # 4. start script
        results["start_script_exists"] = cfg.start_script_local.exists()

        # 5. database
        results["db_exists"] = cfg.db_path.exists()
        if results["db_exists"]:
            results["db_size_bytes"] = cfg.db_path.stat().st_size

        # 6. port
        results["port_listening"] = check_port_listening(cfg.port)

        # 7. health
        ok, info = health_check(cfg.health_url)
        results["health_ok"] = ok
        results["health_info"] = info
        log_event("INFO", "preflight.health", ok=ok)

        # 8. PID file
        if cfg.pid_file.exists():
            try:
                pid = int(cfg.pid_file.read_text().strip())
                results["pid_file"] = pid
                ok, info = verify_pid(pid, cfg.root, cfg.port)
                results["pid_verified"] = ok
                results["pid_info"] = info
                log_event("INFO", "preflight.pid", pid=pid, verified=ok)
            except (ValueError, OSError):
                results["pid_verified"] = False
                results["pid_info"] = "invalid pid file"
        else:
            results["pid_verified"] = False
            results["pid_info"] = "no pid file"

        # 9. disk space + 最低余量检查
        results["disk_free_gb"] = disk_free_gb(cfg.root)
        results["disk_sufficient"] = results["disk_free_gb"] >= MIN_DISK_GB
        log_event("INFO", "preflight.disk",
                   free_gb=results["disk_free_gb"],
                   sufficient=results["disk_sufficient"])

        # 10. secrets / cloud.env / assistant-threads (仅检查存在性)
        results["secrets_dir_exists"] = cfg.secrets_dir.exists()
        results["cloud_env_exists"] = cfg.cloud_env.exists()
        results["assistant_threads_exists"] = cfg.assistant_threads.exists()

        # 总结
        bool_keys = (
            "root_is_fixed", "port_is_fixed", "root_exists", "app_exists",
            "python_exists", "start_script_exists", "db_exists",
            "port_listening", "health_ok", "pid_verified",
            "disk_sufficient", "secrets_dir_exists",
        )
        results["all_checks_passed"] = all(
            results.get(k, False) for k in bool_keys
        )
        log_event("INFO", "preflight.complete",
                   all_passed=results["all_checks_passed"])
        return results

    # ── backup: 微秒/UUID 唯一名, 不删除既有 ──

    def backup(self) -> dict:
        """备份数据库和代码. 目录名含微秒+UUID, 不删除既有备份."""
        cfg = self.cfg
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        unique = uuid.uuid4().hex[:8]
        bdir = cfg.backup_dir / f"{ts}-{unique}"
        bdir.mkdir(parents=True, exist_ok=False)

        # 数据库
        db_dest = bdir / "jiaoqi.db"
        db_info = backup_database(cfg.db_path, db_dest)
        log_event("INFO", "backup.db", size=db_info["size_bytes"])

        ok, verify_info = verify_backup_integrity(cfg.db_path, db_dest)
        log_event("INFO", "backup.verify", ok=ok)

        # 代码
        code_dest = bdir / "app"
        code_info = backup_code(cfg.app_dir, code_dest)
        log_event("INFO", "backup.code", items=code_info["items_copied"])

        return {
            "backup_dir": str(bdir),
            "db": db_info,
            "db_verified": ok,
            "code": code_info,
        }

    # ── 故障恢复: 验证并停止新进程, 重试启动, 不动数据库 ──

    def _recover_from_failure(self, reason: str) -> dict:
        """
        从启动超时或健康检查失败中恢复.

        流程:
          1. 读取 pid_file 获取本次启动的新 PID
          2. verify_pid 严格验证 — 通过则 SIGTERM 停止
          3. 验证失败 → 中止, 不做任何破坏操作
          4. 重试 start-linux.sh
          5. 健康检查

        绝不恢复数据库或删除 WAL/SHM — 保留实时数据库.
        """
        cfg = self.cfg
        log_event("WARN", "recover.start", reason=reason)

        # 1. 查找新 PID
        if not cfg.pid_file.exists():
            log_event("WARN", "recover.no_pid_file")
        else:
            try:
                new_pid = int(cfg.pid_file.read_text().strip())
            except (ValueError, OSError):
                log_event("ERROR", "recover.invalid_pid")
                return {"ok": False, "stage": "recover_invalid_pid",
                        "reason": reason}

            # 2. PID 已不存在 → 允许直接重新启动
            if not Path(f"/proc/{new_pid}").exists():
                log_event("INFO", "recover.pid_already_gone", pid=new_pid)
            else:
                # 3. PID 存在 → 严格验证身份
                ok, info = verify_pid(new_pid, cfg.root, cfg.port)
                if not ok:
                    # 身份不符 → 拒绝, 不做破坏操作
                    log_event("ERROR", "recover.cannot_verify")
                    return {"ok": False, "stage": "recover_cannot_verify",
                            "reason": reason, "verify_reason": info}

                # 停止新进程
                log_event("INFO", "recover.stopping", pid=new_pid)
                try:
                    os.kill(new_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(STOP_GRACE):
                    if not Path(f"/proc/{new_pid}").exists():
                        break
                    time.sleep(1)

                # 停止超时 → 返回失败
                if Path(f"/proc/{new_pid}").exists():
                    log_event("ERROR", "recover.stop_timeout", pid=new_pid)
                    return {"ok": False, "stage": "recover_stop_timeout",
                            "reason": reason, "pid": new_pid}

        # 4. 重试启动
        log_event("INFO", "recover.retry_start")
        try:
            subprocess.run(
                ["bash", str(cfg.start_script_local)],
                capture_output=True, text=True, timeout=START_WAIT,
            )
        except subprocess.TimeoutExpired:
            log_event("ERROR", "recover.retry_timeout")
            return {"ok": False, "stage": "recover_retry_timeout",
                    "reason": reason}

        # 5. 健康检查
        time.sleep(2)
        ok, info = health_check(cfg.health_url)
        if ok:
            log_event("INFO", "recover.success", **info)
            return {"ok": True, "stage": "recovered",
                    "original_failure": reason}
        log_event("ERROR", "recover.health_failed")
        return {"ok": False, "stage": "recover_health_failed",
                "reason": reason}

    # ── deploy: 安全重启 ──

    def deploy(self) -> dict:
        """
        完整部署流程:
          preflight -> lock -> backup -> verify_pid -> stop -> start -> health
          -> (fail? recover: verify+stop+retry, 不动数据库)
        """
        cfg = self.cfg
        log_event("INFO", "deploy.start")

        # 1. preflight
        pf = self.preflight()
        if not pf.get("all_checks_passed"):
            log_event("ERROR", "deploy.preflight_failed")
            return {"ok": False, "stage": "preflight", "results": pf}

        # 2. 部署锁
        lock_ok, lock_info = acquire_lock(cfg.lock_file)
        if not lock_ok:
            log_event("ERROR", "deploy.lock_failed", reason=lock_info)
            return {"ok": False, "stage": "lock_failed", "reason": lock_info}
        log_event("INFO", "deploy.lock_acquired")

        try:
            # 3. backup
            bak = self.backup()
            if not bak.get("db_verified"):
                # 备份验证失败 → 直接退出, 不停止进程
                log_event("ERROR", "deploy.backup_verify_failed")
                return {"ok": False, "stage": "backup_verify_failed"}

            # 4. verify PID
            pid = pf.get("pid_file")
            if pid is None:
                log_event("ERROR", "deploy.no_pid")
                return {"ok": False, "stage": "no_pid"}

            ok, info = verify_pid(pid, cfg.root, cfg.port)
            if not ok:
                log_event("ERROR", "deploy.pid_verify_failed")
                return {"ok": False, "stage": "pid_verify", "reason": info}
            log_event("INFO", "deploy.pid_verified", pid=pid)

            # 5. stop (SIGTERM only to verified PID)
            log_event("INFO", "deploy.stopping", pid=pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                log_event("WARN", "deploy.process_gone", pid=pid)

            stopped = False
            for _ in range(STOP_GRACE):
                if not Path(f"/proc/{pid}").exists():
                    stopped = True
                    break
                time.sleep(1)
            if not stopped:
                log_event("WARN", "deploy.sigterm_timeout", pid=pid)
                return {"ok": False, "stage": "stop_timeout", "pid": pid}
            log_event("INFO", "deploy.stopped", pid=pid)

            # 6. start
            log_event("INFO", "deploy.starting")
            try:
                subprocess.run(
                    ["bash", str(cfg.start_script_local)],
                    capture_output=True, text=True, timeout=START_WAIT,
                )
            except subprocess.TimeoutExpired:
                # 启动超时 → 恢复 (不直接退出)
                return self._recover_from_failure("start_timeout")

            # 7. health check
            time.sleep(2)
            ok, info = health_check(cfg.health_url)
            if ok:
                log_event("INFO", "deploy.complete", ok=True)
                return {"ok": True, "stage": "complete"}

            # 8. 健康检查失败 → 恢复 (不动数据库)
            log_event("ERROR", "deploy.health_failed")
            return self._recover_from_failure("health_check_failed")

        finally:
            release_lock(cfg.lock_file)

    # ── verify only ──

    def verify(self) -> dict:
        """仅验证当前 PID."""
        cfg = self.cfg
        if not cfg.pid_file.exists():
            return {"ok": False, "reason": "no pid file"}
        try:
            pid = int(cfg.pid_file.read_text().strip())
        except (ValueError, OSError):
            return {"ok": False, "reason": "invalid pid file"}
        ok, info = verify_pid(pid, cfg.root, cfg.port)
        log_event("INFO", "verify", pid=pid, ok=ok)
        return {"ok": ok, "pid": pid, "info": info}


# ─── CLI ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="交期管家部署管理工具")
    parser.add_argument("command", choices=["preflight", "backup", "deploy", "verify"],
                        help="要执行的命令")
    args = parser.parse_args()

    deployer = Deployer()

    if args.command == "preflight":
        results = deployer.preflight()
        return 0 if results.get("all_checks_passed") else 1
    elif args.command == "backup":
        results = deployer.backup()
        return 0 if results.get("db_verified") else 1
    elif args.command == "deploy":
        results = deployer.deploy()
        return 0 if results.get("ok") else 1
    elif args.command == "verify":
        results = deployer.verify()
        return 0 if results.get("ok") else 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
