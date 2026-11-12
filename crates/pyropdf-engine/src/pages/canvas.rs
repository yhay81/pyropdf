//! Placing a page or image on a canvas (DESIGN.md sections 7 and 8).
//!
//! A placement captures one source version. Its geometry, content and
//! resources come from that version, even if the page changes before the
//! destination writes its Form XObject.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, PoisonError};

use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Reads;
use pyropdf_store::pages::{Page, PageIndex};
use pyropdf_store::refs::copy_for_edit;
use pyropdf_store::save::deflate;
use pyropdf_store::value::{Bytes, Dict, Name, Reference, Stream, Value};
use pyropdf_store::version::{ObjectStamps, Transaction, Version};

use super::{
    PageError, Result, geometry::space, import, sources, target, tree::Root, with_savepoint,
};
use crate::content::PageContent;
use crate::display::{Box4, Display, display_box};

/// A page and the immutable version and index it was found in.
pub struct SourcePage {
    version: Arc<Version>,
    index: Arc<PageIndex>,
    at: usize,
    reference: Reference,
}

impl SourcePage {
    /// Finds the page in this index. A removed page has no source.
    pub fn new(version: Arc<Version>, index: Arc<PageIndex>, reference: Reference) -> Option<Self> {
        let at = index.index_of(reference)?;
        Some(Self {
            version,
            index,
            at,
            reference,
        })
    }

    fn entry(&self) -> &Page {
        &self.index.pages()[self.at]
    }

    fn resources(&self) -> &Value {
        static EMPTY: Value = Value::Dict(Dict::new());
        self.entry().inherited("Resources").unwrap_or(&EMPTY)
    }

    /// Captures the decoded content alongside the page that supplied it.
    pub fn capture(self, budget: &Budget) -> Result<Captured> {
        let content = PageContent::read(&mut Reads::new(&self.version, budget), self.entry())?;
        Ok(Captured {
            page: self,
            content: content.operations().data().clone(),
        })
    }

    /// Writes a fresh resources dictionary. Within the same document its
    /// indirect entries stay shared unless the caller needs an isolated copy.
    pub fn copy_resources(
        &self,
        transaction: &mut Transaction,
        same_document: bool,
        isolated: bool,
        budget: &Budget,
    ) -> Result<Reference> {
        with_savepoint(transaction, |transaction| {
            let resources = self.resources();
            let value = match (same_document, isolated) {
                (true, false) => copy_for_edit(resources, budget)?,
                (true, true) => {
                    import::copy_resource_graph(transaction, &self.version, resources, budget)?
                }
                (false, _) => {
                    let root = Root::read(transaction.version(), budget)?.reference;
                    let along = import::Along {
                        values: std::slice::from_ref(resources),
                        ..import::Along::default()
                    };
                    let copied = import::import(
                        transaction,
                        &self.version,
                        Vec::new(),
                        &[],
                        along,
                        root,
                        budget,
                    )?;
                    copied.values.into_iter().next().unwrap_or(Value::Null)
                }
            };
            let new = transaction.allocate()?;
            Ok(transaction.write(new.number(), &value, budget)?)
        })
    }
}

/// The decoded operations and page geometry of one canvas source version.
pub struct Captured {
    page: SourcePage,
    content: Bytes,
}

impl Captured {
    /// The content used both for canvas clipping and for the Form.
    #[must_use]
    pub fn content(&self) -> &[u8] {
        &self.content
    }

    /// Gives the caller the pinned source to use for a standalone resources copy.
    #[must_use]
    pub fn into_page(self) -> SourcePage {
        self.page
    }

    /// The rotation, display box in user space, unit, and size in points.
    #[must_use]
    pub fn geometry(&self) -> (i64, Box4, f64, (f64, f64)) {
        let page = self.page.entry();
        let (rotation, bbox, unit) = space(page);
        (rotation, bbox, unit, Display::from_page(page).size())
    }
}

/// At most this many Forms per destination document, least recently used first.
const PAGE_FORMS: usize = 64;

/// A placed page's Form and the source and target objects it depends on.
/// Stamps keep no version alive.
struct PageForm {
    page: Reference,
    contents: Option<Value>,
    resources: Value,
    bbox: [f64; 4],
    source: ObjectStamps,
    target: ObjectStamps,
    form: u32,
}

