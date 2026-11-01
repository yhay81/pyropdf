use unicode_bidi::{BidiInfo, ParagraphBidiInfo};
fn main() {
    let chars = [
        'a', 'ב', 'ب', '日', '1', ' ', '\n', '\r', '\u{2029}', '\u{2066}', '\u{2067}', '\u{2068}',
        '\u{2069}', '\u{202a}', '\u{202b}', '\u{202c}', '\u{0301}',
    ];
    let mut seed = 0x243f6a8885a308d3u64;
    let mut comparisons = 0usize;
    for case in 0..20000 {
        let mut text = String::new();
        for _ in 0..(case % 100) {
            seed ^= seed << 13;
            seed ^= seed >> 7;
            seed ^= seed << 17;
            text.push(chars[seed as usize % chars.len()]);
        }
        let whole = BidiInfo::new(&text, None);
        for p in &whole.paragraphs {
            let (levels, runs) = whole.visual_runs(p, p.range.clone());
            let part = ParagraphBidiInfo::new(&text[p.range.clone()], Some(p.level));
            let (part_levels, part_runs) = part.visual_runs(0..p.range.len());
            if levels[p.range.clone()] != part_levels
                || runs
                    .iter()
                    .map(|r| r.start - p.range.start..r.end - p.range.start)
                    .collect::<Vec<_>>()
                    != part_runs
            {
                eprintln!("mismatch case={case} text={text:?} para={p:?}");
                std::process::exit(1);
            }
            comparisons += 1;
        }
    }
    println!("passed cases=20000 paragraphs={comparisons}");
}
