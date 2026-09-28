#![allow(dead_code)]

use std::sync::Arc;

use dpe_protocol::push::DpePushClient;
use dpe_protocol::schema::{DocElement, DocMetadata, DocPage, Document, ElementCategory, ElementMetadata, FileType};
use dpe_protocol::testing::FakeRobotServer;

pub fn setup() -> (Arc<FakeRobotServer>, DpePushClient<Arc<FakeRobotServer>>) {
    let server = Arc::new(FakeRobotServer::default());
    let client = DpePushClient::new("http://robot.test", server.clone()).with_token("t0ken").with_robot_id("rid-1");
    (server, client)
}

pub fn make_doc(texts: &[&str]) -> Document {
    let mut elements = vec![DocElement::new(ElementCategory::Title, "员工手册")];
    elements.extend(texts.iter().map(|t| DocElement::new(ElementCategory::NarrativeText, *t)));
    elements.push(DocElement::new(ElementCategory::Image, "").with_metadata(ElementMetadata {
        image_url: Some("https://cdn/x.png".into()),
        image_mime_type: Some("image/png".into()),
        ..Default::default()
    }));
    let mut doc = Document::new(
        "dpe://acme/handbook".parse().unwrap(),
        FileType::Md,
        vec![DocPage::new(0, Some("第一章".into()), elements)],
    );
    doc.doc_metadata = DocMetadata { filename: Some("handbook.md".into()), ..Default::default() };
    doc
}

pub fn make_doc_at(uri: &str, texts: &[&str]) -> Document {
    let mut doc = make_doc(texts);
    doc.file_uri = uri.parse().unwrap();
    doc
}
