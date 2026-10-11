"""检查套件：一节一章（``--section`` 过滤的粒度），模块或包。

每章是普通 pytest 模块：用例经 ``dpe_conformance.plugin`` 的夹具取得被测 remote、已配置
身份与服务端钩子；需要而未配置的依赖使对应用例显式 skip（计入计数，见包 README）。
"""
