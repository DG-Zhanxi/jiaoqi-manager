"""pytest 配置 — 确保测试隔离, 不触碰真实部署."""

import sys
from pathlib import Path

# 将 skill 目录加入 sys.path 以便 import deploy
SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))
