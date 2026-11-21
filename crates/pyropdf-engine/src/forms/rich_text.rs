//! Bounded XHTML content of a variable text field (ISO 32000-2 §12.7.4).

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use quick_xml::XmlVersion;
use quick_xml::events::{BytesStart, Event};
use quick_xml::name::{Namespace, NamespaceResolver, ResolveResult};
use quick_xml::reader::NsReader;

use super::{WidgetError, malformed};

const XHTML: &str = "http://www.w3.org/1999/xhtml";

/// A formatting scope in the PDF/XFA subset of XHTML.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Element {
    /// The root of a rich text value.
    Body,
    /// A paragraph.
    Paragraph,
    /// A span with inline CSS declarations.
    Span,
    /// An explicit line break.
    Break,
    /// Bold text.
    Bold,
    /// Italic text.
    Italic,
    /// Underlined text.
    Underline,
    /// Raised text.
    Superscript,
    /// Lowered text.
    Subscript,
}

impl Element {
    fn from_name(name: &str) -> Result<Self, WidgetError> {
        match name {
            "body" => Ok(Self::Body),
            "p" => Ok(Self::Paragraph),
            "span" => Ok(Self::Span),
            "br" => Ok(Self::Break),
            "b" | "strong" => Ok(Self::Bold),
            "i" | "em" => Ok(Self::Italic),
            "u" => Ok(Self::Underline),
            "sup" => Ok(Self::Superscript),
            "sub" => Ok(Self::Subscript),
            _ => Err(malformed("unsupported XHTML element in a rich text field")),
        }
    }
}

/// Text and scoped formatting in document order. Each style declaration
/// occurs once even when a long scope contains many short spans.
#[derive(Debug, PartialEq, Eq)]
pub enum Run {
    /// Enter an element, with its optional CSS style declaration.
    Open(Element, Option<String>),
    /// Its decoded character data, including whitespace.
    Text(String),
    /// Leave an element, restoring its parent's style.
    Close(Element),
}

fn xml_text(text: &str) -> bool {
    text.chars().all(|c| matches!(c, '\t' | '\r' | '\n' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..='\u{10ffff}'))
}

fn invalid() -> WidgetError {
    malformed("malformed rich text XHTML")
}

fn style(
    start: &BytesStart<'_>,
    ns: &NamespaceResolver,
    budget: &Budget,
) -> Result<Option<String>, WidgetError> {
    let mut css = None;
    for (index, attribute) in start.attributes().enumerate() {
        if index >= 128 {
            return Err(malformed("too many attributes on a rich text element"));
        }
        budget.poll()?;
        let attribute = attribute.map_err(|_| invalid())?;
        if matches!(
            ns.resolve_attribute(attribute.key).0,
            ResolveResult::Unknown(_)
        ) {
            return Err(invalid());
        }
        let value = attribute
            .normalized_value(XmlVersion::Implicit1_0)
            .map_err(|_| invalid())?;
        if !xml_text(&value) {
            return Err(invalid());
        }
        if attribute.key.0 == "style" {
            css = Some(value.into_owned());
        }
    }
    Ok(css)
}

