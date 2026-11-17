---
name: jiaoqi-codearts-link
description: 将交期管家公开源码关联到华为云CodeArts免费体验版代码托管服务；用于建立项目、导入仓库、核对提交版本和保存关联证据。
---

# 交期管家关联CodeArts云服务

固定来源为 https://github.com/DG-Zhanxi/jiaoqi-manager ，从此仓库读取当前版本；操作前核对账号、区域、套餐和余额。只使用已经确认零元且关闭自动续费的体验版，不购买基础版、增值包或额外资源。

1. 使用已登录控制台，核对当前CodeArts体验版在有效期内。记录费用、自动续费状态及有效期，不记录Cookie、密钥或密码。
2. 检查是否存在交期管家项目及仓库。存在时核对并复用；不存在时创建独立项目 jiaoqi-manager，保持与韧城分离。
3. 从上述公开GitHub地址导入源码到交期管家仓库。只传输公开版本，不上传本地secrets、运行数据库或会话目录。
4. 核对仓库文件、README、LICENSE、data与experiments目录，比较导入提交和GitHub提交。确认服务在该项目中实际使用。
5. 记录项目/仓库URL与提交、套餐范围和实际结果，保存截图。只有创建、导入和核验均成功才报告关联完成。
6. 将关联仓库地址交给jiaoqi-cloud-deploy部署Skill；部署前核对版本及源码来源。任何需要新增收费套餐或新增认证凭据的操作停止，保留现有应用并报告缺少的条件。

使用控制台UI或已经配置的官方CLI均可。UI操作使用当前会话支持的浏览器工具；根据页面最新状态定位操作，不编造接口或绕过认证。创建新持久化密钥、接受服务条款仍按当前工具所要求的用户确认执行。

## 版本核验命令

```bash
python .codeartsdoer/skills/jiaoqi-codearts-link/scripts/verify_link.py --expected-commit <完整40位提交SHA> --output evidence/skills/cloud-repository-verification.json
```

该脚本只读访问公开云仓库，禁用Git凭据助手；版本不一致或访问失败时退出，不创建资源。
