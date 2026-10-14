//! 实例定义（connector 契约 §4.4）与依赖清单的校验（`dpe_sdk::run::definition`）。
//!
//! 与 Python SDK 的 `test_run_definition.py` 同口径：逐项映射其用例，同一输入两侧的接受/拒绝
//! 结果与错误类别一致。文件末尾的 `remote_url_rules` / `uri_prefix_rules` / 深度边界等用例是
//! Rust 侧的额外钉子，期望值逐条对照 Python SDK 的实际行为核对过（不是从规范文本推的）。
//! `remote_url_rules` 只钉 §4.4 语义级的四条规则——CPython `urlsplit` 的加固细节（括号字面量
//! 格式、NFKC 等）不复刻，`remote.url` 的可判定文法与相应向量见 Issue #112。

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Map, Value};

use dpe_sdk::run::definition::{
    check_against_manifest, load_definition, parse_definition, resolve_command, ConflictAction,
};
use dpe_sdk::run::manifest::{parse_manifest, Manifest};

/// §4.4 的示例定义。
fn example() -> Value {
    json!({
        "definition_version": 1,
        "id": "acme-repo",
        "remote": {
            "url": "https://dpe.example.com/remotes/acme",
            "credential": {"env": "ACME_DPE_AUTHORIZATION"},
        },
        "uri_prefix": "git://acme/repo/",
        "plugin": {
            "manifest": "plugins/git/dpe-connector.json",
            "command": ["dpe-git-connector"],
            "env": {"GIT_SSL_CAINFO": "/etc/ssl/certs/ca.pem"},
        },
        "config": {"repo": "https://example.com/acme/repo.git"},
        "secrets": {"GIT_TOKEN": {"file": "/run/secrets/git_token"}},
    })
}

fn manifest() -> Manifest {
    parse_manifest(&json!({
        "manifest_version": 1,
        "name": "git-connector",
        "version": "0.1.0",
        "protocol_versions": ["dpe-connector/1"],
        "config_schema": {"type": "object"},
        "secrets": [{"name": "GIT_TOKEN", "required": false}],
    }))
    .expect("清单应有效")
}

fn mutated(mutate: impl FnOnce(&mut Map<String, Value>)) -> Value {
    let mut value = example();
    mutate(value.as_object_mut().unwrap());
    value
}

fn code_of(data: &Value, base_dir: &Path) -> &'static str {
    parse_definition(data, base_dir)
        .expect_err("定义应不合法")
        .code()
}

/// 临时目录（Drop 时清理）。与其它测试文件一致不引入额外依赖——`tempfile` 不在依赖面内。
struct TempDir(PathBuf);

