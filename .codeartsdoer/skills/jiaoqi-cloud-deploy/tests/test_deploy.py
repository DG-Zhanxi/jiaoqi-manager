"""
交期管家部署 Skill 隔离单元测试 (v2 — 覆盖代码审查修正的 7 个问题).
"""

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import deploy
from deploy import (
    DEFAULT_ROOT, DEFAULT_PORT, MIN_DISK_GB,
    DeployConfig, Deployer,
    acquire_lock, release_lock,
    backup_database, backup_code,
    log_event, verify_pid,
    validate_cmdline, validate_health_response,
    verify_backup_integrity,
)


@pytest.fixture
def tmp_root(tmp_path):
    (tmp_path / "app" / "var").mkdir(parents=True)
    (tmp_path / "app" / "deploy").mkdir(parents=True)
    (tmp_path / "app" / "backend").mkdir(parents=True)
    (tmp_path / "runtime" / "linux" / "bin").mkdir(parents=True)
    (tmp_path / "runtime" / "logs").mkdir(parents=True)
    (tmp_path / "secrets").mkdir(parents=True)
    (tmp_path / "runtime" / "assistant-threads").mkdir(parents=True)
    (tmp_path / "app" / "backend" / "server.py").write_text("# stub")
    (tmp_path / "app" / "deploy" / "start-linux.sh").write_text("#!/bin/bash\n")
    (tmp_path / "runtime" / "linux" / "bin" / "python").write_text("# stub")
    return tmp_path


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE plans (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO orders VALUES (1, 'A'), (2, 'B'), (3, 'C');
        INSERT INTO plans VALUES (1, 'active'), (2, 'done');
    """)
    conn.commit()
    conn.close()
    return db_path


def capture_log(level, event, **fields):
    buf = io.StringIO()
    with redirect_stdout(buf):
        log_event(level, event, **fields)
    return json.loads(buf.getvalue())


# ─── 1. PID 验证 — argv 精确匹配 (审查问题 1) ─────────────────────────

class TestPIDVerification:

    def test_nonexistent_pid_rejected(self):
        ok, info = verify_pid(999999, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False
        assert "does not exist" in info

    def test_wrong_cwd_rejected(self, tmp_path):
        ok, info = verify_pid(os.getpid(), tmp_path, DEFAULT_PORT)
        assert ok is False

    def test_wrong_port_rejected_real(self):
        real_pid_file = DEFAULT_ROOT / "runtime" / "cloud.pid"
        if not real_pid_file.exists():
            pytest.skip("真实 PID 文件不存在")
        pid = int(real_pid_file.read_text().strip())
        ok, info = verify_pid(pid, DEFAULT_ROOT, 9999)
        assert ok is False

    def test_validate_cmdline_correct(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "uvicorn", "backend.server:app",
                "--host", "0.0.0.0", "--port", "8788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is True
        assert info["port"] == "8788"

    def test_validate_cmdline_rejects_18788(self):
        """端口 18788 绝不能匹配 8788."""
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "uvicorn", "backend.server:app",
                "--host", "0.0.0.0", "--port", "18788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False
        assert "18788" in info

    def test_validate_cmdline_rejects_wrong_exe(self):
        args = ["/usr/bin/python3", "-m", "uvicorn",
                "backend.server:app", "--port", "8788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False
        assert "executable" in info

    def test_validate_cmdline_rejects_missing_m(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "uvicorn", "backend.server:app", "--port", "8788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False

    def test_validate_cmdline_rejects_wrong_module(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "gunicorn", "backend.server:app", "--port", "8788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False

    def test_validate_cmdline_rejects_missing_app(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "uvicorn", "other.app", "--port", "8788"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False

    def test_validate_cmdline_rejects_missing_port(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "uvicorn", "backend.server:app", "--host", "0.0.0.0"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False

    def test_validate_cmdline_rejects_rencheng(self):
        args = ["/root/JiaoqiManager/runtime/linux/bin/python",
                "-m", "uvicorn", "backend.server:app",
                "--port", "8788", "/root/RenCheng/x"]
        ok, info = validate_cmdline(args, DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False
        assert "RenCheng" in info

    def test_validate_cmdline_empty(self):
        ok, info = validate_cmdline([], DEFAULT_ROOT, DEFAULT_PORT)
        assert ok is False


# ─── 2. 根目录验证 (审查问题 7) ────────────────────────────────────────

class TestRootDirValidation:

    def test_nonexistent_root_preflight_fails(self):
        cfg = DeployConfig(root=Path("/nonexistent/path/xyz"))
        results = Deployer(cfg).preflight()
        assert results["root_exists"] is False
        assert results["all_checks_passed"] is False

    def test_wrong_root_does_not_touch_real(self, tmp_path):
        results = Deployer(DeployConfig(root=tmp_path)).preflight()
        assert results["root_exists"] is True
        assert results["app_exists"] is False

    def test_preflight_checks_fixed_root(self, tmp_root):
        results = Deployer(DeployConfig(root=tmp_root)).preflight()
        assert results["root_is_fixed"] is False
        assert results["all_checks_passed"] is False

    def test_preflight_checks_fixed_port(self):
        results = Deployer(DeployConfig(root=DEFAULT_ROOT, port=9999)).preflight()
        assert results["port_is_fixed"] is False


# ─── 3. 数据库备份完整性 ───────────────────────────────────────────────

class TestDatabaseBackup:

    def test_backup_creates_valid_file(self, tmp_db, tmp_path):
        dest = tmp_path / "backup.db"
        info = backup_database(tmp_db, dest)
        assert dest.exists()
        assert info["size_bytes"] > 0

    def test_backup_data_matches_original(self, tmp_db, tmp_path):
        dest = tmp_path / "backup.db"
        backup_database(tmp_db, dest)
        ok, info = verify_backup_integrity(tmp_db, dest)
        assert ok is True

    def test_backup_independent_of_original(self, tmp_db, tmp_path):
        dest = tmp_path / "backup.db"
        backup_database(tmp_db, dest)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("INSERT INTO orders VALUES (999, 'new')")
        conn.commit()
        conn.close()
        conn = sqlite3.connect(str(dest))
        assert conn.execute("SELECT count(*) FROM orders").fetchone()[0] == 3
        conn.close()

    def test_backup_with_wal(self, tmp_path):
        db_path = tmp_path / "wal_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, val TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'a'), (2, 'b')")
        conn.commit()
        conn.close()
        dest = tmp_path / "wal_backup.db"
        backup_database(db_path, dest)
        ok, info = verify_backup_integrity(db_path, dest)
        assert ok is True
        assert info["backup"]["items"] == 2

    def test_backup_integrity_detects_mismatch(self, tmp_db, tmp_path):
        dest = tmp_path / "backup.db"
        backup_database(tmp_db, dest)
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("INSERT INTO orders VALUES (100, 'extra')")
        conn.commit()
        conn.close()
        ok, _ = verify_backup_integrity(tmp_db, dest)
        assert ok is False


# ─── 4. 日志无凭据 (递归脱敏 + 真实凭据样式, 审查问题 6) ──────────────

class TestLogNoCredentials:

    def test_api_key_redacted(self):
        assert capture_log("INFO", "t", api_key="sk-123")["api_key"] == "[REDACTED]"

    def test_password_redacted(self):
        assert capture_log("INFO", "t", password="s3")["password"] == "[REDACTED]"

    def test_token_redacted(self):
        assert capture_log("INFO", "t", token="abc")["token"] == "[REDACTED]"

    def test_cookie_redacted(self):
        assert capture_log("INFO", "t", cookie="s=x")["cookie"] == "[REDACTED]"

    def test_secret_redacted(self):
        assert capture_log("INFO", "t", secret="v")["secret"] == "[REDACTED]"

    def test_credential_redacted(self):
        assert capture_log("INFO", "t", credential="v")["credential"] == "[REDACTED]"

    def test_secrets_path_in_value_redacted(self):
        r = capture_log("INFO", "t", path="/root/JiaoqiManager/secrets/moonshot.key")
        assert r["path"] == "[REDACTED]"

    def test_cloud_env_in_value_redacted(self):
        r = capture_log("INFO", "t", config="/root/JiaoqiManager/runtime/cloud.env")
        assert r["config"] == "[REDACTED]"

    def test_assistant_threads_in_value_redacted(self):
        r = capture_log("INFO", "t", f="/root/JiaoqiManager/runtime/assistant-threads/a.json")
        assert r["f"] == "[REDACTED]"

    def test_normal_fields_not_redacted(self):
        r = capture_log("INFO", "t", port=8788, ok=True, path="/root/JiaoqiManager/app")
        assert r["port"] == 8788 and r["ok"] is True

    def test_log_output_is_valid_json(self):
        r = capture_log("INFO", "t", port=8788)
        assert "timestamp" in r and r["event"] == "t"

    def test_nested_dict_redacted(self):
        r = capture_log("INFO", "t", detail={"api_key": "sk-abc", "port": 8788})
        assert r["detail"]["api_key"] == "[REDACTED]"
        assert r["detail"]["port"] == 8788

    def test_nested_list_redacted(self):
        r = capture_log("INFO", "t", paths=["/app", "/root/JiaoqiManager/secrets/x.key"])
        assert r["paths"][0] == "/app"
        assert r["paths"][1] == "[REDACTED]"

    def test_deeply_nested_redacted(self):
        r = capture_log("INFO", "t", data={"l1": {"l2": {"password": "s"}}})
        assert r["data"]["l1"]["l2"]["password"] == "[REDACTED]"

    def test_sk_prefix_redacted(self):
        assert capture_log("INFO", "t", value="sk-abcdef123")["value"] == "[REDACTED]"

    def test_bearer_prefix_redacted(self):
        assert capture_log("INFO", "t", header="Bearer abc.def")["header"] == "[REDACTED]"

    def test_token_equals_redacted(self):
        assert capture_log("INFO", "t", url="https://api?token=xyz")["url"] == "[REDACTED]"


# ─── 5. 代码备份 (不删除既有, 审查问题 5) ──────────────────────────────

class TestCodeBackup:

    def test_backup_code_copies_files(self, tmp_root):
        (tmp_root / "app" / "main.py").write_text("# main")
        dest = tmp_root / "code_backup"
        info = backup_code(tmp_root / "app", dest)
        assert (dest / "main.py").exists()

    def test_backup_code_excludes_var(self, tmp_root):
        (tmp_root / "app" / "var" / "jiaoqi.db").write_text("db")
        dest = tmp_root / "code_backup"
        backup_code(tmp_root / "app", dest)
        assert not (dest / "var").exists()

    def test_backup_code_no_delete_existing(self, tmp_root):
        (tmp_root / "app" / "main.py").write_text("# main")
        dest = tmp_root / "code_backup"
        backup_code(tmp_root / "app", dest)
        with pytest.raises(FileExistsError):
            backup_code(tmp_root / "app", dest)


# ─── 6. 健康检查 (审查问题 3) ──────────────────────────────────────────

class TestHealthCheck:

    def test_validate_health_correct(self):
        ok, info = validate_health_response('{"status":"ok","app":"交期管家"}', "200")
        assert ok is True
        assert info["app"] == "交期管家"

    def test_validate_health_non_200(self):
        ok, info = validate_health_response("{}", "503")
        assert ok is False
        assert "503" in info

    def test_validate_health_wrong_status(self):
        ok, info = validate_health_response('{"status":"error","app":"交期管家"}', "200")
        assert ok is False

    def test_validate_health_wrong_app(self):
        ok, info = validate_health_response('{"status":"ok","app":"other"}', "200")
        assert ok is False

    def test_non_json_no_body_leak(self):
        """非 JSON 响应体绝不出现在错误消息中 (审查问题 3)."""
        secret_body = '{"incomplete": "json"'
        ok, info = validate_health_response(secret_body, "200")
        assert ok is False
        # 错误消息中不应包含响应体内容
        assert secret_body not in info
        assert "body suppressed" in info

    def test_non_json_html_no_leak(self):
        """HTML 响应体不泄漏."""
        html_body = "<html><body>Internal Error: password=secret123</body></html>"
        ok, info = validate_health_response(html_body, "500")
        assert ok is False
        assert "secret123" not in info
        assert html_body not in info


# ─── 7. 部署锁 (审查问题 5) ────────────────────────────────────────────

class TestDeployLock:

    def test_acquire_lock_succeeds(self, tmp_path):
        lock = tmp_path / "test.lock"
        ok, info = acquire_lock(lock)
        assert ok is True
        assert lock.exists()
        release_lock(lock)

    def test_concurrent_lock_fails(self, tmp_path):
        """第二个 acquire 应失败."""
        lock = tmp_path / "test.lock"
        ok1, _ = acquire_lock(lock)
        assert ok1 is True
        ok2, reason = acquire_lock(lock)
        assert ok2 is False
        assert "lock held" in reason or "cannot read" in reason
        release_lock(lock)

    def test_stale_lock_taken_over(self, tmp_path):
        """过期锁 (进程不存在) 应被接管."""
        lock = tmp_path / "test.lock"
        lock.write_text("999999")  # 不存在的 PID
        ok, info = acquire_lock(lock)
        assert ok is True
        release_lock(lock)

    def test_release_lock_idempotent(self, tmp_path):
        lock = tmp_path / "test.lock"
        release_lock(lock)  # 不存在也不报错


# ─── 8. 备份目录唯一名 (审查问题 5) ────────────────────────────────────

class TestBackupUniqueDir:

    def test_backup_dir_has_unique_name(self, tmp_root):
        """备份目录名应含微秒+UUID."""
        import re
        # 需要一个数据库才能备份
        (tmp_root / "app" / "var" / "jiaoqi.db").touch()
        # 创建空 db
        import sqlite3
        conn = sqlite3.connect(str(tmp_root / "app" / "var" / "jiaoqi.db"))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.commit()
        conn.close()
        cfg = DeployConfig(root=tmp_root)
        bak = Deployer(cfg).backup()
        bdir = Path(bak["backup_dir"])
        # 目录名应匹配 YYYYmmddTHHMMSSffffffZ-xxxxxxxx 格式
        name = bdir.name
        assert re.match(r"\d{8}T\d{6}\d{6}Z-[0-9a-f]{8}", name), f"name={name}"

    def test_backup_no_delete_existing(self, tmp_root):
        """新备份不应删除既有备份目录."""
        import sqlite3
        db = tmp_root / "app" / "var" / "jiaoqi.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.commit()
        conn.close()
        cfg = DeployConfig(root=tmp_root)
        bak1 = Deployer(cfg).backup()
        bak2 = Deployer(cfg).backup()
        # 两个备份目录都应存在
        assert Path(bak1["backup_dir"]).exists()
        assert Path(bak2["backup_dir"]).exists()
        assert bak1["backup_dir"] != bak2["backup_dir"]


# ─── 9. 故障恢复 — 不覆盖运行 DB (审查问题 4) ──────────────────────────

class TestRecoveryNoDbRestore:

    def test_recover_does_not_restore_db(self, tmp_root, monkeypatch):
        """_recover_from_failure 不应恢复数据库或删除 WAL/SHM."""
        import sqlite3
        db = tmp_root / "app" / "var" / "jiaoqi.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (1)")
        conn.commit()
        conn.close()
        wal = Path(str(db) + "-wal")
        wal.write_bytes(b"wal data")
        shm = Path(str(db) + "-shm")
        shm.write_bytes(b"shm data")

        cfg = DeployConfig(root=tmp_root)
        deployer = Deployer(cfg)

        # mock 外部调用 — 不触碰真实服务
        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        monkeypatch.setattr(deploy, "health_check", lambda *a, **kw: (True, {"status": "ok"}))

        result = deployer._recover_from_failure("test_failure")

        # WAL/SHM 应原封不动 (先检查, 再开数据库, 避免 SQLite 清理无效 WAL)
        assert wal.read_bytes() == b"wal data"
        assert shm.read_bytes() == b"shm data"
        # 数据库内容应不变
        assert db.exists()
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT count(*) FROM x").fetchone()[0] == 1
        conn.close()

    def test_recover_cannot_verify_aborts(self, tmp_root, monkeypatch):
        """PID 存在但身份不符时应中止, 不做破坏操作."""
        import os
        # 用当前测试进程 PID — 它存在但 cwd/cmdline 不匹配交期管家
        my_pid = os.getpid()
        (tmp_root / "runtime").mkdir(parents=True, exist_ok=True)
        (tmp_root / "runtime" / "cloud.pid").write_text(str(my_pid))
        cfg = DeployConfig(root=tmp_root)
        deployer = Deployer(cfg)
        result = deployer._recover_from_failure("test")
        assert result["ok"] is False
        assert "cannot_verify" in result["stage"]

    def test_recover_pid_gone_allows_restart(self, tmp_root, monkeypatch):
        """PID 文件存在但进程已消失 → 应允许重新启动."""
        import subprocess
        # 写入一个不存在的 PID (999999 几乎不可能存在)
        (tmp_root / "runtime").mkdir(parents=True, exist_ok=True)
        (tmp_root / "runtime" / "cloud.pid").write_text("999999")
        cfg = DeployConfig(root=tmp_root)
        deployer = Deployer(cfg)

        # mock 外部调用
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
        monkeypatch.setattr(deploy, "health_check", lambda *a, **kw: (True, {"status": "ok"}))

        result = deployer._recover_from_failure("test_failure")
        assert result["ok"] is True
        assert result["stage"] == "recovered"

    def test_recover_stop_timeout_returns_failure(self, tmp_root, monkeypatch):
        """PID 存在且身份确认, 但进程拒绝退出 → 应返回失败."""
        import subprocess, os, signal, time
        # 启动一个 sleep 300 子进程模拟无法停止的进程
        proc = subprocess.Popen(["sleep", "300"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            (tmp_root / "runtime").mkdir(parents=True, exist_ok=True)
            (tmp_root / "runtime" / "cloud.pid").write_text(str(proc.pid))
            cfg = DeployConfig(root=tmp_root)
            deployer = Deployer(cfg)

            # mock verify_pid 返回 True (身份确认)
            monkeypatch.setattr(deploy, "verify_pid", lambda *a, **kw: (True, "ok"))
            # mock health_check (不应被调用到)
            monkeypatch.setattr(deploy, "health_check", lambda *a, **kw: (True, {"status": "ok"}))
            # mock subprocess.run 防止真实启动
            monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
            # 临时将 STOP_GRACE 设为 1 秒以加速测试
            monkeypatch.setattr(deploy, "STOP_GRACE", 1)

            result = deployer._recover_from_failure("test_failure")
            assert result["ok"] is False
            assert "stop_timeout" in result["stage"]
        finally:
            proc.kill()
            proc.wait()

    def test_rollback_function_removed(self):
        """旧的 rollback 函数 (会恢复 DB) 应已不存在."""
        assert not hasattr(deploy, "rollback"), \
            "rollback function should be removed — use _recover_from_failure"


# ─── 10. 备份验证失败不停止进程 (审查问题 2) ───────────────────────────

class TestBackupVerifyFailure:

    def test_deploy_fails_on_bad_backup(self, tmp_root, monkeypatch):
        """备份验证失败时 deploy 应直接退出, 不停止进程."""
        import sqlite3
        db = tmp_root / "app" / "var" / "jiaoqi.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.commit()
        conn.close()

        cfg = DeployConfig(root=tmp_root)
        deployer = Deployer(cfg)

        # monkeypatch backup 返回 db_verified=False
        def fake_backup(self):
            return {"backup_dir": "/tmp/fake", "db_verified": False}
        monkeypatch.setattr(Deployer, "backup", fake_backup)

        # monkeypatch preflight 返回通过
        def fake_preflight(self):
            return {"all_checks_passed": True, "pid_file": 999999}
        monkeypatch.setattr(Deployer, "preflight", fake_preflight)

        result = deployer.deploy()
        assert result["ok"] is False
        assert result["stage"] == "backup_verify_failed"
        # 不应到达 pid_verify 阶段 — 进程未被停止
        assert result["stage"] != "pid_verify"


# ─── 11. 启动超时恢复 (审查问题 4) ─────────────────────────────────────

class TestStartTimeoutRecovery:

    def test_start_timeout_triggers_recovery(self, tmp_root, monkeypatch):
        """启动超时应触发恢复, 而非直接退出."""
        cfg = DeployConfig(root=tmp_root)
        deployer = Deployer(cfg)

        call_log = []

        def fake_preflight(self):
            return {"all_checks_passed": True, "pid_file": 999999}
        monkeypatch.setattr(Deployer, "preflight", fake_preflight)

        def fake_backup(self):
            return {"backup_dir": "/tmp/fake", "db_verified": True}
        monkeypatch.setattr(Deployer, "backup", fake_backup)

        # verify_pid 返回 True (模拟验证通过)
        monkeypatch.setattr(deploy, "verify_pid", lambda *a: (True, {"pid": 999999}))

        # os.kill 不做任何事
        monkeypatch.setattr(os, "kill", lambda *a: call_log.append("kill"))

        # /proc/999999 不存在 → stopped=True
        monkeypatch.setattr(Path, "exists", lambda self: False)

        # start script 超时
        import subprocess
        def fake_run(*a, **kw):
            call_log.append("start_attempt")
            raise subprocess.TimeoutExpired(cmd="bash", timeout=90)
        monkeypatch.setattr(subprocess, "run", fake_run)

        # _recover_from_failure 返回恢复结果
        def fake_recover(self, reason):
            call_log.append(f"recover:{reason}")
            return {"ok": False, "stage": "recovered", "reason": reason}
        monkeypatch.setattr(Deployer, "_recover_from_failure", fake_recover)

        result = deployer.deploy()
        assert "recover:start_timeout" in call_log
        assert result["stage"] == "recovered"


# ─── 12. 最低磁盘余量 (审查问题 7) ─────────────────────────────────────

class TestMinDiskCheck:

    def test_preflight_checks_disk_sufficient(self):
        """preflight 应检查最低磁盘余量."""
        results = Deployer().preflight()
        assert "disk_sufficient" in results
        assert "disk_free_gb" in results
        # 当前环境应有足够磁盘
        assert results["disk_sufficient"] is True

    def test_min_disk_gb_constant(self):
        """MIN_DISK_GB 应为正数."""
        assert MIN_DISK_GB > 0


# ─── 13. 端口检查 ──────────────────────────────────────────────────────

class TestPortCheck:

    def test_check_port_listening_returns_bool(self):
        assert isinstance(deploy.check_port_listening(8788), bool)

    def test_check_nonexistent_port(self):
        assert deploy.check_port_listening(59999) is False
