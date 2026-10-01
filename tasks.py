"""Invoke 任务入口。

使用方式:
    inv --list              # 查看所有可用任务
    inv docs.serve          # 本地预览文档
    inv docs.build          # mike 构建当前版本
    inv docs.deploy         # 部署文档到 doc.turingfocus.cn
"""

from invoke import Collection

from scripts.docs import tasks as docs_tasks

ns = Collection()
ns.add_collection(Collection.from_module(docs_tasks), name="docs")
