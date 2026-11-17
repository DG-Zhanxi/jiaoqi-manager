# Skills部署与华为云服务关联

## 实际实现

- `jiaoqi-codearts-link`：核对零元套餐，建立独立CodeArts项目，导入GitHub公开仓库并匿名核验提交。
- `jiaoqi-cloud-deploy`：检查现有环境，备份SQLite一致性快照和代码，精确验证工作目录、启动命令与8788端口，定向重启并检查HTTP状态及应用名称。故障分支重试原服务，业务数据库始终保留。

关联云服务采用 **CodeArts Repo**。应用运行于 **AI开发者空间**，业务存储采用 **SQLite**。这次执行为已有版本的自动重部署，RDS与Redis托管实例未纳入本版本。

## 已建立的云资源

[CodeArts项目与仓库](https://devcloud.cn-southwest-2.huaweicloud.com/codehub/project/e7989a0bb95d41038306ce4b0a94fcf7/codehub/8095130/home)

```text
https://codehub.devcloud.cn-southwest-2.huaweicloud.com/e7989a0bb95d41038306ce4b0a94fcf7/jiaoqi-manager.git
```

CodeArts体验版：50人，实付0元，手动续费，有效期至2026年12月17日23:59:59（北京时间）。部署使用已有开发者空间核时。证据中的提交是核验当时的固定版本；后续源代码更新后需同步云仓库并重新校验。

## 使用

项目级Skill位于`.codeartsdoer/skills/`，按CodeArts的项目级技能机制加载。AI Shell的发现目录已验证为`/root/.agents/skills/jiaoqi-cloud-deploy/`，实际`load_skill`调用成功，并执行部署前检查。其他智能体可按其目录规则导入同一Skill。

在已有云端环境中：

```bash
cd /root/JiaoqiManager
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py preflight
runtime/linux/bin/python .codeartsdoer/skills/jiaoqi-cloud-deploy/deploy.py deploy
```

首次安装的Linux环境先使用`bash app/deploy/install-linux.sh`准备运行时，再执行`bash app/deploy/start-linux.sh`并注册自己的账号。该部署Skill仅接受既定项目目录与端口。

## 实际运行结果

2026年11月17日15:36（北京时间），部署脚本完成一次真实执行，进程15165切换到10778，22张业务表的行数前后一致，公网健康检查通过。日志中的部署起止时间为07:36:49.354920—07:36:54.642942 UTC。

云端执行64项Skill隔离测试和54项应用回归测试，均通过。焊装研究由`research.runner.run_suite(999,"biw",5,3)`生成21条结果，覆盖3个种子与7种算法，全部通过独立约束校验，原始记录为`docs/skills/welding-research-21.json`。独立公开交付版本此前的56项应用测试记录继续保留，测试环境与范围分别标注。

JSON、部署日志和测试输出保存在`docs/skills/`。其中`deployment-run.json`保存前后PID与逐表记录数，`git-remote-verify.json`保存关联仓库的实际提交核验。

## 凭据与数据

密钥、账号数据库、会话、云端配置和数据库备份保留在部署环境中。公开仓库提供可执行脚本、配置示例与不含凭据的证据。