impl TempDir {
    fn new(tag: &str) -> TempDir {
        use std::sync::atomic::{AtomicUsize, Ordering};
        static COUNTER: AtomicUsize = AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "dpe-sdk-definition-{}-{}-{}",
            tag,
            std::process::id(),
            COUNTER.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).expect("临时目录应可创建");
        TempDir(path)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn example_definition_is_valid() {
    let tmp = TempDir::new("example");
    let definition = parse_definition(&example(), tmp.path()).expect("定义应有效");
    assert_eq!(definition.base_dir(), tmp.path());
    assert_eq!(
        definition.manifest_path(),
        tmp.path().join("plugins/git/dpe-connector.json")
    );
    assert_eq!(
        definition.conflict_policy().commit(),
        ConflictAction::SourceWins
    );
    assert_eq!(
        definition.conflict_policy().delete(),
        ConflictAction::Report
    );
    check_against_manifest(&definition, &manifest()).expect("清单核对应通过");
}

#[test]
fn optional_members_and_runner_is_open() {
    let tmp = TempDir::new("optional");
    let mut data = mutated(|map| {
        map["remote"].as_object_mut().unwrap().remove("credential");
        map["plugin"].as_object_mut().unwrap().remove("env");
        map.remove("secrets");
    });
    data["runner"] = json!({"anything": {"nested": true}});
    data["conflict_policy"] = json!({"delete": "source_wins"});
    let definition = parse_definition(&data, tmp.path()).expect("定义应有效");
    assert!(definition.remote().credential().is_none());
    assert!(definition.plugin().env().is_empty());
    assert_eq!(
        definition.runner(),
        json!({"anything": {"nested": true}}).as_object().unwrap()
    );
    assert_eq!(
        definition.conflict_policy().commit(),
        ConflictAction::SourceWins
    );
    assert_eq!(
        definition.conflict_policy().delete(),
        ConflictAction::SourceWins
    );
}

#[test]
fn invalid_definitions_are_rejected() {
    let tmp = TempDir::new("invalid");
    let cases: Vec<(&str, Value)> = vec![
        (
            "顶层未定义成员",
            mutated(|map| {
                map.insert("extra".to_string(), json!(1));
            }),
        ),
        (
            "plugin 未定义成员",
            mutated(|map| {
                map["plugin"]["extra"] = json!(1);
            }),
        ),
        (
            "remote 未定义成员",
            mutated(|map| {
                map["remote"]["extra"] = json!(1);
            }),
        ),
        (
            "conflict_policy 未定义成员",
            mutated(|map| {
                map.insert("conflict_policy".to_string(), json!({"move": "report"}));
            }),
        ),
        (
            "conflict_policy 取值越界",
            mutated(|map| {
                map.insert("conflict_policy".to_string(), json!({"commit": "force"}));
            }),
        ),
        (
            "definition_version 不认识",
            mutated(|map| {
                map["definition_version"] = json!(2);
            }),
        ),
        (
            "definition_version 布尔",
            mutated(|map| {
                map["definition_version"] = json!(true);
            }),
        ),
        (
            "id 首字符连字符",
            mutated(|map| {
                map["id"] = json!("-leading-dash");
            }),
        ),
        (
            "id 超长",
            mutated(|map| {
                map["id"] = json!("a".repeat(129));
            }),
        ),
        (
            "id 含空格",
            mutated(|map| {
                map["id"] = json!("has space");
            }),
        ),
        (
            "remote.url 含 userinfo",
            mutated(|map| {
                map["remote"]["url"] = json!("https://user:pw@dpe.example.com/r");
            }),
        ),
        (
            "remote.url 方案非 http(s)",
            mutated(|map| {
                map["remote"]["url"] = json!("ftp://dpe.example.com/r");
            }),
        ),
        (
            "remote.url 无方案",
            mutated(|map| {
                map["remote"]["url"] = json!("dpe.example.com/r");
            }),
        ),
        (
            "来源引用两键",
            mutated(|map| {
                map["remote"]["credential"] = json!({"env": "A", "file": "b"});
            }),
        ),
        (
            "来源引用空对象",
            mutated(|map| {
                map["remote"]["credential"] = json!({});
            }),
        ),
        (
            "来源引用字面量凭证",
            mutated(|map| {
                map["remote"]["credential"] = json!("Bearer literal");
            }),
        ),
        (
            "uri_prefix 非规范化 scheme",
            mutated(|map| {
                map["uri_prefix"] = json!("Git://acme/repo/");
            }),
        ),
        (
            "uri_prefix 非不动点（%70 → p）",
            mutated(|map| {
                map["uri_prefix"] = json!("git://acme/re%70o/");
            }),
        ),
        (
            "uri_prefix 无 scheme",
            mutated(|map| {
                map["uri_prefix"] = json!("no scheme");
            }),
        ),
        (
            "command 空数组",
            mutated(|map| {
                map["plugin"]["command"] = json!([]);
            }),
        ),
        (
            "command[0] 为空",
            mutated(|map| {
                map["plugin"]["command"] = json!([""]);
            }),
        ),
        (
            "command 非数组",
            mutated(|map| {
                map["plugin"]["command"] = json!("dpe-git-connector");
            }),
        ),
        (
            "env 名以 DPE_ 开头",
            mutated(|map| {
                map["plugin"]["env"] = json!({"DPE_X": "1"});
            }),
        ),
        (
            "env 名与基础环境同名",
            mutated(|map| {
                map["plugin"]["env"] = json!({"PATH": "/bin"});
            }),
        ),
        (
            "env 名与 LC_* 同名",
            mutated(|map| {
                map["plugin"]["env"] = json!({"LC_ALL": "C"});
            }),
        ),
        (
            "env 名含等号",
            mutated(|map| {
                map["plugin"]["env"] = json!({"A=B": "1"});
            }),
        ),
        (
            "env 值非字符串",
            mutated(|map| {
                map["plugin"]["env"] = json!({"X": 1});
            }),
        ),
        (
            "config 为 null",
            mutated(|map| {
                map["config"] = Value::Null;
            }),
        ),
        (
            "config 为数组",
            mutated(|map| {
                map["config"] = json!([]);
            }),
        ),
        (
            "secrets 值含 value 成员",
            mutated(|map| {
                map["secrets"] = json!({"GIT_TOKEN": {"value": "literal"}});
            }),
        ),
        (
            "来源引用成员为 null",
            mutated(|map| {
                map["remote"]["credential"] = json!({"env": null});
            }),
        ),
        (
            "缺 definition_version",
            mutated(|map| {
                map.remove("definition_version");
            }),
        ),
        (
            "缺 id",
            mutated(|map| {
                map.remove("id");
            }),
        ),
        (
            "缺 remote",
            mutated(|map| {
                map.remove("remote");
            }),
        ),
        (
            "缺 plugin",
            mutated(|map| {
                map.remove("plugin");
            }),
        ),
        (
            "缺 config",
            mutated(|map| {
                map.remove("config");
            }),
        ),
        (
            "plugin.manifest 为空串",
            mutated(|map| {
                map["plugin"]["manifest"] = json!("");
            }),
        ),
        (
            "plugin.manifest 非字符串",
            mutated(|map| {
                map["plugin"]["manifest"] = json!(1);
            }),
        ),
        (
            "plugin.manifest 缺省",
            mutated(|map| {
                map["plugin"].as_object_mut().unwrap().remove("manifest");
            }),
        ),
        (
            "command 成员含 NUL",
            mutated(|map| {
                map["plugin"]["command"] = json!(["x\u{0}y"]);
            }),
        ),
        (
            "env 名含 NUL",
            mutated(|map| {
                map["plugin"]["env"] = json!({"A\u{0}B": "1"});
            }),
        ),
        (
            "env 值含 NUL",
            mutated(|map| {
                map["plugin"]["env"] = json!({"A": "v\u{0}"});
            }),
        ),
        (
            "runner 为非对象",
            mutated(|map| {
                map.insert("runner".to_string(), json!([1]));
            }),
        ),
        (
            "runner 为 null",
            mutated(|map| {
                map.insert("runner".to_string(), Value::Null);
            }),
        ),
        (
            "secrets 为非对象",
            mutated(|map| {
                map.insert("secrets".to_string(), json!([1]));
            }),
        ),
        (
            "remote.credential 为 null",
            mutated(|map| {
                map["remote"]["credential"] = Value::Null;
            }),
        ),
        (
            "conflict_policy 为 null",
            mutated(|map| {
                map.insert("conflict_policy".to_string(), Value::Null);
            }),
        ),
        ("顶层不是对象", json!([1, 2])),
    ];
    for (name, data) in cases {
        assert_eq!(code_of(&data, tmp.path()), "definition_invalid", "{name}");
    }
}

#[test]
fn secrets_must_not_share_remote_credential_source() {
    let tmp = TempDir::new("same-source");
    for (secret, remote) in [
        (json!({"env": "SAME"}), json!({"env": "SAME"})),
        (
            json!({"file": "creds/token"}),
            json!({"file": "./creds/../creds/token"}),
        ),
    ] {
        let data = mutated(|map| {
            map["remote"]["credential"] = remote.clone();
            map.insert("secrets".to_string(), json!({"GIT_TOKEN": secret.clone()}));
        });
        let failure = parse_definition(&data, tmp.path()).expect_err("同源应被拒绝");
        assert_eq!(failure.code(), "definition_invalid");
        assert!(
            failure.message().contains("GIT_TOKEN"),
            "{}",
            failure.message()
        );
    }
}

#[test]
fn failure_messages_do_not_echo_input_values() {
    // §4.3「不外泄」：消息会进入日志与 §7.4 轮报告，不得回显 URL（可能含 userinfo 密码）与
    // 凭证字面量（恰是这条校验要拦的误写形态）；Python 侧的消息同样不含输入值。
    let tmp = TempDir::new("messages");
    let failure = parse_definition(
        &mutated(|map| {
            map["remote"]["url"] = json!("https://user:s3cret-token@host/r");
        }),
        tmp.path(),
    )
    .expect_err("userinfo 应被拒绝");
    assert!(
        !failure.message().contains("s3cret-token"),
        "{}",
        failure.message()
    );
    let failure = parse_definition(
        &mutated(|map| {
            map["remote"]["credential"] = json!("Bearer eyJhbGciOiJIUzI1NiJ9.sekret");
        }),
        tmp.path(),
    )
    .expect_err("字面量凭证应被拒绝");
    assert!(
        !failure.message().contains("eyJhbGciOiJIUzI1NiJ9"),
        "{}",
        failure.message()
    );
    let failure = parse_definition(
        &mutated(|map| {
            map.insert(
                "secrets".to_string(),
                json!({"A": {"env": {"ref": "sekret"}}}),
            );
        }),
        tmp.path(),
    )
    .expect_err("来源引用嵌套对象应被拒绝");
    assert!(
        !failure.message().contains("sekret"),
        "{}",
        failure.message()
    );
}

#[test]
fn secret_env_reference_never_shares_file_source() {
    // 一侧 env、一侧 file 永远不同源（§4.3 的同源只在同类引用之间判定）
    let tmp = TempDir::new("env-file");
    fs::write(tmp.path().join("token"), "t").unwrap();
    let data = mutated(|map| {
        map["remote"]["credential"] = json!({"file": "token"});
        map.insert(
            "secrets".to_string(),
            json!({"GIT_TOKEN": {"env": "GIT_TOKEN"}}),
        );
    });
    parse_definition(&data, tmp.path()).expect("env 引用与 file 引用不同源");
}

#[test]
fn id_length_boundary() {
    let tmp = TempDir::new("id");
    parse_definition(
        &mutated(|map| {
            map["id"] = json!("a".repeat(128));
        }),
        tmp.path(),
    )
    .expect("128 字符应接受");
}

#[cfg(unix)]
#[test]
fn secret_file_through_symlink_is_same_source() {
    let tmp = TempDir::new("symlink");
    fs::write(tmp.path().join("token"), "t").unwrap();
    std::os::unix::fs::symlink(tmp.path().join("token"), tmp.path().join("alias")).unwrap();
    let data = mutated(|map| {
        map["remote"]["credential"] = json!({"file": "token"});
        map.insert(
            "secrets".to_string(),
            json!({"GIT_TOKEN": {"file": "alias"}}),
        );
    });
    assert_eq!(code_of(&data, tmp.path()), "definition_invalid");
}

#[test]
fn check_against_manifest_cases() {
    let tmp = TempDir::new("check");
    let manifest = manifest();

    let undeclared = mutated(|map| {
        map.insert("secrets".to_string(), json!({"OTHER": {"env": "X"}}));
    });
    let definition = parse_definition(&undeclared, tmp.path()).expect("定义本身应有效");
    let failure = check_against_manifest(&definition, &manifest).expect_err("未声明应被拒绝");
    assert_eq!(failure.code(), "definition_invalid");

    let clash = mutated(|map| {
        map["plugin"]["env"] = json!({"GIT_TOKEN": "x"});
    });
    let definition = parse_definition(&clash, tmp.path()).expect("定义本身应有效");
    assert_eq!(
        check_against_manifest(&definition, &manifest)
            .expect_err("与凭证同名应被拒绝")
            .code(),
        "definition_invalid"
    );
}

#[test]
fn load_definition_uses_file_directory_as_base() {
    let tmp = TempDir::new("load");
    let dir = tmp.path().join("instances");
    fs::create_dir(&dir).unwrap();
    let path = dir.join("acme.json");
    fs::write(&path, serde_json::to_vec(&example()).unwrap()).unwrap();

    let definition = load_definition(&path).expect("定义应有效");
    assert_eq!(definition.base_dir(), dir.as_path());

    let missing = load_definition(&tmp.path().join("absent.json")).expect_err("缺失文件");
    assert_eq!(missing.code(), "definition_invalid");
}

#[cfg(unix)]
#[test]
fn resolve_command_cases() {
    use std::os::unix::fs::PermissionsExt;

    let tmp = TempDir::new("resolve");
    let script = tmp.path().join("bin/plugin");
    fs::create_dir(tmp.path().join("bin")).unwrap();
    fs::write(&script, "#!/bin/sh\n").unwrap();
    let mut permissions = fs::metadata(&script).unwrap().permissions();
    permissions.set_mode(0o755);
    fs::set_permissions(&script, permissions).unwrap();

    let relative = parse_definition(
        &mutated(|map| {
            map["plugin"]["command"] = json!(["bin/plugin", "--flag"]);
        }),
        tmp.path(),
    )
    .unwrap();
    assert_eq!(
        resolve_command(&relative, &BTreeMap::new()).unwrap(),
        vec![script.clone().into_os_string(), "--flag".into()]
    );

    let on_path = parse_definition(
        &mutated(|map| {
            map["plugin"]["command"] = json!(["plugin"]);
        }),
        tmp.path(),
    )
    .unwrap();
    let path = BTreeMap::from([(
        "PATH".to_string(),
        tmp.path().join("bin").display().to_string(),
    )]);
    assert_eq!(
        resolve_command(&on_path, &path).unwrap(),
        vec![script.clone().into_os_string()]
    );
    // 查找用插件环境的 PATH，不用运行器自身的
    let wrong = BTreeMap::from([("PATH".to_string(), tmp.path().display().to_string())]);
    assert_eq!(
        resolve_command(&on_path, &wrong)
            .expect_err("不在 PATH 中")
            .code(),
        "definition_invalid"
    );
    // 空 PATH 与缺 PATH 都视为找不到（shutil.which 同口径）
    assert!(resolve_command(
        &on_path,
        &BTreeMap::from([("PATH".to_string(), String::new())])
    )
    .is_err());
    assert!(resolve_command(&on_path, &BTreeMap::new()).is_err());
    // 空条目按当前目录解析（`os.path.join('', cmd)` = `cmd`）：`:{dir}` 的首个空条目在当前
    // 目录找不到后继续命中 {dir}（`shutil.which` 的 `PATH=':'` 会在当前目录查找）。
    // 注：「空条目命中当前目录」这一支有意不加断言——进程内不能安全 chdir（会污染并行的
    // 测试），期望值按 `shutil.which` 实测核对。
    let leading_empty = BTreeMap::from([(
        "PATH".to_string(),
        format!(":{}", tmp.path().join("bin").display()),
    )]);
    assert_eq!(
        resolve_command(&on_path, &leading_empty).unwrap(),
        vec![script.clone().into_os_string()]
    );

    let missing_relative = parse_definition(
        &mutated(|map| {
            map["plugin"]["command"] = json!(["./bin/missing"]);
        }),
        tmp.path(),
    )
    .unwrap();
    assert_eq!(
        resolve_command(&missing_relative, &BTreeMap::new())
            .expect_err("相对路径缺失")
            .code(),
        "definition_invalid"
    );

    // 无执行位的文件与目录都不是可执行文件
    let noexec = tmp.path().join("noexec");
    fs::write(&noexec, "x").unwrap();
    let mut permissions = fs::metadata(&noexec).unwrap().permissions();
    permissions.set_mode(0o644);
    fs::set_permissions(&noexec, permissions).unwrap();
    let noexec_definition = parse_definition(
        &mutated(|map| {
            map["plugin"]["command"] = json!(["noexec"]);
        }),
        tmp.path(),
    )
    .unwrap();
    assert!(resolve_command(&noexec_definition, &path).is_err());
    let dir_definition = parse_definition(
        &mutated(|map| {
            map["plugin"]["command"] = json!(["bin"]);
        }),
        tmp.path(),
    )
    .unwrap();
    assert!(resolve_command(&dir_definition, &path).is_err());
}

#[test]
fn out_of_range_config_numbers() {
    let tmp = TempDir::new("numbers");
    for config in [
        json!({"n": 9007199254740992u64}),
        json!({"n": [-9007199254740992i64]}),
    ] {
        let data = mutated(|map| {
            map["config"] = config.clone();
        });
        assert_eq!(code_of(&data, tmp.path()), "definition_invalid", "{config}");
    }
    // 界内数值照常接受
    let ok = mutated(|map| {
        map["config"] = json!({"n": 9007199254740991u64, "f": 1.5});
    });
    parse_definition(&ok, tmp.path()).expect("界内数值应接受");
}

#[test]
fn load_definition_rejects_out_of_range_literals() {
    let tmp = TempDir::new("literals");
    let base = serde_json::to_string(&mutated(|map| {
        map.insert("config".to_string(), json!({}));
    }))
    .unwrap();
    for literal in ["1e400", &"1".repeat(5000)] {
        let text = base.replace("\"config\":{}", &format!("\"config\":{{\"n\":{literal}}}"));
        let path = tmp.path().join("def.json");
        fs::write(&path, text).unwrap();
        let failure = load_definition(&path).expect_err("越界字面量应被拒绝");
        assert_eq!(failure.code(), "definition_invalid", "{literal}");
    }
}

#[test]
fn load_definition_rejects_json_violations() {
    // 与 Python SDK 的 `load_json_file` 同口径：I-JSON 违例与非 UTF-8 都是 definition_invalid
    let tmp = TempDir::new("ijson");
    let path = tmp.path().join("def.json");
    let valid = serde_json::to_string(&example()).unwrap();
    assert!(valid.contains("\"id\":\"acme-repo\""), "{valid}");
    let texts = [
        // 重复键
        valid.replacen(
            "\"id\":\"acme-repo\"",
            "\"id\":\"acme-repo\",\"id\":\"x\"",
            1,
        ),
        // 孤立代理项（I-JSON 禁止）
        valid.replacen("\"id\":\"acme-repo\"", "\"id\":\"acme-repo\\ud800\"", 1),
        // BOM
        format!("\u{feff}{valid}"),
        // 尾部多余字符
        format!("{valid}{{}}"),
    ];
    for text in texts {
        fs::write(&path, &text).unwrap();
        let failure = load_definition(&path).expect_err("I-JSON 违例应被拒绝");
        assert_eq!(failure.code(), "definition_invalid", "{text}");
    }
    // 非 UTF-8 字节
    fs::write(&path, b"\xff\xfe{}").unwrap();
    assert_eq!(
        load_definition(&path).expect_err("非 UTF-8").code(),
        "definition_invalid"
    );
}

#[cfg(unix)]
#[test]
fn secret_file_in_symlink_loop_is_definition_invalid() {
    let tmp = TempDir::new("loop");
    std::os::unix::fs::symlink(tmp.path().join("loop_b"), tmp.path().join("loop_a")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("loop_a"), tmp.path().join("loop_b")).unwrap();
    let data = mutated(|map| {
        map["remote"]["credential"] = json!({"file": "loop_a/token"});
        map.insert(
            "secrets".to_string(),
            json!({"GIT_TOKEN": {"file": "loop_b/x"}}),
        );
    });
    let failure = parse_definition(&data, tmp.path()).expect_err("符号链接环应被拒绝");
    assert_eq!(failure.code(), "definition_invalid");
}

// ---------------------------------------------------------------------------
// Rust 侧的额外钉子：期望值逐条对照 Python SDK 的实际行为核对（见 PR 说明）
// ---------------------------------------------------------------------------

#[test]
fn remote_url_rules() {
    let tmp = TempDir::new("url");
    let accepted = [
        "https://dpe.example.com/remotes/acme",
        "HTTP://dpe.example.com/r", // 方案大小写不敏感（RFC 3986 §3.1）
        "https://dpe.example.com:8080/r", // 带端口
        "http://h",
        "http://h/x?q=1",
        "http://h/x#frag",
        "http://[::1]:8080/x", // RFC 3986 的 IP-literal 只做结构切分，不校验字面量（#112）
        "http://x/y@z",        // `@` 在路径而非 netloc
    ];
    for url in accepted {
        assert!(
            parse_definition(
                &mutated(|map| {
                    map["remote"]["url"] = json!(url);
                }),
                tmp.path()
            )
            .is_ok(),
            "{url}"
        );
    }
    // 语义级拒绝（§4.4 的四条规则；文法收紧见 #112）
    let rejected = [
        "https://",                // authority 为空
        "https:///path",           // authority 为空
        "https://:8080/r",         // host 为空
        "//h/x",                   // 相对 URL（无 scheme）
        "dpe.example.com/r",       // 相对 URL（无 scheme）
        "mailto:x@y",              // 非 http(s)，且无 authority
        "ftp://dpe.example.com/r", // 方案非 http(s)
        "",                        // 空串
        "http://@h/x",             // userinfo
        "http://u:p@h/x",          // userinfo
        "http:h/x",                // 无 "//"，缺 authority
    ];
    for url in rejected {
        assert_eq!(
            code_of(
                &mutated(|map| {
                    map["remote"]["url"] = json!(url);
                }),
                tmp.path()
            ),
            "definition_invalid",
            "{url}"
        );
    }
}

#[test]
fn uri_prefix_rules() {
    let tmp = TempDir::new("prefix");
    let accepted = [
        "git://acme/repo/",
        "git://acme/repo",
        "git://acme/repo/../x/", // 不做点段移除（core §1.1）
        "git://acme/a%2Fb/",     // 三元的 hex 大写即不动点
        "git://acme/a%20b/",
        "git:",
        "git:/x",
        "urn:isbn:0451450523",
        "git://acme/~x/",
        "git://acme/repo/?q=1",
    ];
    for prefix in accepted {
        assert!(
            parse_definition(
                &mutated(|map| {
                    map["uri_prefix"] = json!(prefix);
                }),
                tmp.path()
            )
            .is_ok(),
            "{prefix}"
        );
    }
    let rejected = [
        "Git://acme/repo/",  // scheme 大小写
        "git://ACME/repo/",  // host 大小写
        "git://acme/a%2fb/", // hex 小写不是不动点
        "git://acme/%7Ex/",  // %7E 解码为 unreserved 的 `~`
        "git://acme/a b/",   // 非法字符
        "git://例子/repo/",  // 非 ASCII
        "no scheme",
        "",
    ];
    for prefix in rejected {
        assert_eq!(
            code_of(
                &mutated(|map| {
                    map["uri_prefix"] = json!(prefix);
                }),
                tmp.path()
            ),
            "definition_invalid",
            "{prefix}"
        );
    }
}

#[test]
fn definition_json_depth_bound() {
    let tmp = TempDir::new("depth");
    fn nested(levels: usize) -> Value {
        let mut node = json!({});
        for _ in 0..levels {
            // move 子值，不经 `json!` 插值递归复制整棵子树（#166）
            node = Value::Object(Map::from_iter([("a".to_owned(), node)]));
        }
        node
    }
    // §4.4 深度 ≤ 64，计数口径见 core §2.8 第 0 步；定义对象本身计 1 层，
    // config 的值再包一层，故 config 嵌套 levels 时整值深度 = levels + 2
    let ok = mutated(|map| {
        map.insert("config".to_string(), nested(62));
    });
    assert_eq!(dpe_hash::nesting_depth(&ok, None), 64);
    parse_definition(&ok, tmp.path()).expect("深度 64 应接受");

    let over = mutated(|map| {
        map.insert("config".to_string(), nested(63));
    });
    assert_eq!(dpe_hash::nesting_depth(&over, None), 65);
    assert_eq!(code_of(&over, tmp.path()), "definition_invalid");

    // 文本入口同判：深度远超上界的文件在读取阶段即被拒绝
    let path = tmp.path().join("deep.json");
    fs::write(
        &path,
        serde_json::to_vec(&mutated(|map| {
            map.insert("config".to_string(), nested(100));
        }))
        .unwrap(),
    )
    .unwrap();
    assert_eq!(
        load_definition(&path).expect_err("超深").code(),
        "definition_invalid"
    );
}

/// `same_source` 的边界形状（经 `parse_definition` 的同源检查观察）：悬空符号链接、
/// 经符号链接目录的 `..`、中间段不是目录——与 Python `Path.resolve()` 同结论。
#[cfg(unix)]
#[test]
fn same_source_path_shapes() {
    let tmp = TempDir::new("shapes");
    fs::create_dir(tmp.path().join("real")).unwrap();
    fs::write(tmp.path().join("real/token"), "t").unwrap();
    fs::create_dir(tmp.path().join("other")).unwrap();
    fs::write(tmp.path().join("other/token"), "t").unwrap();
    std::os::unix::fs::symlink(tmp.path().join("real"), tmp.path().join("link")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("nodir/token"), tmp.path().join("dangling"))
        .unwrap();
    std::os::unix::fs::symlink("./nodir/../nodir/token", tmp.path().join("dangling2")).unwrap();
    std::os::unix::fs::symlink(tmp.path().join("real/token"), tmp.path().join("file_link"))
        .unwrap();

    let same = |secret: Value, remote: Value| {
        let data = mutated(|map| {
            map["remote"]["credential"] = remote;
            map.insert("secrets".to_string(), json!({"GIT_TOKEN": secret}));
        });
        parse_definition(&data, tmp.path()).is_err()
    };
    // 悬空符号链接解析到同一目标路径
    assert!(same(
        json!({"file": "dangling"}),
        json!({"file": "dangling2"})
    ));
    // 经符号链接目录的 `..`：`link/../other/token` 解析为 other/token，不是 real/token
    assert!(!same(
        json!({"file": "other/token"}),
        json!({"file": "link/../real/token"})
    ));
    assert!(same(
        json!({"file": "other/token"}),
        json!({"file": "link/../other/token"})
    ));
    // 中间段不是目录：按字面保留，不视为同源
    assert!(!same(
        json!({"file": "file_link"}),
        json!({"file": "file_link/../real/token"})
    ));
    // 相对路径与绝对路径同源
    assert!(same(
        json!({"file": "real/token"}),
        json!({"file": tmp.path().join("real/token").display().to_string()})
    ));
}
