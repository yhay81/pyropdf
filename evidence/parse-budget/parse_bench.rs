//! Times parsing a 200,000-entry dictionary and reading every entry lazily,
//! without a budget, to measure what the budget hooks cost other callers.

use hayro_syntax::object::{Dict, FromBytes, MaybeRef, Object};
use std::time::Instant;
fn main() {
    let mut d = b"<<".to_vec();
    for i in 0..200_000 {
        d.extend_from_slice(format!("/Key{i} [{i} 0 R (s{i}) /N{i} 1.5] ").as_bytes());
    }
    d.extend_from_slice(b">>");
    let mut best = f64::MAX;
    for _ in 0..15 {
        let t = Instant::now();
        let dict = Dict::from_bytes(&d).unwrap();
        let mut n = 0;
        for k in dict.keys() {
            if let Some(MaybeRef::NotRef(Object::Array(a))) = dict.get_raw::<Object<'_>>(k.as_ref()) {
                n += a.raw_iter().count();
            }
        }
        assert_eq!(n, 800_000);
        best = best.min(t.elapsed().as_secs_f64());
    }
    println!("best {:.1} ms", best * 1e3);
}
