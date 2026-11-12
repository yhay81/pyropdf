//! Insert pages from another document and keep only the navigation, form
//! fields, and structure content that belong to the imported pages.

use std::sync::Arc;

use pyropdf_store::budget::{Budget, BudgetVec, LimitCode};
use pyropdf_store::memo::{Memo, Reads};
use pyropdf_store::pages::{Page, PageIndex, PageTree};
use pyropdf_store::refs::{self, Edits, Rule};
use pyropdf_store::value::{Array, Dict, Name, Reference, Value};
use pyropdf_store::version::{Transaction, Version};

use crate::forms;
use crate::navigation::{self, Destination, Lead, LeadReader, NamedDestinations, lead, read};
use crate::pages::tree::{INHERITABLE, Root, rewrite_with};
use crate::pages::{PageError, Result, indirect, not_a_page, page_dict, resolve, with_savepoint};

/// A pinned source version with its page index and document-owned caches.
pub struct Source<'a> {
    pub version: &'a Arc<Version>,
    pub index: &'a PageIndex,
    pub references: &'a refs::Cache,
    pub destinations: &'a Memo<(), NamedDestinations>,
}

/// Inserts copies of the selected pages at `at`, preserving internal links
/// between them and registering their forms and structure in the destination.
/// On failure the destination transaction keeps its previous writes.
pub fn import_pages(
    transaction: &mut Transaction,
    pages_memo: &Memo<PageTree, PageIndex>,
    source: &Source<'_>,
    pages: impl IntoIterator<Item = Reference>,
    at: usize,
    budget: &Budget,
) -> Result<()> {
    let view = transaction.snapshot();
    let index = PageIndex::memoized(pages_memo, &view, budget)?;
    let root = Root::read(&view, budget)?;
    let mut selected = BudgetVec::new(budget);
    let mut imported = BudgetVec::new(budget);
    for reference in pages {
        budget.poll()?;
        let at = source
            .index
            .index_of(reference)
            .ok_or_else(|| not_a_page(reference))?;
        selected.try_push((reference, &source.index.pages()[at]))?;
        imported.try_push(reference.number())?;
    }
    imported.sort_unstable();
    if imported.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(PageError::Value(
            "a page of another document can be inserted once per call".into(),
        ));
    }

    let mut links = Links {
        source,
        imported: &imported,
        reads: Reads::new(source.version, budget),
        named: None,
        overrides: BudgetVec::new(budget),
    };
    let mut prepared = BudgetVec::new(budget);
    prepared.try_reserve_exact(selected.len())?;
    for &(reference, entry) in &selected {
        budget.poll()?;
        let mut dict = page_dict(source.version, reference, budget)?;
        materialize(&mut dict, entry, root.dict(), budget)?;
        if let Some(annots) = links.annots(&dict)? {
            if annots.is_empty() {
                dict.remove("Annots");
            } else {
                #[expect(clippy::disallowed_methods, reason = "replaces the fixed /Annots key")]
                dict.insert(Name::from("Annots"), Value::Array(annots));
            }
        }
        prepared.try_push((reference, dict))?;
    }

    let mut overrides = links.overrides;
    let (mut fields, calculation) = refs::form_fields(&mut Reads::new(source.version, budget))?;
    let (pruned, kept) = pruned(source, &imported, &overrides, !fields.is_empty(), budget)?;
    // A complete import registers every top-level field, a partial import
    // registers the fields that keep a widget of the selected pages.
    if let Some(kept) = kept {
        fields.retain(|field| kept.binary_search(&field.number()).is_ok());
    }
    overrides.try_extend(pruned.into_vec())?;
    let mut overrides = overrides.into_vec();
    // Link overrides take precedence when pruning also changed a link.
    // Charge the scratch space of the stable sort.
    budget.charge(
        LimitCode::MemorySize,
        overrides
            .len()
            .saturating_mul(size_of::<(Reference, Option<Value>)>()) as u64,
    )?;
    overrides.sort_by_key(|(reference, _)| *reference);
    overrides.dedup_by_key(|(reference, _)| *reference);

    let mut before = BudgetVec::new(budget);
    for (at, page) in index.pages().iter().enumerate() {
        before.try_push(indirect(page, at)?)?;
    }
    with_savepoint(transaction, |transaction| {
        let defaults = if fields.is_empty() {
            forms::ImportDefaults::default()
        } else {
            forms::plan_imported_defaults(&view, source.version, budget)?
        };
        let along = super::Along {
            objects: &fields,
            found: &calculation,
            values: &defaults.values,
            copies: defaults.renames(),
        };
        let imported = super::import(
            transaction,
            source.version,
            prepared.into_vec(),
            &overrides,
            along,
            root.reference,
            budget,
        )?;
        let at = at.min(before.len());
        let mut order = BudgetVec::new(budget);
        order.try_reserve_exact(before.len().saturating_add(imported.pages.len()))?;
        order.try_extend_from_slice(&before[..at])?;
        order.try_extend_from_slice(&imported.pages)?;
        order.try_extend_from_slice(&before[at..])?;
        let view = transaction.snapshot();
        rewrite_with(transaction, &view, &index, &order, &imported.pages, budget)?;

        let mut fields = BudgetVec::new(budget);
        for value in &imported.objects {
            if let Value::Reference(field) = value {
                fields.try_push(*field)?;
            }
        }
        let mut calculation = BudgetVec::new(budget);
        calculation.try_extend(imported.found.iter().flatten().copied())?;
        forms::register_imported(
            transaction,
            &fields,
            &calculation,
            &defaults,
            &imported.values,
            &imported.copies,
            budget,
        )?;
        Ok(())
    })
}

