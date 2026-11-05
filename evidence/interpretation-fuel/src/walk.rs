//! A prototype of option B of issue #678: before hayro interprets a page,
//! count the operators that interpreting it runs.
//!
//! The walk tokenizes the page's content with pyropdf's content syntax
//! (`pyropdf_engine::content::Operations`, #93), and each stream that the
//! content invokes, in the roles and with the resources that hayro 0.9
//! (yhay81/hayro 394e4a0d) gives them:
//!
//! - a form XObject that `Do` draws, with its own `/Resources` or else those
//!   it is drawn with (`x_object/form.rs`);
//! - the normal appearance of each annotation that is neither hidden nor
//!   `NoView`: `/AP /N`, or the state of `/AS` in it, or `/Off`, with its own
//!   resources or the page's (`interpret_annotation`);
//! - the group of a soft mask that `gs` sets, with its own resources or
//!   those of the stream that sets it (`SoftMask::new`);
//! - the cell of a tiling pattern that `scn` or `SCN` sets, with only its
//!   own resources (`pattern.rs`), once per paint that uses it and per copy
//!   of the cell that one tile needs (`hayro/src/tiling.rs`);
//! - the glyph procedure of each code that a Type 3 font shows, with its own
//!   resources, else its font's, else those it is drawn with
//!   (`font/type3.rs`).
//!
//! A stream's count is its own operators plus each stream it invokes times
//! the invocations, memoized per stream and resources, so that the walk
//! tokenizes each distinct stream once. The walk is iterative. hayro stops
//! at 50 nested interpretations, and so does the count; a stream that
//! invokes itself saturates the count.
//!
//! This prototype stops short of a product walk where the count does not
//! depend on it: it ignores optional content, which can only hide work; it
//! charges a soft mask at every `gs` that sets it, where the renderer draws
//! each mask once; and it maps a Type 3 code to its procedure through
//! `/Differences` only.

use std::collections::{HashMap, HashSet};

use hayro::hayro_syntax::object::{Array, Dict, Name, Object, Stream};
use hayro::hayro_syntax::page::{Page, Resources};
use pyropdf_engine::content::Operations;
use pyropdf_store::budget::{Budget, LimitCode};
use pyropdf_store::value::{Bytes, Value};

/// hayro-interpret's `MAX_NESTED_INTERPRETATION_DEPTH`.
const MAX_DEPTH: usize = 50;
/// `/F` flags that hide an annotation from hayro: `Hidden` and `NoView`.
const HIDDEN: u32 = (1 << 1) | (1 << 5);

/// What interpreting a page runs.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Count {
    /// Operators: each stream's own times the interpretations of it.
    pub operators: u64,
    /// Bytes of the strings that text operators show, multiplied likewise:
    /// at least the glyphs that they draw.
    pub shown: u64,
    /// Interpretations of streams other than the page's content.
    pub invocations: u64,
}

impl Count {
    /// Adds `times` interpretations of a stream that counts `other`.
    fn add_times(&mut self, other: Self, times: u64) {
        self.operators = self
            .operators
            .saturating_add(other.operators.saturating_mul(times));
        self.shown = self.shown.saturating_add(other.shown.saturating_mul(times));
        self.invocations = self
            .invocations
            .saturating_add(times.saturating_mul(other.invocations.saturating_add(1)));
    }

    const SATURATED: Self = Self {
        operators: u64::MAX,
        shown: u64::MAX,
        invocations: u64::MAX,
    };
}

/// The result of walking one page.
#[derive(Clone, Copy, Debug, Default)]
pub struct Walk {
    pub count: Count,
    /// Distinct streams tokenized, the page's content included.
    pub streams: u64,
    /// Bytes of decoded content tokenized.
    pub bytes: u64,
    /// Whether a stream invokes itself.
    pub cycle: bool,
    /// Type 3 codes shown that `/Differences` does not map.
    pub unmapped: u64,
}

/// A stream with the resources it is interpreted with. Resources are named
/// by the address of their dictionary's bytes, or by a sentinel.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
struct Key {
    stream: (u32, i32),
    resources: usize,
}

/// The page's resources, and empty resources.
const PAGE_RESOURCES: usize = 0;
const NO_RESOURCES: usize = 1;

#[derive(Clone)]
struct Invoked<'a> {
    key: Key,
    stream: Stream<'a>,
    resources: Resources<'a>,
}

