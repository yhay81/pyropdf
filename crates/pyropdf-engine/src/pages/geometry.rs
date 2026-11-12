//! Display-space geometry and edits to a page's rotation and boxes.

use pyropdf_store::budget::Budget;
use pyropdf_store::memo::Memo;
use pyropdf_store::pages::{Page, PageIndex, PageTree};
use pyropdf_store::settle::Scope;
use pyropdf_store::value::{Array, Name, Reference, Value};
use pyropdf_store::version::Transaction;

use super::{PageError, Result, page_dict, with_savepoint};
use crate::display::{
    BOXES, Box4, Display, display_box, number, rect, rotation, to_display, to_pdf, user_boxes,
};

/// Normalized rotation and rotated width and height in points.
#[must_use]
pub fn geometry(page: &Page) -> (i64, f64, f64) {
    let (width, height) = Display::from_page(page).size();
    (rotation(page.rotate()), width, height)
}

/// Normalized rotation, unrotated display box and points per user-space unit.
#[must_use]
pub fn space(page: &Page) -> (i64, Box4, f64) {
    let [x0, y0, x1, y1] = display_box(page);
    (rotation(page.rotate()), (x0, y0, x1, y1), page.user_unit())
}

/// Media, crop, bleed, trim and art boxes in display space.
#[must_use]
pub fn boxes(page: &Page) -> [Box4; 5] {
    let (frame, rotation, unit) = (display_box(page), rotation(page.rotate()), page.user_unit());
    user_boxes(page).map(|(x0, y0, x1, y1)| {
        let (a, b) = to_display(frame, rotation, unit, x0, y0);
        let (c, d) = to_display(frame, rotation, unit, x1, y1);
        (a.min(c), b.min(d), a.max(c), b.max(d))
    })
}

/// Writes a normalized rotation, returning false for a stale page.
pub fn set_rotation(
    transaction: &mut Transaction,
    pages: &Memo<PageTree, PageIndex>,
    reference: Reference,
    rotation: i64,
    budget: &Budget,
) -> Result<bool> {
    edit_attribute(transaction, pages, reference, "Rotate", budget, |_| {
        Ok(Value::Integer(rotation))
    })
}

/// Writes a display-space rectangle as one of the page's five boxes.
/// Returns false for a stale page. A rejected box leaves the page untouched.
pub fn set_box(
    transaction: &mut Transaction,
    pages: &Memo<PageTree, PageIndex>,
    reference: Reference,
    kind: usize,
    display: Box4,
    budget: &Budget,
) -> Result<bool> {
    let Some(&key) = BOXES.get(kind) else {
        return Err(PageError::Value(format!("no page box {kind}")));
    };
    edit_attribute(transaction, pages, reference, key, budget, |page| {
        box_value(page, key, display)
    })
}

/// Read the settled page and publish one validated attribute under the same writer lock.
fn edit_attribute(
    transaction: &mut Transaction,
    pages: &Memo<PageTree, PageIndex>,
    reference: Reference,
    key: &'static str,
    budget: &Budget,
    value: impl FnOnce(&Page) -> Result<Value>,
) -> Result<bool> {
    with_savepoint(transaction, |transaction| {
        transaction.settle(Scope::Commit, budget)?;
        let view = transaction.snapshot();
        let index = PageIndex::memoized(pages, &view, budget)?;
        let Some(at) = index.index_of(reference) else {
            return Ok(false);
        };
        let value = value(&index.pages()[at])?;
        let mut dict = page_dict(&view, reference, budget)?;
        #[expect(
            clippy::disallowed_methods,
            reason = "one fixed key in the budgeted page dictionary"
        )]
        dict.insert(Name::from(key), value);
        transaction.write(reference.number(), &Value::Dict(dict), budget)?;
        Ok(true)
    })
}