/// Makes the source page's inherited attributes explicit, including defaults
/// it would otherwise inherit from the receiving root.
#[expect(
    clippy::disallowed_methods,
    reason = "inserts only the fixed INHERITABLE keys with budgeted copies"
)]
fn materialize(dict: &mut Dict, page: &Page, root: &Dict, budget: &Budget) -> Result<()> {
    for key in INHERITABLE {
        match page.inherited(key) {
            Some(value) => {
                dict.insert(Name::from(key), refs::copy_for_edit(value, budget)?);
            }
            None => {
                dict.remove(key);
            }
        }
    }
    for key in INHERITABLE {
        if dict.get(key).is_some() || root.get(key).is_none() {
            continue;
        }
        let default = match key {
            "CropBox" => dict
                .get("MediaBox")
                .map(|value| refs::copy_for_edit(value, budget))
                .transpose()?
                .unwrap_or_else(a4),
            "MediaBox" => a4(),
            "Rotate" => Value::Integer(0),
            _ => Value::Dict(Dict::new()),
        };
        dict.insert(Name::from(key), default);
    }
    Ok(())
}

/// Source objects that an import replaces: `Some` with the value to copy
/// instead, `None` with null.
type Overrides<'b> = BudgetVec<'b, (Reference, Option<Value>)>;

