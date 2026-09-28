"""DPE 枚举类型 —— 与 TFRobot 内核 ``tfrobot.schema.document`` 的取值逐字对齐。

这里只镜像**线上（wire）可见的取值**，不镜像内核的 partitioner / MIME 等运行期属性。
"""

from enum import StrEnum


class FileType(StrEnum):
    """文档类型，取值等于内核 ``TFFileType`` 的 ``.value``（进入 ``doc_hash``）。

    注意是 ``"md"`` 而不是 ``"markdown"``。
    """

    BMP = "bmp"
    CSV = "csv"
    DOC = "doc"
    DOCX = "docx"
    EML = "eml"
    EPUB = "epub"
    HEIC = "heic"
    HTML = "html"
    JPG = "jpg"
    JSON = "json"
    MD = "md"
    MSG = "msg"
    NDJSON = "ndjson"
    ODT = "odt"
    ORG = "org"
    PDF = "pdf"
    PNG = "png"
    PPT = "ppt"
    PPTX = "pptx"
    RST = "rst"
    RTF = "rtf"
    TIFF = "tiff"
    TSV = "tsv"
    TXT = "txt"
    WAV = "wav"
    XLS = "xls"
    XLSX = "xlsx"
    XML = "xml"
    ZIP = "zip"
    JAVA_REPO = "java_repo"
    PYTHON_REPO = "python_repo"
    JAVASCRIPT_REPO = "javascript_repo"
    TYPESCRIPT_REPO = "typescript_repo"
    UNK = "unk"
    EMPTY = "empty"
    TFCHAT = "tfchat"
    JIRA_PROJECT = "jira_project"
    JIRA_ISSUE = "jira_issue"


class ElementCategory(StrEnum):
    """Element 类别，即内核 ``create_element`` 的 discriminator 标签全集。"""

    UNCATEGORIZED_TEXT = "UncategorizedText"
    CHECK_BOX = "CheckBox"
    FORMULA = "Formula"
    COMPOSITE_ELEMENT = "CompositeElement"
    FIGURE_CAPTION = "FigureCaption"
    NARRATIVE_TEXT = "NarrativeText"
    LIST_ITEM = "ListItem"
    TITLE = "Title"
    ADDRESS = "Address"
    EMAIL_ADDRESS = "EmailAddress"
    IMAGE = "Image"
    PAGE_BREAK = "PageBreak"
    TABLE = "Table"
    TABLE_CHUNK = "TableChunk"
    HEADER = "Header"
    FOOTER = "Footer"
    CODE_SNIPPET = "CodeSnippet"
    PAGE_NUMBER = "PageNumber"
    FORM_KEYS_VALUES = "FormKeysValues"
    TFCHAT = "tfchat"