/// Read a single XHTML body without loading auxiliary documents or expanding
/// declared entities. The input is the text already decoded from its PDF
/// string or stream. Malformed input never returns a partial result.
pub fn parse(xml: &str, budget: &Budget) -> Result<Vec<Run>, WidgetError> {
    budget.poll()?;
    let size = xml.len() as u64;
    budget.check(LimitCode::FormFieldInputSize, size)?;
    // Text, styles and the tokenizer's temporary markup and namespace buffers
    // are bounded by the UTF-8 input. The event and depth vectors charge separately.
    budget.charge(LimitCode::MemorySize, size.saturating_mul(4))?;
    let mut reader = NsReader::from_str(xml);
    reader.config_mut().expand_empty_elements = true;
    reader.config_mut().check_comments = true;
    let mut runs = BudgetVec::new(budget);
    let mut stack = BudgetVec::new(budget);
    let mut seen_body = false;
    let mut first = true;
    let depth_limit = budget
        .limit(LimitCode::ObjectDepth)
        .unwrap_or(super::MAX_PARENTS as u64);
    loop {
        budget.poll()?;
        let (ns, event) = reader.read_resolved_event().map_err(|_| invalid())?;
        let first_event = std::mem::take(&mut first);
        let text = match event {
            Event::Start(start) => {
                if ns != ResolveResult::Bound(Namespace(XHTML)) {
                    return Err(malformed("expected an XHTML element in the rich text body"));
                }
                let element = Element::from_name(start.local_name().as_ref())?;
                if stack.is_empty() && (seen_body || element != Element::Body)
                    || !stack.is_empty() && element == Element::Body
                {
                    return Err(invalid());
                }
                seen_body = true;
                let depth = stack.len() as u64 + 1;
                if depth > depth_limit {
                    return Err(pyropdf_store::LimitExceeded::over(
                        LimitCode::ObjectDepth,
                        depth_limit,
                        depth,
                    )
                    .into());
                }
                stack.try_push(element)?;
                runs.try_push(Run::Open(
                    element,
                    style(&start, reader.resolver(), budget)?,
                ))?;
                continue;
            }
            Event::End(_) => {
                runs.try_push(Run::Close(stack.pop().ok_or_else(invalid)?))?;
                continue;
            }
            Event::Text(text) => text.xml_content(XmlVersion::Implicit1_0).into_owned(),
            Event::CData(data) if !stack.is_empty() => {
                data.xml_content(XmlVersion::Implicit1_0).into_owned()
            }
            Event::GeneralRef(reference) if !stack.is_empty() => {
                if let Some(character) = reference.resolve_char_ref().map_err(|_| invalid())? {
                    character.to_string()
                } else {
                    quick_xml::escape::resolve_predefined_entity(reference.as_ref())
                        .ok_or_else(invalid)?
                        .to_owned()
                }
            }
            Event::Eof if seen_body && stack.is_empty() => return Ok(runs.into_vec()),
            Event::Decl(decl) if first_event && decl.version().is_ok_and(|v| v == "1.0") => {
                continue;
            }
            Event::Comment(_) | Event::PI(_) => continue,
            _ => return Err(invalid()),
        };
        if !xml_text(&text) {
            return Err(invalid());
        }
        if stack.is_empty() {
            if !text
                .bytes()
                .all(|b| matches!(b, b' ' | b'\t' | b'\n' | b'\r'))
            {
                return Err(invalid());
            }
        } else {
            runs.try_push(Run::Text(text))?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn namespaced_scopes_and_decoded_text_preserve_document_order() {
        let xml = r#"<h:body xmlns:h="http://www.w3.org/1999/xhtml"><h:p style="text-align:center"><h:b>X<h:span style="font-family:A&amp;B">Y&amp;&#x1f600;</h:span></h:b><h:br/></h:p><h:p> Z</h:p></h:body>"#;
        use Element::{Body, Bold, Break, Paragraph, Span};
        use Run::{Close, Open, Text};
        assert_eq!(
            parse(xml, &Budget::default()).unwrap(),
            [
                Open(Body, None),
                Open(Paragraph, Some("text-align:center".into())),
                Open(Bold, None),
                Text("X".into()),
                Open(Span, Some("font-family:A&B".into())),
                Text("Y".into()),
                Text("&".into()),
                Text("😀".into()),
                Close(Span),
                Close(Bold),
                Open(Break, None),
                Close(Break),
                Close(Paragraph),
                Open(Paragraph, None),
                Text(" Z".into()),
                Close(Paragraph),
                Close(Body)
            ]
        );
    }

    #[test]
    fn malformed_or_external_markup_fails_before_returning_runs() {
        for xml in [
            "",
            "<body/>",
            "<!DOCTYPE x><body/>",
            "<h:body/>",
            "<body xmlns='http://www.w3.org/1999/xhtml' x:style='red'/>",
            "<body xmlns='http://www.w3.org/1999/xhtml'>&#x1;</body>",
            "<?xml version='1.0'?><?xml version='1.0'?><body xmlns='http://www.w3.org/1999/xhtml'/>",
            "<body xmlns='http://www.w3.org/1999/xhtml'>&custom;</body>",
            "<body xmlns='http://www.w3.org/1999/xhtml'><p></body>",
            "<body xmlns='http://www.w3.org/1999/xhtml'><p>",
            "<body xmlns='http://www.w3.org/1999/xhtml'/><body xmlns='http://www.w3.org/1999/xhtml'/>",
            "<body xmlns='http://www.w3.org/1999/xhtml' style='&unknown;'/>",
        ] {
            assert!(
                matches!(
                    parse(xml, &Budget::default()),
                    Err(WidgetError::Malformed(_))
                ),
                "{xml}"
            );
        }
    }

    #[test]
    fn configured_byte_depth_and_memory_limits_are_typed() {
        let xml = "<body xmlns='http://www.w3.org/1999/xhtml'><p>x</p></body>";
        for code in [
            LimitCode::ObjectDepth,
            LimitCode::FormFieldInputSize,
            LimitCode::MemorySize,
        ] {
            let budget = Budget::default().with_limit(code, Some(1));
            let result = parse(xml, &budget);
            assert!(
                matches!(result, Err(WidgetError::Store(ref e)) if matches!(e.kind(), pyropdf_store::ErrorKind::Limit(refusal) if refusal.code() == code)),
                "{result:?}"
            );
        }
    }
}