/// Returns the overrides that leave the structure content of the source
/// pages other than `imported` out of a tagged `source`, and the widgets
/// and fields of those pages out of a source with a form (`form`), as
/// deleting those pages would, with the links that `links` removes. Empty
/// when neither applies or when every page comes along.
///
/// With a form and pages left behind, also returns the top-level fields
/// that keep a widget of the imported pages, sorted by number.
fn pruned<'b>(
    source: &Source<'_>,
    imported: &[u32],
    links: &[(Reference, Option<Value>)],
    form: bool,
    budget: &'b Budget,
) -> Result<(Overrides<'b>, Option<Vec<u32>>)> {
    let version = source.version;
    let catalog = resolve(version, version.trailer().get("Root"), budget)?;
    let tagged =
        matches!(&catalog, Some(Value::Dict(catalog)) if catalog.get("StructTreeRoot").is_some());
    let mut removed = BudgetVec::new(budget);
    let mut out = BudgetVec::new(budget);
    if !(tagged || form) {
        return Ok((out, None));
    }
    for page in source.index.pages().iter().filter_map(Page::object) {
        if imported.binary_search(&page.number()).is_err() {
            removed.try_push(page.number())?;
        }
    }
    if removed.is_empty() {
        return Ok((out, None));
    }
    let mut removed = removed.into_vec();
    removed.sort_unstable();
    removed.dedup();
    let references = source.references.get(version, budget)?;
    let mut reads = Reads::new(version, budget);
    let mut edits = Edits::new(budget);
    for (reference, value) in links {
        if value.is_none() {
            edits.apply(
                &mut reads,
                &references,
                reference.number(),
                &[],
                Rule::RemoveReferrer,
            )?;
        }
    }
    if tagged {
        refs::prune_structure(&mut reads, &references, &removed, imported, &mut edits)?;
    }
    let kept = if form {
        let mut kept = refs::prune_forms(&mut reads, &references, &removed, imported, &mut edits)?;
        kept.sort_unstable();
        Some(kept)
    } else {
        None
    };
    // Computing the values removes the tree nodes that they empty.
    let values = edits.values(version, budget)?;
    for &number in edits.orphans() {
        if let Some(generation) = version.generation(number) {
            out.try_push((Reference::new(number, generation), None))?;
        }
    }
    for (number, value) in values.into_vec() {
        if let Some(generation) = version.generation(number) {
            out.try_push((Reference::new(number, generation), Some(value)))?;
        }
    }
    Ok((out, kept))
}

/// The media box that a page without one gets: A4, as the renderer draws it.
fn a4() -> Value {
    Value::Array(Array::from(vec![
        Value::Integer(0),
        Value::Integer(0),
        Value::Integer(595),
        Value::Integer(842),
    ]))
}

/// Prepares the links of pages that an import brings from `source`.
struct Links<'a, 'b> {
    source: &'a Source<'a>,
    /// The numbers of the imported pages, sorted.
    imported: &'a [u32],
    reads: Reads<'b>,
    named: Option<Arc<NamedDestinations>>,
    /// The link objects that lead elsewhere once imported, or go.
    overrides: BudgetVec<'b, (Reference, Option<Value>)>,
}

impl Links<'_, '_> {
    /// Returns the annotations of `page` without the links within `source`
    /// that lead to a page left behind, and with explicit destinations for
    /// the others; `None` when the page has no annotation array.
    fn annots(&mut self, page: &Dict) -> Result<Option<Array>> {
        let Some(Value::Array(mut items)) = self.resolved(page.get("Annots"))? else {
            return Ok(None);
        };
        self.reads
            .budget()
            .check(LimitCode::AnnotationCount, items.len() as u64)?;
        let budget = self.overrides.budget();
        let mut kept = BudgetVec::new(budget);
        kept.try_reserve_exact(items.len())?;
        for item in items.iter_mut() {
            let item = std::mem::replace(item, Value::Null);
            let Some(Value::Dict(annotation)) = self.resolved(Some(&item))? else {
                kept.try_push(item)?;
                continue;
            };
            let link = matches!(
                self.resolved(annotation.get("Subtype"))?,
                Some(Value::Name(subtype)) if subtype.as_bytes() == b"Link"
            );
            if !link {
                kept.try_push(item)?;
                continue;
            }
            let destination = match lead(self, &annotation)? {
                Lead::Target(_) => {
                    kept.try_push(item)?;
                    continue;
                }
                Lead::Destination(destination) => destination,
                Lead::Name(name) => self.lookup(&name)?,
            };
            let prepared = destination
                .page_in(self.source.index)
                .filter(|page| self.imported.binary_search(&page.number()).is_ok())
                .map(|page| self.retarget(annotation, &destination, page))
                .transpose()?;
            match item {
                Value::Reference(reference) => {
                    if prepared.is_some() {
                        kept.try_push(item)?;
                    }
                    self.overrides
                        .try_push((reference, prepared.map(Value::Dict)))?;
                }
                _ => {
                    if let Some(prepared) = prepared {
                        kept.try_push(Value::Dict(prepared))?;
                    }
                }
            }
        }
        Ok(Some(Array::from(kept.into_vec())))
    }

