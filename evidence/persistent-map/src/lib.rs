//! The candidates of issue #9 for the persistent map behind versions
//! (DESIGN.md sections 4.1 and 16), the value that a version maps an object
//! number to, and the instruments that `src/main.rs` measures them with.

pub mod alloc;
pub mod chunks;
pub mod flat;
pub mod maps;
pub mod rng;
pub mod slot;