fn invoked<'a>(
    stream: Stream<'a>,
    own: Option<Dict<'a>>,
    inherited: (&Resources<'a>, usize),
) -> Invoked<'a> {
    let (resources, id) = match own {
        Some(dict) => {
            let id = dict.data().as_ptr() as usize;
            (Resources::new(dict), id)
        }
        None => (inherited.0.clone(), inherited.1),
    };
    let id_of = stream.obj_id();
    Invoked {
        key: Key {
            stream: (id_of.obj_number, id_of.gen_number),
            resources: id,
        },
        stream,
        resources,
    }
}

/// A Type 3 font: its procedures and the names of its codes.
#[derive(Clone)]
struct Type3<'a> {
    procedures: Dict<'a>,
    resources: Option<Dict<'a>>,
    names: Vec<Option<Vec<u8>>>,
}

impl<'a> Type3<'a> {
    fn new(font: &Dict<'a>) -> Option<Self> {
        if font.get::<Name<'_>>(b"Subtype")?.as_ref() != b"Type3" {
            return None;
        }
        let procedures = font.get::<Dict<'_>>(b"CharProcs")?;
        let mut names = vec![None; 256];
        if let Some(differences) = font
            .get::<Dict<'_>>(b"Encoding")
            .and_then(|encoding| encoding.get::<Array<'_>>(b"Differences"))
        {
            let mut code: i64 = 0;
            for item in differences.iter::<Object<'_>>() {
                match item {
                    Object::Number(number) => code = number.as_i64(),
                    Object::Name(name) => {
                        if let Ok(at) = usize::try_from(code)
                            && at < names.len()
                        {
                            names[at] = Some(name.as_ref().to_vec());
                        }
                        code += 1;
                    }
                    _ => {}
                }
            }
        }
        Some(Self {
            procedures,
            resources: font.get::<Dict<'_>>(b"Resources"),
            names,
        })
    }
}

/// The graphics state that the walk follows.
#[derive(Clone, Default)]
struct State<'a> {
    fill: Option<(Invoked<'a>, u64)>,
    stroke: Option<(Invoked<'a>, u64)>,
    type3: Option<Type3<'a>>,
}

/// One stream on the walk's stack.
struct Frame<'a> {
    key: Key,
    depth: usize,
    total: Count,
    children: Vec<(Invoked<'a>, u64)>,
    next: usize,
}

pub struct Walker<'a> {
    budget: Budget,
    memo: HashMap<Key, Count>,
    walk: Walk,
    _page: std::marker::PhantomData<&'a ()>,
}

impl<'a> Walker<'a> {
    pub fn new() -> Self {
        Self {
            budget: Budget::default().with_limit(LimitCode::ContentOperationCount, None),
            memo: HashMap::new(),
            walk: Walk::default(),
            _page: std::marker::PhantomData,
        }
    }

