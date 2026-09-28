//! DPE 枚举类型：与 TFRobot 内核 `tfrobot.schema.document` 的取值逐字对齐。

use std::fmt;
use std::str::FromStr;

use serde::{Deserialize, Serialize};

macro_rules! wire_enum {
    ($(#[$meta:meta])* $name:ident { $($variant:ident => $value:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
        pub enum $name {
            $(#[serde(rename = $value)] $variant),+
        }

        impl $name {
            /// 线上取值（即内核枚举的 `.value`）。
            pub const fn as_str(&self) -> &'static str {
                match self { $(Self::$variant => $value),+ }
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl FromStr for $name {
            type Err = String;
            fn from_str(s: &str) -> Result<Self, Self::Err> {
                match s {
                    $($value => Ok(Self::$variant),)+
                    other => Err(format!(concat!("unknown ", stringify!($name), ": {}"), other)),
                }
            }
        }
    };
}

wire_enum! {
    /// 文档类型，取值等于内核 `TFFileType.value`（进入 `doc_hash`）。注意是 `"md"` 而不是 `"markdown"`。
    FileType {
        Bmp => "bmp", Csv => "csv", Doc => "doc", Docx => "docx", Eml => "eml", Epub => "epub",
        Heic => "heic", Html => "html", Jpg => "jpg", Json => "json", Md => "md", Msg => "msg",
        Ndjson => "ndjson", Odt => "odt", Org => "org", Pdf => "pdf", Png => "png", Ppt => "ppt",
        Pptx => "pptx", Rst => "rst", Rtf => "rtf", Tiff => "tiff", Tsv => "tsv", Txt => "txt",
        Wav => "wav", Xls => "xls", Xlsx => "xlsx", Xml => "xml", Zip => "zip",
        JavaRepo => "java_repo", PythonRepo => "python_repo", JavascriptRepo => "javascript_repo",
        TypescriptRepo => "typescript_repo", Unk => "unk", Empty => "empty", Tfchat => "tfchat",
        JiraProject => "jira_project", JiraIssue => "jira_issue",
    }
}

wire_enum! {
    /// Element 类别，即内核 `create_element` 的 discriminator 标签全集。
    ElementCategory {
        UncategorizedText => "UncategorizedText", CheckBox => "CheckBox", Formula => "Formula",
        CompositeElement => "CompositeElement", FigureCaption => "FigureCaption",
        NarrativeText => "NarrativeText", ListItem => "ListItem", Title => "Title", Address => "Address",
        EmailAddress => "EmailAddress", Image => "Image", PageBreak => "PageBreak", Table => "Table",
        TableChunk => "TableChunk", Header => "Header", Footer => "Footer", CodeSnippet => "CodeSnippet",
        PageNumber => "PageNumber", FormKeysValues => "FormKeysValues", Tfchat => "tfchat",
    }
}

// 枚举由宏生成，无法在变体上标注 #[default]
#[allow(clippy::derivable_impls)]
impl Default for ElementCategory {
    fn default() -> Self {
        Self::UncategorizedText
    }
}