    /// Returns `link` leading to `destination` on `page` with an explicit
    /// array, in its `/GoTo` action if it has one.
    fn retarget(
        &mut self,
        mut link: Dict,
        destination: &Destination,
        page: Reference,
    ) -> Result<Dict> {
        let array = Value::Array(destination.array(page));
        if let Some(Value::Dict(mut action)) = self.resolved(link.get("A"))? {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /D key"
            )]
            action.insert(Name::from("D"), array);
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /A key"
            )]
            link.insert(Name::from("A"), Value::Dict(action));
            link.remove("Dest");
        } else {
            #[expect(
                clippy::disallowed_methods,
                reason = "this call inserts only the fixed /Dest key"
            )]
            link.insert(Name::from("Dest"), array);
            link.remove("A");
        }
        Ok(link)
    }

    fn lookup(&mut self, name: &[u8]) -> Result<Destination> {
        if self.named.is_none() {
            self.named = Some(NamedDestinations::memoized(
                self.source.destinations,
                self.source.version,
                self.reads.budget(),
            )?);
        }
        Ok(self
            .named
            .as_ref()
            .and_then(|named| named.get(name))
            .unwrap_or(Destination::UNRESOLVED))
    }
}

impl LeadReader for Links<'_, '_> {
    fn budget(&self) -> &Budget {
        self.reads.budget()
    }

    fn resolved(&mut self, value: Option<&Value>) -> navigation::Result<Option<Value>> {
        read(&mut self.reads, value)
    }

    /// The import reads no text of a link.
    fn text(&mut self, _: &[u8], _: &str, _: bool) -> navigation::Result<String> {
        Ok(String::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pages::test_document;

    #[test]
    fn partial_import_keeps_source_geometry_and_retargets_only_selected_links() {
        let source = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 80 160] >>",
            "<< /Type /Page /Parent 2 0 R /Annots [5 0 R 6 0 R] >>",
            "<< /Type /Page /Parent 2 0 R >>",
            "<< /Subtype /Link /Dest [4 0 R /Fit] >>",
            "<< /Subtype /Link /A << /S /GoTo /D [3 0 R /Fit] >> >>",
        ]);
        let destination = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 /CropBox [10 10 20 20] /Rotate 90 >>",
            "<< /Type /Page /Parent 2 0 R >>",
        ]);
        let version = source.current();
        let budget = Budget::default();
        let pages = PageIndex::build(&mut Reads::new(&version, &budget)).unwrap();
        let source = Source {
            version: &version,
            index: &pages,
            references: &refs::Cache::new(),
            destinations: &Memo::new(1),
        };
        let mut transaction = destination.begin();
        import_pages(
            &mut transaction,
            &Memo::new(1),
            &source,
            [Reference::new(3, 0)],
            0,
            &budget,
        )
        .unwrap();
        let view = transaction.snapshot();
        let pages = PageIndex::build(&mut Reads::new(&view, &budget)).unwrap();
        assert_eq!(pages.len(), 2);
        assert_eq!(pages.get(1).unwrap().object(), Some(Reference::new(3, 0)));
        let imported = pages.get(0).unwrap();
        assert_eq!(
            imported.inherited("MediaBox"),
            source.index.get(0).unwrap().inherited("MediaBox")
        );
        assert_eq!(
            imported.inherited("CropBox"),
            imported.inherited("MediaBox")
        );
        assert_eq!(imported.inherited("Rotate"), Some(&Value::Integer(0)));
        let Some(Value::Array(annots)) = imported.dict().get("Annots") else {
            panic!("missing imported annotations");
        };
        assert_eq!(annots.len(), 1);
        let Some(Value::Dict(link)) = resolve(&view, annots.get(0), &budget).unwrap() else {
            panic!("missing imported link");
        };
        let Some(Value::Dict(action)) = link.get("A") else {
            panic!("missing GoTo action");
        };
        let Some(Value::Array(dest)) = action.get("D") else {
            panic!("missing link destination");
        };
        assert_eq!(
            dest.get(0),
            Some(&Value::Reference(imported.object().unwrap()))
        );
    }
}