fn box_value(page: &Page, key: &str, display: Box4) -> Result<Value> {
    let (frame, rotation, unit) = (display_box(page), rotation(page.rotate()), page.user_unit());
    let (a, b) = to_pdf(frame, rotation, unit, display.0, display.1);
    let (c, d) = to_pdf(frame, rotation, unit, display.2, display.3);
    let user = (a.min(c), b.min(d), a.max(c), b.max(d));
    // The setter's tolerance is one millionth of a display point.
    let slack = 1e-6 / unit;
    if user.2 - user.0 < slack || user.3 - user.1 < slack {
        return Err(PageError::Value(format!("the {key} is empty")));
    }
    let inside = |inner: Box4, outer: Box4| {
        inner.0 >= outer.0 - slack
            && inner.1 >= outer.1 - slack
            && inner.2 <= outer.2 + slack
            && inner.3 <= outer.3 + slack
    };
    if key == "MediaBox" {
        let set = [page.crop_box().and_then(rect)].into_iter().chain(
            BOXES[2..]
                .iter()
                .map(|key| page.dict().get(*key).and_then(rect)),
        );
        if set.flatten().any(|inner| !inside(inner, user)) {
            return Err(PageError::Value(
                "the new MediaBox does not hold the page's other boxes; set them first".into(),
            ));
        }
    } else if !inside(user, user_boxes(page)[0]) {
        return Err(PageError::Value(format!("the {key} leaves the MediaBox")));
    }
    Ok(Value::Array(Array::from(vec![
        number(user.0),
        number(user.1),
        number(user.2),
        number(user.3),
    ])))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pages::test_document;

    #[test]
    fn editing_an_inherited_box_and_rotation_changes_only_the_selected_page() {
        let doc = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 /MediaBox [0 0 200 100] /CropBox [10 20 190 90] /Rotate 90 >>",
            "<< /Type /Page /Parent 2 0 R /UserUnit 2 >>",
            "<< /Type /Page /Parent 2 0 R >>",
        ]);
        let memo = Memo::new(1);
        let budget = Budget::default();
        let mut tx = doc.begin();
        let selected = Reference::new(3, 0);
        let display = (20.0, 40.0, 100.0, 180.0);
        assert!(set_box(&mut tx, &memo, selected, 1, display, &budget).unwrap());
        assert!(set_rotation(&mut tx, &memo, selected, 180, &budget).unwrap());
        let index = PageIndex::memoized(&memo, &tx.snapshot(), &budget).unwrap();
        let updated = &index.pages()[0];
        assert_eq!(space(updated), (180, (30.0, 30.0, 100.0, 70.0), 2.0));
        assert_eq!(geometry(updated), (180, 140.0, 80.0));
        assert_eq!(boxes(updated)[1], (0.0, 0.0, 140.0, 80.0));
        assert_eq!(
            space(&index.pages()[1]),
            (90, (10.0, 20.0, 190.0, 90.0), 1.0)
        );
        let error =
            set_box(&mut tx, &memo, selected, 0, (1.0, 1.0, 10.0, 10.0), &budget).unwrap_err();
        assert!(matches!(error, PageError::Value(_)));
        let unchanged = PageIndex::memoized(&memo, &tx.snapshot(), &budget).unwrap();
        assert_eq!(updated.dict(), unchanged.pages()[0].dict());
    }

    #[test]
    fn a_warm_page_dictionary_is_still_charged_to_the_edit() {
        use pyropdf_store::budget::LimitCode;
        let data = "X".repeat(128 * 1024);
        let doc = test_document(&[
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            &format!("<< /Type /Page /Parent 2 0 R /Extra ({data}) >>"),
        ]);
        let memo = Memo::new(1);
        let mut tx = doc.begin();
        let before = tx.snapshot();
        PageIndex::memoized(&memo, &before, &Budget::default()).unwrap();
        let budget = Budget::default().with_limit(LimitCode::MemorySize, Some(32 * 1024));
        let error = set_rotation(&mut tx, &memo, Reference::new(3, 0), 90, &budget).unwrap_err();
        let PageError::Store(error) = error else {
            panic!("expected a store limit")
        };
        assert_eq!(error.limit().unwrap().code(), LimitCode::MemorySize);
        assert!(
            before
                .changes(&tx.snapshot(), &Budget::default())
                .unwrap()
                .unwrap()
                .is_empty()
        );
    }
}