/// The reusable Form XObjects of one destination document (DESIGN.md F4).
#[derive(Default)]
pub struct PageForms {
    entries: Mutex<VecDeque<PageForm>>,
}

impl PageForms {
    /// The number of Forms the cache keeps.
    #[must_use]
    pub fn count(&self) -> usize {
        self.entries
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .len()
    }

    /// Reuses a Form while its page values, their reachable source objects,
    /// and the Form and its resources dictionary in the target are unchanged.
    pub fn find(
        &self,
        source: &Captured,
        target: &Version,
        budget: &Budget,
    ) -> Result<Option<u32>> {
        let page = &source.page;
        let contents = page.entry().dict().get("Contents");
        let resources = page.resources();
        let bbox = display_box(page.entry());
        let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        for at in 0..entries.len() {
            let form = &entries[at];
            if form.page == page.reference
                && form.bbox == bbox
                && form.contents.as_ref() == contents
                && &form.resources == resources
                && form.source.unchanged(&page.version, budget)?
                && form.target.unchanged(target, budget)?
                && let Some(form) = entries.remove(at)
            {
                let number = form.form;
                #[expect(
                    clippy::disallowed_methods,
                    reason = "returns the removed entry to the bounded cache"
                )]
                entries.push_back(form);
                return Ok(Some(number));
            }
        }
        Ok(None)
    }

    /// Writes a new Form from this capture and records only the values and
    /// stamps needed to reuse it. A refusal restores the transaction.
    pub fn write(
        &self,
        transaction: &mut Transaction,
        captured: &Captured,
        same_document: bool,
        budget: &Budget,
    ) -> Result<u32> {
        let page = &captured.page;
        let contents = page.entry().dict().get("Contents");
        let resources = page.resources();
        let bbox = display_box(page.entry());
        let reached = sources::reachable(
            &page.version,
            &[contents.unwrap_or(&Value::Null), resources],
            budget,
        )?;
        let source = page.version.stamps(&reached, budget)?;
        drop(reached);
        let contents = contents
            .map(|value| copy_for_edit(value, budget))
            .transpose()?;
        let resources = copy_for_edit(resources, budget)?;
        let entry = with_savepoint(transaction, |transaction| {
            let dictionary = page.copy_resources(transaction, same_document, true, budget)?;
            let form = write_form(transaction, &captured.content, bbox, dictionary, budget)?;
            let target = transaction
                .version()
                .stamps(&[form, dictionary.number()], budget)?;
            Ok(PageForm {
                page: page.reference,
                contents,
                resources,
                bbox,
                source,
                target,
                form,
            })
        })?;
        let number = entry.form;
        let mut entries = self.entries.lock().unwrap_or_else(PoisonError::into_inner);
        if entries.len() >= PAGE_FORMS {
            entries.pop_front();
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "the cache holds at most PAGE_FORMS entries"
        )]
        entries.push_back(entry);
        Ok(number)
    }
}

#[expect(
    clippy::disallowed_methods,
    reason = "five fixed Form keys and four BBox coordinates"
)]
fn write_form(
    transaction: &mut Transaction,
    content: &[u8],
    bbox: [f64; 4],
    resources: Reference,
    budget: &Budget,
) -> Result<u32> {
    let data = deflate(content, 6, usize::MAX, budget)?
        .ok_or_else(|| PageError::Value("the data could not be compressed".into()))?;
    let mut stream = Stream::new(
        [
            (Name::from("Type"), Value::Name(Name::from("XObject"))),
            (Name::from("Subtype"), Value::Name(Name::from("Form"))),
            (
                Name::from("BBox"),
                Value::Array(bbox.into_iter().map(Value::from).collect()),
            ),
            (Name::from("Resources"), Value::Reference(resources)),
        ]
        .into_iter()
        .collect(),
        Bytes::from(data),
    );
    stream
        .dict_mut()
        .insert(Name::from("Filter"), Value::Name(Name::from("FlateDecode")));
    let form = transaction.allocate()?;
    Ok(transaction
        .write(form.number(), &Value::Stream(Box::new(stream)), budget)?
        .number())
}

