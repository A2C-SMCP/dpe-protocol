"""文件路径 → file_type 的映射表（连接器自己的映射策略，见 README「映射规则」）。

映射是**封闭**的：只有下表列出的文件会被枚举；其余文件一律不产出（既不产 document 也不产
error）。不产出意味着运行器**不会**推断为删除（connector 契约 §2、§6.5：删除必须显式产出），
因此这里每增删一行都是范围决策，要同步 README 与增量游标的设计。

路径按**原始字节**判定与比较：git 的路径是字节串，可能不是 UTF-8（本模块只对扩展名做 ASCII
后缀比较，不要求整条路径解码成功）。``file_type`` 取值必须属于 core §2.5 的封闭枚举，扩展名
一律按 ASCII 小写后比较。**源代码文件标 ``txt``**：枚举里没有单个代码文件的类型（``*_repo``
是仓库级的），未知取值会被拒绝，本连接器不自行发明取值。扩展名不写进任何 metadata——它是
file_uri（身份）的一部分，改扩展名即改身份，这与其他 metadata 留空的理由一致。
"""

from __future__ import annotations

#: 扩展名（小写 ASCII，含点）→ file_type（core §2.5 的封闭枚举）
_EXTENSIONS: dict[bytes, str] = {
    # 标记文本
    b".md": "md",
    b".markdown": "md",
    b".mdown": "md",
    # 纯文本
    b".txt": "txt",
    b".text": "txt",
    # 分隔文本
    b".csv": "csv",
    b".tsv": "tsv",
    # 结构化文本
    b".json": "json",
    b".ndjson": "ndjson",
    # 源代码（标 txt：枚举里没有单个代码文件的类型）
    b".java": "txt",
    b".py": "txt",
    b".pyi": "txt",
    b".js": "txt",
    b".mjs": "txt",
    b".cjs": "txt",
    b".jsx": "txt",
    b".ts": "txt",
    b".mts": "txt",
    b".cts": "txt",
    b".tsx": "txt",
}

#: 无扩展名但按整名识别的文件 → file_type（整名按小写 ASCII 比较）
_FILENAMES: dict[bytes, str] = {
    b"makefile": "txt",
    b"dockerfile": "txt",
    b"license": "txt",
    b"notice": "txt",
}


def file_type_of(path: bytes) -> str | None:
    """路径 → ``file_type``；不在映射表内返回 ``None``（调用方跳过）。

    入参是 git 给出的原始路径字节。文件名取最后一个 ``/`` 之后的部分，整体 ASCII 小写后
    比较扩展名或整名：非 ASCII 字节原样保留、不参与匹配，但**不影响**扩展名命中
    （``笔记.md``、``caf\\xe9.md`` 都按 ``.md`` 枚举，file_uri 由字节百分号编码表达）。
    """
    name = path.rsplit(b"/", 1)[-1].lower()
    dot = name.rfind(b".")
    if dot > 0:
        file_type = _EXTENSIONS.get(name[dot:])
        if file_type is not None:
            return file_type
    return _FILENAMES.get(name)
