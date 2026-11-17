---
name: jiaoqi-cloud-deploy
description: >
  交期管家（JiaoqiManager）部署管理 Skill。支持只读 preflight 检查、SQLite 数据库
  备份（sqlite3 backup API 保证 WAL 一致性）、app 代码备份、精确 PID 验证后安全重启、
  健康检查、故障恢复（重试启动并保留实时数据库，绝不覆盖运行中 SQLite/WAL）。
  结构化 JSON 日志（自动脱敏）。固定根目录 /root/JiaoqiManager、端口 8788。
---

# jiaoqi-cloud-deploy

## 用途

管理 `/root/JiaoqiManager` 交期管家服务的部署生命周期。仅使用当前已有开发者空间核时，
不创建云资源、不购买、不调用收费 API。

## 安全约束（不可违反）

1. **固定根目录** `/root/JiaoqiManager`，端口 `8788` — 不可配置为其他路径。
2. **精确 PID 验证** — 停止任何进程前，必须验证 `/proc/PID/cwd` 属于本项目且
   `cmdline` 精确匹配 `runtime/linux/bin/python` + `-m uvicorn` +
   `backend.server:app` + `--port 8788`。验证失败则拒绝执行。
3. **绝不 pkill** — 只对验证通过的特定 PID 发送 SIGTERM。
4. **绝不影响 /root/RenCheng** — 所有文件操作限定在 `/root/JiaoqiManager` 内。
5. **始终保留实时数据库** — 部署和故障恢复过程中绝不覆盖或删除 `app/var/jiaoqi.db`
   及其 WAL/SHM 文件。备份是只读副本，不用于回写。
6. **日志无凭据** — JSON 日志中敏感字段自动替换为 `[REDACTED]`。
7. **复用现有运行时** — 使用 `runtime/linux/bin/python` 和 `app/deploy/start-linux.sh`。

## 命令

```bash
# 只读 preflight（不修改任何文件、不重启服务）
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py preflight

# 完整部署（备份 → 精确PID验证 → 停止 → 启动 → 健康检查 → 失败则故障恢复）
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py deploy

# 仅备份数据库和代码
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py backup

# 仅验证当前 PID
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py verify
```

## 部署流程

```
preflight → backup(SQLite + code) → verify_pid(精确argv) → stop(SIGTERM)
→ start(start-linux.sh) → health_check(HTTP 200 + JSON status=ok + app=交期管家)
→ fail? _recover_from_failure : done
```

## 故障恢复策略

健康检查失败或启动超时时，`_recover_from_failure` 执行：

1. 读取 `pid_file` 获取本次启动的新 PID
2. **PID 已不存在**（`/proc/PID` 不存在）→ 允许直接重新启动
3. **PID 存在但身份不符**（verify_pid 失败）→ 拒绝执行，返回失败
4. **PID 存在且身份确认** → SIGTERM 停止；超时未退出 → 返回失败
5. 重试 `start-linux.sh` → 健康检查

**绝不恢复数据库备份、绝不删除 WAL/SHM — 始终保留实时数据库。**

## 测试

```bash
cd /root/JiaoqiManager
runtime/linux/bin/python -m pytest .codeartsdoer/skills/jiaoqi-cloud-deploy/tests/ -v
```
