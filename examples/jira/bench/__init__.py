"""Jira 映射粒度的基准测试（#237）：「工单 = 文档」（正式插件）对「项目 = 文档」（``project_mode``，
只用于测试）。不发布，不属于插件的正式映射；用法见 ``bench/README.md``。"""

import sys
from pathlib import Path

# 模拟 Jira 复用测试目录里的 ``fake_jira``（与 pytest 的 pythonpath 配置一致），不复制一份
_TESTS = str(Path(__file__).resolve().parents[1] / "tests")
if _TESTS not in sys.path:
    sys.path.append(_TESTS)
