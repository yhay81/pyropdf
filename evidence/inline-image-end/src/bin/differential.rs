//! Prints a digest of everything hayro-syntax's `UntypedIter` reads from
//! generated content streams around inline images. Two checkouts that print
//! the same digest read every stream the same way.

use hayro_syntax::content::UntypedIter;
use std::fmt::Write;

const STREAMS: usize = 20_000_000;

/// Pieces that make the `EI` search decide differently: `EI`s with and
/// without whitespace behind them, other inline images, strings and
/// escapes, binary bytes, operators that are or are not content.
const PIECES: &[&[u8]] = &[
    b"EI",
    b"EI ",
    b" EI\n",
    b"EI~",
    b"BI ",
    b"BI /W 1 /H 1 ID ",
    b"/W 1 ID ",
    b"ID ",
    b"(",
    b")",
    b"\\",
    b"\x80",
    b"\xff",
    b" ",
    b"\n",
    b"Tj ",
    b"(EI) Tj ",
    b"q ",
    b"Q ",
    b"]",
    b"[",
    b"<",
    b">",
    b"%",
    b"1 ",
    b"0 0 m ",
    b"BT ",
    b"ET ",
    b"x",
    b"/F1 12 Tf ",
];

struct XorShift(u64);

impl XorShift {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
}

fn main() {
    let mut rng = XorShift(0x9E37_79B9_7F4A_7C15);
    let mut digest: u64 = 0xcbf2_9ce4_8422_2325;
    let mut data = Vec::new();
    let mut text = String::new();
    let mut images = 0usize;

    for _ in 0..STREAMS {
        data.clear();
        data.extend_from_slice(b"q BI /W 1 /H 1 /BPC 8 /CS /G ID ");
        for _ in 0..1 + rng.below(40) {
            data.extend_from_slice(PIECES[rng.below(PIECES.len())]);
        }

        text.clear();
        let mut iter = UntypedIter::new(&data);
        while let Some(instruction) = iter.next() {
            if instruction.operator.as_ref() == b"BI" {
                images += 1;
            }
            writeln!(text, "{instruction:?}").unwrap();
        }

        for byte in text.bytes().chain([0xff]) {
            digest ^= u64::from(byte);
            digest = digest.wrapping_mul(0x0100_0000_01b3);
        }
    }

    println!("{STREAMS} streams, {images} inline images, digest {digest:016x}");
}
