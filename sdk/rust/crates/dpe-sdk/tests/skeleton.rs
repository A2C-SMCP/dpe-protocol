//! 工程骨架冒烟测试：两个 crate 版本一致（锁步发布的对等约束）。

#[test]
fn same_version_as_dpe_hash() {
    assert_eq!(dpe_sdk::VERSION, dpe_hash::VERSION);
}