/// Copies the image XObject and the objects it reaches, excluding pages.
/// An absent object or a non-image is not copied.
pub fn copy_image(
    transaction: &mut Transaction,
    version: &Version,
    number: u32,
    budget: &Budget,
) -> Result<Option<u32>> {
    let Some(generation) = version.generation(number) else {
        return Ok(None);
    };
    let reference = Reference::new(number, generation);
    match target(version, reference, budget)?.as_deref() {
        Some(Value::Stream(stream)) if matches!(stream.dict().get("Subtype"), Some(Value::Name(name)) if name.as_bytes() == b"Image") =>
            {}
        _ => return Ok(None),
    }
    with_savepoint(transaction, |transaction| {
        let root = Root::read(transaction.version(), budget)?.reference;
        let along = import::Along {
            objects: std::slice::from_ref(&reference),
            ..import::Along::default()
        };
        let imported = import::import(transaction, version, Vec::new(), &[], along, root, budget)?;
        Ok(match imported.objects.into_iter().next() {
            Some(Value::Reference(copy)) => Some(copy.number()),
            _ => None,
        })
    })
}

#[cfg(test)]
mod tests {
    use crate::pages::{resolve, test_document};

    use super::*;

    fn capture(version: Arc<Version>, budget: &Budget) -> Captured {
        let index = PageIndex::build(&mut Reads::new(&version, budget)).unwrap();
        SourcePage::new(version, Arc::new(index), Reference::new(3, 0))
            .unwrap()
            .capture(budget)
            .unwrap()
    }

    #[test]
    fn form_uses_the_captured_content_box_and_resource_graph() {
        let budget = Budget::default();
        let document = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /MediaBox [0 0 50 20] /Rotate 90 /Resources << /Marker 5 0 R >> >>",
            "<< /Type /Page /Parent 2 0 R /Contents 4 0 R /UserUnit 2 >>",
            "<< /Length 3 >>\nstream\nq Q\nendstream",
            "42",
        ]);
        let captured = capture(document.current(), &budget);
        assert_eq!(
            captured.geometry(),
            (90, (0.0, 0.0, 50.0, 20.0), 2.0, (40.0, 100.0))
        );
        let mut tx = document.begin();
        tx.write(5, &Value::Integer(43), &budget).unwrap();
        tx.write(4, &Value::Null, &budget).unwrap();

        let forms = PageForms::default();
        let form = forms.write(&mut tx, &captured, true, &budget).unwrap();
        let object = tx.version().get(form, &budget).unwrap().unwrap();
        let Value::Stream(stream) = object.value().as_ref() else {
            panic!("expected a Form stream")
        };
        assert_eq!(
            stream.dict().get("Subtype"),
            Some(&Value::Name(Name::from("Form")))
        );
        assert_eq!(stream.decoded(&budget).unwrap(), b"q Q");
        assert_eq!(
            stream.dict().get("BBox").and_then(crate::display::rect),
            Some((0., 0., 50., 20.))
        );
        let Some(Value::Dict(resources)) =
            resolve(tx.version(), stream.dict().get("Resources"), &budget).unwrap()
        else {
            panic!("expected copied resources")
        };
        assert_eq!(
            resolve(tx.version(), resources.get("Marker"), &budget).unwrap(),
            Some(Value::Integer(42))
        );

        assert_eq!(
            forms.find(&captured, tx.version(), &budget).unwrap(),
            Some(form)
        );
        let current = capture(tx.snapshot(), &budget);
        assert_eq!(forms.find(&current, tx.version(), &budget).unwrap(), None);
        tx.write(form, &Value::Null, &budget).unwrap();
        assert_eq!(forms.find(&captured, tx.version(), &budget).unwrap(), None);
    }
    #[test]
    fn failed_form_allocation_discards_its_resources_and_cache_entry() {
        let budget = Budget::default();
        let doc = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R >>",
        ]);
        let captured = capture(doc.current(), &budget);
        let mut tx = doc.begin();
        // Only the resource dictionary will fit in the remaining object numbers.
        tx.write(u32::MAX - 1, &Value::Integer(7), &budget).unwrap();
        let forms = PageForms::default();
        assert!(forms.write(&mut tx, &captured, true, &budget).is_err());
        assert_eq!(forms.count(), 0);
        assert!(tx.version().get(u32::MAX, &budget).unwrap().is_none());
        assert_eq!(
            tx.version()
                .get(u32::MAX - 1, &budget)
                .unwrap()
                .unwrap()
                .value()
                .as_ref(),
            &Value::Integer(7)
        );
    }
}
