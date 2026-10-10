"""提交 → 元素（连接器自己的映射策略，见 README「映射规则」）。

元素是**一次提交**：``category`` 取 ``NarrativeText``（提交说明是叙述文本），``text`` 是提交
说明全文（原样，不剥离），``metadata`` 是 Git 中不可变的源事实，全部进 hash（契约 1 §2）：

- ``commit``：提交 SHA——凭它调 Git 工具查看现状或复原历史，也使同文说明的不同提交互不混同；
- ``author``：作者 ident 原样（``Name <email>``，不做 mailmap——改写过的 ident 不是源内容）；
- ``authored_at`` / ``committed_at``：RFC 3339 UTC（作者时间与提交时间）。

大元素的确定性切分
------------------
元素对象不分块（core §3.2），一次提交的说明可能很长，因此超预算的说明按行边界切成多个连续
元素（类别不变、metadata 复制到每个片段）。切分预算是一个**固定常量**（``ELEMENT_BUDGET_BYTES``，
按元素对象 JCS 序列化后的 UTF-8 字节数计），而不是运行器协商出的限额：远端调大或调小限额都
不改变本连接器的切分，同一份源在任何远端都得到相同的 hash（契约 §2 确定性、§8.3 判据 1）。

``remote_limits`` 的角色是**守卫**而不是重新切分（契约 §6.3 把它交给插件做切分决策）：按固定
预算切分后元素仍超过远端 ``max_payload_bytes`` 的，属部署配置问题，如实上报为条目级失败——
静默按远端值再切一遍会得到同一源的第二套映射。守卫在 ``server`` 层实现（那里才有 remote_limits）。
"""

from __future__ import annotations

from typing import Any, cast

from dpe_hash import ElementObject, jcs

from dpe_git_connector.gitrepo import CommitRecord

__all__ = [
    "ELEMENT_BUDGET_BYTES",
    "commit_elements",
    "element_bytes",
    "split_element",
]

#: 单个元素对象（JCS 序列化后）的 UTF-8 字节预算。超出的说明按行边界切成多个连续元素
#: （类别与 metadata 不变），行尾字节留在前一块里。取 1 MiB 的原因：远小于各实现常见的
#: ``max_payload_bytes``（如 8 MiB）与 ``max_message_bytes``，同时大到不会让正常提交碎片化。
ELEMENT_BUDGET_BYTES = 1024 * 1024


def element_bytes(element: ElementObject) -> int:
    """元素对象 JCS 序列化后的 UTF-8 字节数（预算按这个量，与传输时的实际大小同口径）。

    ``jcs`` 是 dpe-hash 的公开入口（纯规范化，不做 DPE 的 null 键删除）——本模块构造的元素
    不带 null 键，两者一致。``server`` 的 ``remote_limits`` 守卫也用这个量。
    """
    return len(jcs(dict(element)).encode("utf-8"))


def commit_elements(record: CommitRecord) -> list[ElementObject]:
    """一次提交 → 一个或多个元素（说明超出预算时切分，metadata 复制到每个片段）。"""
    element: ElementObject = {
        "category": "NarrativeText",
        "text": record.message,
        "metadata": {
            "commit": record.sha,
            "author": record.author,
            "authored_at": record.authored_at,
            "committed_at": record.committed_at,
        },
    }
    return split_element(element)


def split_element(
    element: ElementObject, budget: int = ELEMENT_BUDGET_BYTES
) -> list[ElementObject]:
    """把超预算的文本元素切成连续片段（带同一组非文本字段），每个片段都不超过 ``budget`` 字节。

    切分在行边界进行，行尾字节留在前一块里；单个片段至少含一行——某行自身就超预算时该行独占
    一块（不再细分，留给 ``server`` 的 ``remote_limits`` 守卫上报）。非文本字段（如
    ``metadata``）原样复制到每个片段。

    大小按**元素级增量计费**：常数开销（键名、标点与固定字段的 JCS 字节）加上各行的
    ``jcs(line)``。每行的 JCS 转义只会让长度不小于原字节数（转义是变长的），所以这个估计
    是上界，切出的片段必定 ≤ ``budget``；同时每行只做一次序列化，整体 O(n)。
    """
    raw = element.get("text")
    if not isinstance(raw, str):
        return [element]
    text: str = raw
    if element_bytes(element) <= budget:
        return [element]

    def build(piece: str) -> ElementObject:
        clone: dict[str, Any] = {key: value for key, value in element.items() if key != "text"}
        clone["text"] = piece
        return cast(ElementObject, clone)

    # 元素骨架的开销：把 text 换成空串后 JCS 的长度（键名、标点与固定字段都在其中）
    overhead = element_bytes(build(""))
    pieces: list[ElementObject] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        line_size = len(jcs(line).encode("utf-8"))
        if current and overhead + size + line_size > budget:
            pieces.append(build("".join(current)))
            current, size = [], 0
        current.append(line)
        size += line_size
    if current:
        pieces.append(build("".join(current)))
    return pieces
