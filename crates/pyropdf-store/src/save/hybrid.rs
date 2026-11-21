//! Hybrid-reference files (ISO 32000-2 section 7.5.8.4).

use super::{
    Ending, LimitCode, Mode, Output, Result, Row, Version, stream, table, table_row, trailer_fields,
};

/// A table-only reader sees a complete document. Members hidden in packed
/// streams are retired in the main table and defined by the supplemental
/// stream in an update. Only the final table carries /XRefStm.
pub(super) fn finish(
    version: &Version,
    rows: &[Row],
    out: &mut Output<'_>,
    mode: &Mode<'_>,
    ending: &Ending<'_>,
) -> Result<()> {
    let (number, level) = ending
        .stream
        .expect("a hybrid has a cross-reference stream");
    let mut digest = None;
    let previous = if let Some(previous) = ending.previous {
        previous
    } else {
        let main = out.offset()?;
        // A classic main section must cover every number. Bound the work
        // before filling gaps in an input with a sparse /Size.
        out.budget
            .check(LimitCode::ObjectCount, u64::from(number))?;
        out.display(format_args!("xref\n0 {number}\n"))?;
        let mut next = rows.iter().peekable();
        for index in 0..number {
            if let Some(row) = next.next_if(|row| row.number == index)
                && row.member.is_none()
            {
                table_row(row, out)?;
            } else {
                out.bytes(b"0000000000 65535 f \n")?;
            }
        }
        out.bytes(b"trailer\n<<")?;
        trailer_fields(version, mode, out, ending, &mut digest)?;
        out.display(format_args!(
            " /Size {number} >>\nstartxref\n{main}\n%%EOF\n"
        ))?;
        main
    };
    let supplement = out.offset()?;
    let size = u64::from(number) + 1;
    let own = Row {
        number,
        generation: 0,
        offset: Some(supplement),
        next_free: 0,
        member: None,
    };
    out.display(format_args!("{number} 0 obj\n<< /Type /XRef /Size {size}"))?;
    stream(
        rows.iter().filter(|row| row.member.is_some()),
        &own,
        level,
        out,
    )?;
    let xref = out.offset()?;
    if ending.previous.is_some() {
        // Incremental updates write edited objects in ordinary form.
        table(rows, out)?;
    } else {
        out.bytes(b"xref\n")?;
    }
    out.display(format_args!("{number} 1\n"))?;
    table_row(&own, out)?;
    out.bytes(b"trailer\n<<")?;
    trailer_fields(version, mode, out, ending, &mut digest)?;
    out.display(format_args!(
        " /Size {size} /Prev {previous} /XRefStm {supplement} >>\nstartxref\n{xref}\n%%EOF\n"
    ))
}