    /// Walks `page` with an empty memo, as one render of the page would.
    pub fn page(mut self, page: &Page<'a>) -> Walk {
        let resources = page.resources().clone();
        let data = page.page_stream().unwrap_or_default();
        let (own, mut children) = self.scan(data, &resources, PAGE_RESOURCES);
        if let Some(annotations) = page.raw().get::<Array<'_>>(b"Annots") {
            for annotation in annotations.iter::<Dict<'_>>() {
                if let Some(appearance) = appearance(&annotation) {
                    let own = appearance.dict().get::<Dict<'_>>(b"Resources");
                    children.push((invoked(appearance, own, (&resources, PAGE_RESOURCES)), 1));
                }
            }
        }
        let mut stack = vec![Frame {
            key: Key {
                stream: (0, -1),
                resources: PAGE_RESOURCES,
            },
            depth: 0,
            total: own,
            children,
            next: 0,
        }];
        let mut active: HashSet<Key> = HashSet::new();
        loop {
            let top = stack
                .last_mut()
                .expect("the page's frame stays until the end");
            if top.next < top.children.len() {
                let (child, times) = top.children[top.next].clone();
                if let Some(count) = self.memo.get(&child.key) {
                    top.total.add_times(*count, times);
                    top.next += 1;
                    continue;
                }
                if active.contains(&child.key) {
                    self.walk.cycle = true;
                    top.total = Count::SATURATED;
                    top.next += 1;
                    continue;
                }
                let depth = top.depth + 1;
                if depth > MAX_DEPTH {
                    top.next += 1;
                    continue;
                }
                let data = child.stream.decoded().map(|data| data.into_owned());
                let (own, children) = match data {
                    Ok(data) => self.scan(&data, &child.resources, child.key.resources),
                    Err(_) => (Count::default(), Vec::new()),
                };
                active.insert(child.key);
                stack.push(Frame {
                    key: child.key,
                    depth,
                    total: own,
                    children,
                    next: 0,
                });
            } else {
                let done = stack.pop().expect("a frame");
                active.remove(&done.key);
                match stack.last_mut() {
                    Some(parent) => {
                        let times = parent.children[parent.next].1;
                        self.memo.insert(done.key, done.total);
                        parent.total.add_times(done.total, times);
                        parent.next += 1;
                    }
                    None => {
                        self.walk.count = done.total;
                        return self.walk;
                    }
                }
            }
        }
    }

    /// Tokenizes one stream and returns its own count and the streams that
    /// it invokes, with how often.
    fn scan(
        &mut self,
        data: &[u8],
        resources: &Resources<'a>,
        resources_id: usize,
    ) -> (Count, Vec<(Invoked<'a>, u64)>) {
        self.walk.streams += 1;
        self.walk.bytes += data.len() as u64;
        let Ok(operations) = Operations::parse(Bytes::new(data.to_vec()), &self.budget) else {
            return (Count::default(), Vec::new());
        };
        let mut own = Count::default();
        let mut invoked_here: HashMap<Key, (Invoked<'a>, u64)> = HashMap::new();
        let mut add = |child: &Invoked<'a>, times: u64| {
            invoked_here
                .entry(child.key)
                .and_modify(|entry| entry.1 = entry.1.saturating_add(times))
                .or_insert_with(|| (child.clone(), times));
        };
        let mut states = vec![State::default()];
        let mut type3_fonts: HashMap<usize, Option<Type3<'a>>> = HashMap::new();
        let mut font = |dict: Dict<'a>| {
            type3_fonts
                .entry(dict.data().as_ptr() as usize)
                .or_insert_with(|| Type3::new(&dict))
                .clone()
        };
        for operation in operations.operations() {
            own.operators += 1;
            let operator = operations.operator(operation);
            let operands = operations.operands(operation);
            let state = states.last_mut().expect("a state");
            match operator {
                b"q" => {
                    let copy = state.clone();
                    states.push(copy);
                }
                b"Q" => {
                    if states.len() > 1 {
                        states.pop();
                    }
                }
                b"Do" => {
                    if let Some(stream) = name(operands.first())
                        .and_then(|name| resources.x_objects.get::<Stream<'_>>(name))
                        && is_form(&stream)
                    {
                        let own = stream.dict().get::<Dict<'_>>(b"Resources");
                        add(&invoked(stream, own, (resources, resources_id)), 1);
                    }
                }
                b"cs" | b"sc" | b"g" | b"rg" | b"k" => state.fill = None,
                b"CS" | b"SC" | b"G" | b"RG" | b"K" => state.stroke = None,
                b"scn" | b"SCN" => {
                    let pattern = name(operands.last())
                        .and_then(|name| resources.patterns.get::<Stream<'_>>(name))
                        .and_then(|stream| tiling(stream));
                    if operator == b"scn" {
                        state.fill = pattern;
                    } else {
                        state.stroke = pattern;
                    }
                }
                b"f" | b"F" | b"f*" => {
                    if let Some((cell, copies)) = &state.fill {
                        add(cell, *copies);
                    }
                }
                b"S" | b"s" => {
                    if let Some((cell, copies)) = &state.stroke {
                        add(cell, *copies);
                    }
                }
                b"B" | b"B*" | b"b" | b"b*" => {
                    if let Some((cell, copies)) = &state.fill {
                        add(cell, *copies);
                    }
                    if let Some((cell, copies)) = &state.stroke {
                        add(cell, *copies);
                    }
                }
                b"Tf" => {
                    state.type3 = name(operands.first())
                        .and_then(|name| resources.fonts.get::<Dict<'_>>(name))
                        .and_then(&mut font);
                }
                b"gs" => {
                    if let Some(gs) = name(operands.first())
                        .and_then(|name| resources.ext_g_states.get::<Dict<'_>>(name))
                    {
                        if let Some(dict) = gs
                            .get::<Array<'_>>(b"Font")
                            .and_then(|array| array.iter::<Dict<'_>>().next())
                        {
                            state.type3 = font(dict);
                        }
                        if let Some(group) = gs
                            .get::<Dict<'_>>(b"SMask")
                            .and_then(|mask| mask.get::<Stream<'_>>(b"G"))
                        {
                            let own = group.dict().get::<Dict<'_>>(b"Resources");
                            add(&invoked(group, own, (resources, resources_id)), 1);
                        }
                    }
                }
                b"Tj" | b"TJ" | b"'" | b"\"" => {
                    let strings: Vec<&[u8]> = shown(operator, operands);
                    let bytes: u64 = strings.iter().map(|text| text.len() as u64).sum();
                    own.shown = own.shown.saturating_add(bytes);
                    if let Some((cell, copies)) = &state.fill {
                        add(cell, *copies);
                    }
                    if let Some(type3) = &state.type3 {
                        let mut codes = [0u64; 256];
                        for text in &strings {
                            for &code in *text {
                                codes[usize::from(code)] += 1;
                            }
                        }
                        for (code, &times) in codes.iter().enumerate() {
                            if times == 0 {
                                continue;
                            }
                            let procedure = type3.names[code].as_ref().and_then(|name| {
                                type3.procedures.get::<Stream<'_>>(name.as_slice())
                            });
                            match procedure {
                                Some(procedure) => {
                                    let own = procedure
                                        .dict()
                                        .get::<Dict<'_>>(b"Resources")
                                        .or_else(|| type3.resources.clone());
                                    add(&invoked(procedure, own, (resources, resources_id)), times);
                                }
                                None => self.walk.unmapped += times,
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        (own, invoked_here.into_values().collect())
    }
}

fn name(value: Option<&Value>) -> Option<&[u8]> {
    match value {
        Some(Value::Name(name)) => Some(name.as_bytes()),
        _ => None,
    }
}

fn is_form(stream: &Stream<'_>) -> bool {
    let dict = stream.dict();
    dict.get::<Name<'_>>(b"Subtype")
        .is_some_and(|subtype| subtype.as_ref() == b"Form")
        && dict.get::<[f32; 4]>(b"BBox").is_some()
}

/// The cell of a tiling pattern and how many copies of it one tile draws:
/// per axis, the extent of `/BBox` over the step, rounded up, plus one for a
/// tile that is not repeated (`TileAxis::new`).
fn tiling<'a>(stream: Stream<'a>) -> Option<(Invoked<'a>, u64)> {
    let dict = stream.dict().clone();
    if dict.get::<i64>(b"PatternType")? != 1 {
        return None;
    }
    let [x0, y0, x1, y1] = dict.get::<[f64; 4]>(b"BBox")?;
    let x_step = f64::from(dict.get::<f32>(b"XStep")?).abs();
    let y_step = f64::from(dict.get::<f32>(b"YStep")?).abs();
    if x_step < 1e-6 || y_step < 1e-6 || x0 == x1 || y0 == y1 {
        return None;
    }
    let axis = |extent: f64, step: f64| {
        let copies = (extent.abs() / step).ceil() + 1.0;
        if copies.is_finite() && copies < 1e18 {
            copies as u64
        } else {
            u64::MAX
        }
    };
    let copies = axis(x1 - x0, x_step).saturating_mul(axis(y1 - y0, y_step));
    let own = dict.get::<Dict<'_>>(b"Resources");
    let id = stream.obj_id();
    let (resources, resources_id) = match own {
        Some(dict) => {
            let id = dict.data().as_ptr() as usize;
            (Resources::new(dict), id)
        }
        None => (Resources::new(Dict::default()), NO_RESOURCES),
    };
    Some((
        Invoked {
            key: Key {
                stream: (id.obj_number, id.gen_number),
                resources: resources_id,
            },
            stream,
            resources,
        },
        copies,
    ))
}

/// The strings that a text operator shows.
fn shown<'v>(operator: &[u8], operands: &'v [Value]) -> Vec<&'v [u8]> {
    let string = |value: Option<&'v Value>| match value {
        Some(Value::String(text)) => Some(text.as_bytes()),
        _ => None,
    };
    match operator {
        b"TJ" => match operands.first() {
            Some(Value::Array(items)) => {
                items.iter().filter_map(|item| string(Some(item))).collect()
            }
            _ => Vec::new(),
        },
        b"\"" => string(operands.get(2)).into_iter().collect(),
        _ => string(operands.first()).into_iter().collect(),
    }
}

/// The appearance that hayro draws for `annotation`, if any.
fn appearance<'a>(annotation: &Dict<'a>) -> Option<Stream<'a>> {
    if annotation.get::<u32>(b"F").unwrap_or(0) & HIDDEN != 0 {
        return None;
    }
    annotation.get::<[f32; 4]>(b"Rect")?;
    let stream = match annotation.get::<Dict<'_>>(b"AP")?.get::<Object<'_>>(b"N")? {
        Object::Stream(stream) => stream,
        Object::Dict(states) => annotation
            .get::<Name<'_>>(b"AS")
            .and_then(|state| states.get::<Stream<'_>>(state.as_ref()))
            .or_else(|| states.get::<Stream<'_>>(b"Off"))?,
        _ => return None,
    };
    stream.dict().get::<[f32; 4]>(b"BBox")?;
    Some(stream)
}
