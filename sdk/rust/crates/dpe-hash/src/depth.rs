//! 对象的嵌套深度（core §2.8 第 0 步第三项，Issue #94）。
//!
//! 计数口径：标量记 0；对象或数组记 1 + 其子值的最大深度；空容器记 1（`{}` 记 1、
//! `{"a": {"b": [1]}}` 记 3）。页的 `elements` 与文档的 `pages` 按子 hash 列表计：列表自身
//! 记 1，其每一项不计入（线上原像中它们本是不可再分的 hash 串）——展开视图中内联给出的子对象
//! 同样不计入，它们作为独立对象、以自身为根校验。
//!
//! 迭代遍历，不消耗调用栈：远超上界的输入（解析器防护上限之内的）在这里干净地判成
//! `DPE_VALIDATION`，不落进栈溢出或恐慌。

use serde_json::Value;

use crate::constants::MAX_NESTING_DEPTH;
use crate::error::{Error, ErrorKind, Result};

/// JSON 值的嵌套深度（core §2.8 第 0 步，全协议唯一的定义，connector 契约 §4.1 / §4.4 也引用
/// 本口径）；`child_field` 是子 hash 列表字段（页的 `elements`、文档的 `pages`），`None` 表示
/// 没有（元素对象，以及清单、实例定义等非 DPE 载体）。
pub fn nesting_depth(value: &Value, child_field: Option<&str>) -> u32 {
    let mut depth = 0u32;
    let mut stack: Vec<(&Value, u32, bool)> = vec![(value, 0, true)];
    while let Some((item, ancestors, is_root)) = stack.pop() {
        match item {
            Value::Array(items) => {
                depth = depth.max(ancestors + 1);
                stack.extend(items.iter().map(|child| (child, ancestors + 1, false)));
            }
            Value::Object(map) => {
                depth = depth.max(ancestors + 1);
                for (key, child) in map {
                    if is_root && Some(key.as_str()) == child_field && child.is_array() {
                        depth = depth.max(ancestors + 2); // 子 hash 列表：列表自身记 1
                    } else {
                        stack.push((child, ancestors + 1, false));
                    }
                }
            }
            _ => depth = depth.max(ancestors),
        }
    }
    depth
}

/// 第 0 步的嵌套深度判定（core §2.8）：超出 [`MAX_NESTING_DEPTH`] 返回 `DPE_VALIDATION`、
/// 位置为对象自身。每个公开入口在处理值之前都要经过它，文本入口与对象级入口结论一致。
pub(crate) fn check_depth(value: &Value, child_field: Option<&str>) -> Result<()> {
    if nesting_depth(value, child_field) > MAX_NESTING_DEPTH {
        return Err(Error::new(
            ErrorKind::Validation,
            format!("对象的嵌套深度超过上限 {MAX_NESTING_DEPTH} 层（core §2.8 第 0 步）"),
            "",
        ));
    }
    Ok(())
}
