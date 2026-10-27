//! The structural check (DESIGN.md section 8): a walk from the trailer
//! through the descriptors that reports each departure with its object, its
//! key, the Arlington clause, and the specification clause.
//!
//! The walk is forward and iterative. It visits an indirect object at most
//! [`MAX_TARGETS`] times, once per object type it is reached as, and asks the
//! [`Graph`] to charge every visit, so a deep or cyclic graph stays within
//! the operation's budget (F6). Predicates that the check does not decide
//! are counted in [`Report::unchecked`] instead of being reported.

mod expr;

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::ops::Deref;

use self::expr::{Expr, Scope, Val, parse, scalar, version};
use crate::{Condition, Key, Kind, Link, OBJECTS, Object, Since, Typed, Version};

/// How many object types one indirect object is checked as.
pub const MAX_TARGETS: usize = 4;

/// How many defects a report keeps before it is truncated.
pub const MAX_DEFECTS: usize = 10_000;

/// How many `/Parent` links the search for an inherited key follows.
const MAX_INHERITANCE: usize = 64;

/// A value of the document as the check sees it.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum View<'v> {
    Null,
    Boolean(bool),
    Integer(i64),
    Real(f64),
    Name(&'v [u8]),
    String(&'v [u8]),
    /// An array of this many elements.
    Array(usize),
    Dictionary,
    Stream,
    Reference(u32),
}

impl View<'_> {
    const fn name(&self) -> &'static str {
        match self {
            Self::Null => "null",
            Self::Boolean(_) => "boolean",
            Self::Integer(_) => "integer",
            Self::Real(_) => "number",
            Self::Name(_) => "name",
            Self::String(_) => "string",
            Self::Array(_) => "array",
            Self::Dictionary => "dictionary",
            Self::Stream => "stream",
            Self::Reference(_) => "reference",
        }
    }
}

impl fmt::Display for View<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Boolean(value) => write!(f, "{value}"),
            Self::Integer(value) => write!(f, "{value}"),
            Self::Real(value) => write!(f, "{value}"),
            Self::Name(bytes) => write!(f, "/{}", String::from_utf8_lossy(bytes)),
            Self::String(bytes) => write!(f, "({})", String::from_utf8_lossy(bytes)),
            other => f.write_str(other.name()),
        }
    }
}

/// The document that the check walks. The check does not depend on the
/// store: the binding implements this over one version.
pub trait Graph {
    type Value;
    /// A resolved indirect object.
    type Owned: Deref<Target = Self::Value>;
    /// A refusal of the budget, which stops the check.
    type Error;

    /// The version that the file declares: the later of its header and the
    /// catalog's `/Version`.
    fn version(&self) -> Version;
    /// The trailer dictionary.
    fn trailer(&self) -> &Self::Value;
    /// The indirect object `number`, or `None` when it does not resolve.
    fn resolve(&self, number: u32) -> Result<Option<Self::Owned>, Self::Error>;
    fn view<'v>(&self, value: &'v Self::Value) -> View<'v>;
    /// The entry `key` of a dictionary or of a stream's dictionary.
    fn get<'v>(&self, dict: &'v Self::Value, key: &[u8]) -> Option<&'v Self::Value>;
    /// The entries of a dictionary or of a stream's dictionary.
    fn entries<'v>(&self, dict: &'v Self::Value) -> Vec<(&'v [u8], &'v Self::Value)>;
    /// The elements of an array.
    fn elements<'v>(&self, array: &'v Self::Value) -> &'v [Self::Value];
    /// Charges one visit of an indirect object to the budget; `first` is
    /// whether the check meets this object for the first time.
    fn visit(&self, first: bool) -> Result<(), Self::Error>;
}

/// The kind of a departure.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DefectKind {
    /// A value has none of the key's types.
    Type,
    /// A required key is missing.
    Required,
    /// A key, type, or value is newer than the file's version.
    Version,
    /// A value is direct where it must be indirect, or the reverse.
    Indirect,
    /// A value is none of the key's possible values.
    Value,
    /// A page, name, or number tree breaks an invariant that the model
    /// cannot state, such as a page count or the order of keys.
    Invariant,
}

impl DefectKind {
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Type => "type",
            Self::Required => "required",
            Self::Version => "version",
            Self::Indirect => "indirect",
            Self::Value => "value",
            Self::Invariant => "invariant",
        }
    }
}

/// One departure from the model.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Defect {
    /// The indirect object, or `None` for the trailer.
    pub object: Option<u32>,
    /// The keys and indexes from the object to the value, joined by `/`.
    pub path: String,
    /// The Arlington clause: the object type and the key, such as
    /// `Catalog/Pages`.
    pub clause: String,
    /// The specification clause that the model names, such as `Table 29`.
    pub spec: &'static str,
    pub kind: DefectKind,
    pub message: String,
}

/// What the check found.
#[derive(Clone, Debug, Default)]
pub struct Report {
    pub defects: Vec<Defect>,
    /// Whether defects past [`MAX_DEFECTS`] were dropped.
    pub truncated: bool,
    /// The indirect objects that the check visited.
    pub objects: usize,
    /// The predicates that the check met but could not decide.
    pub unchecked: usize,
}

/// Runs the structural check over `graph`.
///
/// # Errors
///
/// Returns the graph's error when the budget refuses a visit or a
/// resolution.
pub fn check<G: Graph>(graph: &G) -> Result<Report, G::Error> {
    let trailer = OBJECTS
        .binary_search_by(|object| object.name.cmp("FileTrailer"))
        .ok()
        .and_then(|at| u16::try_from(at).ok())
        .unwrap_or(0);
    let mut walk = Walk {
        graph,
        version: graph.version(),
        report: Report::default(),
        queue: Vec::new(),
        visited: HashSet::new(),
        targets: HashMap::new(),
        counted: HashSet::new(),
    };
    let mut met = HashSet::new();
    walk.object(None, graph.trailer(), Target::Object(trailer))?;
    let size = match graph
        .get(graph.trailer(), b"Size")
        .map(|size| graph.view(size))
    {
        // A `/Size` that is not positive is a value defect already.
        Some(View::Integer(size)) if size > 0 => Some(size),
        _ => None,
    };
    while let Some((number, target)) = walk.queue.pop() {
        let first = met.insert(number);
        if first
            && let Some(size) = size
            && i64::from(number) >= size
        {
            walk.invariant(
                Some(number),
                "",
                "FileTrailer/Size",
                "7.5.5",
                format!("object {number} is referenced, but the trailer's /Size is {size}"),
            );
        }
        graph.visit(first)?;
        walk.report.objects += usize::from(first);
        if let Some(value) = graph.resolve(number)? {
            walk.object(Some(number), &value, target)?;
        }
    }
    Ok(walk.report)
}

#[derive(Clone, Copy, Debug)]
enum Target {
    /// An object type, by its index in [`OBJECTS`].
    Object(u16),
    /// A name or number tree whose leaves are the type's links.
    Tree(&'static Typed),
}

impl Target {
    fn id(self) -> usize {
        match self {
            Self::Object(at) => usize::from(at),
            Self::Tree(typed) => std::ptr::from_ref(typed).addr(),
        }
    }
}

struct Walk<'g, G: Graph> {
    graph: &'g G,
    version: Version,
    report: Report,
    queue: Vec<(u32, Target)>,
    visited: HashSet<(u32, usize)>,
    targets: HashMap<u32, usize>,
    /// The page tree nodes and structure elements whose kids are checked,
    /// once each although a node is checked as more than one type.
    counted: HashSet<u32>,
}

/// A direct value waiting to be checked as `Target`, at `String`.
type Frame<'v, V> = (&'v V, Target, String);

impl<'g, G: Graph> Walk<'g, G> {
    fn enqueue(&mut self, number: u32, target: Target) {
        let count = self.targets.entry(number).or_default();
        if *count < MAX_TARGETS && self.visited.insert((number, target.id())) {
            *count += 1;
            self.queue.push((number, target));
        }
    }

    fn scope<'v>(&self, dict: &'v G::Value, subject: Option<&'v G::Value>) -> Scope<'g, 'v, G> {
        Scope {
            graph: self.graph,
            version: self.version,
            dict: Some(dict),
            subject,
        }
    }

    /// Evaluates a predicate to a truth value, counting what it cannot
    /// decide.
    fn decide(
        &mut self,
        scope: &Scope<'_, '_, G>,
        predicate: &str,
    ) -> Result<Option<bool>, G::Error> {
        let decided = match parse(predicate) {
            Some(expr) => scope.eval(&expr)?.truth(),
            None => None,
        };
        if decided.is_none() {
            self.report.unchecked += 1;
        }
        Ok(decided)
    }

    fn defect(
        &mut self,
        object: Option<u32>,
        path: &str,
        described: &Object,
        key: &Key,
        kind: DefectKind,
        message: String,
    ) {
        if self.report.defects.len() >= MAX_DEFECTS {
            self.report.truncated = true;
            return;
        }
        let spec = if key.note.is_empty() {
            described.keys.first().map_or("", |first| first.note)
        } else {
            key.note
        };
        self.report.defects.push(Defect {
            object,
            path: path.to_owned(),
            clause: format!("{}/{}", described.name, key.name),
            spec,
            kind,
            message,
        });
    }

    /// Checks one object and the direct values inside it.
    fn object(
        &mut self,
        object: Option<u32>,
        root: &G::Value,
        target: Target,
    ) -> Result<(), G::Error> {
        let mut stack: Vec<Frame<'_, G::Value>> = vec![(root, target, String::new())];
        while let Some((value, target, path)) = stack.pop() {
            match (target, self.graph.view(value)) {
                (Target::Object(at), View::Dictionary | View::Stream) => {
                    self.dict(object, value, &OBJECTS[usize::from(at)], &path, &mut stack)?;
                }
                (Target::Object(at), View::Array(_)) => {
                    self.array(object, value, &OBJECTS[usize::from(at)], &path, &mut stack)?;
                }
                (Target::Tree(typed), View::Dictionary) => {
                    self.tree(object, value, typed, &path, &mut stack)?;
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn dict<'v>(
        &mut self,
        object: Option<u32>,
        dict: &'v G::Value,
        described: &'static Object,
        path: &str,
        stack: &mut Vec<Frame<'v, G::Value>>,
    ) -> Result<(), G::Error> {
        for key in described.keys {
            if key.name.ends_with('*') {
                continue;
            }
            let present = self
                .graph
                .get(dict, key.name.as_bytes())
                .is_some_and(|value| self.graph.view(value) != View::Null);
            if !present {
                self.required(object, dict, described, key, path)?;
            }
        }
        if described.name.starts_with("PageTreeNode")
            && let Some(number) = object
            && path.is_empty()
            && self.counted.insert(number)
        {
            self.pages(number, dict)?;
        }
        if described.name.starts_with("Annot")
            && let Some(number) = object
            && path.is_empty()
            && self.counted.insert(number)
        {
            self.appearance_state(number, dict, described.name);
        }
        if matches!(described.name, "StructTreeRoot" | "StructElem")
            && let Some(number) = object
            && path.is_empty()
            && self.counted.insert(number)
        {
            self.structure(number, dict, described.name)?;
        }
        for (name, value) in self.graph.entries(dict) {
            let Ok(name) = std::str::from_utf8(name) else {
                continue;
            };
            let key = described
                .key(name)
                .filter(|key| !key.name.ends_with('*'))
                .or_else(|| described.key("*"));
            if let Some(key) = key {
                let at = join(path, name);
                self.value(object, dict, described, key, value, &at, stack)?;
            }
        }
        Ok(())
    }

    fn array<'v>(
        &mut self,
        object: Option<u32>,
        array: &'v G::Value,
        described: &'static Object,
        path: &str,
        stack: &mut Vec<Frame<'v, G::Value>>,
    ) -> Result<(), G::Error> {
        let items = self.graph.elements(array);
        let repeating: Vec<&'static Key> = described
            .keys
            .iter()
            .filter(|key| key.name.len() > 1 && key.name.ends_with('*'))
            .collect();
        for key in described.keys {
            let Ok(at) = key.name.parse::<usize>() else {
                continue;
            };
            if at >= items.len() && key.required == Condition::Always && self.introduced(key) {
                let message = format!("element {at} is required");
                self.defect(object, path, described, key, DefectKind::Required, message);
            }
        }
        for (at, item) in items.iter().enumerate() {
            let named = described.key(&at.to_string());
            let key = named
                .or_else(|| (!repeating.is_empty()).then(|| repeating[at % repeating.len()]))
                .or_else(|| described.key("*"));
            if let Some(key) = key {
                let at = join(path, &at.to_string());
                self.value(object, array, described, key, item, &at, stack)?;
            }
        }
        Ok(())
    }

    /// Whether the file's version has the key at all.
    fn introduced(&self, key: &Key) -> bool {
        match key.since {
            Since::Version(since) => since <= self.version,
            Since::When(_) => true,
        }
    }

    fn required(
        &mut self,
        object: Option<u32>,
        dict: &G::Value,
        described: &Object,
        key: &Key,
        path: &str,
    ) -> Result<(), G::Error> {
        if !self.introduced(key) {
            return Ok(());
        }
        let required = match key.required {
            Condition::Never => false,
            Condition::Always => true,
            Condition::When(predicate) => {
                let scope = self.scope(dict, None);
                self.decide(&scope, predicate)? == Some(true)
            }
        };
        if !required || (key.inheritable && self.inherited(dict, key.name)?) {
            return Ok(());
        }
        let message = format!("/{} is required", key.name);
        self.defect(
            object,
            &join(path, key.name),
            described,
            key,
            DefectKind::Required,
            message,
        );
        Ok(())
    }

    /// Whether an ancestor along `/Parent` holds the key `name`.
    fn inherited(&self, dict: &G::Value, name: &str) -> Result<bool, G::Error> {
        let reference = |value: Option<&G::Value>| match value.map(|value| self.graph.view(value)) {
            Some(View::Reference(number)) => Some(number),
            _ => None,
        };
        let mut next = reference(self.graph.get(dict, b"Parent"));
        for _ in 0..MAX_INHERITANCE {
            let Some(number) = next else {
                break;
            };
            let Some(parent) = self.graph.resolve(number)? else {
                break;
            };
            if self.graph.get(&parent, name.as_bytes()).is_some() {
                return Ok(true);
            }
            next = reference(self.graph.get(&parent, b"Parent"));
        }
        Ok(false)
    }

    #[expect(clippy::too_many_arguments, reason = "the location of one value")]
    fn value<'v>(
        &mut self,
        object: Option<u32>,
        holder: &'v G::Value,
        described: &'static Object,
        key: &'static Key,
        raw: &'v G::Value,
        path: &str,
        stack: &mut Vec<Frame<'v, G::Value>>,
    ) -> Result<(), G::Error> {
        let reference = match self.graph.view(raw) {
            View::Reference(number) => Some(number),
            _ => None,
        };
        let owned;
        let value: &G::Value = match reference {
            Some(number) => {
                owned = self.graph.resolve(number)?;
                match &owned {
                    Some(value) => value,
                    None => return Ok(()),
                }
            }
            None => raw,
        };
        let view = self.graph.view(value);
        if view == View::Null {
            return Ok(());
        }
        if let Since::Version(since) = key.since
            && since > self.version
        {
            let message = format!(
                "/{} is PDF {since}; the file is PDF {}",
                key.name, self.version
            );
            self.defect(object, path, described, key, DefectKind::Version, message);
        }
        let matching: Vec<&'static Typed> = key
            .types
            .iter()
            .filter(|typed| matches(typed.kind, &view))
            .collect();
        if matching.is_empty() {
            let kinds: Vec<&str> = key.types.iter().map(|typed| typed.kind.name()).collect();
            let message = format!(
                "{} is a {}; expected {}",
                path,
                view.name(),
                kinds.join(" or ")
            );
            self.defect(object, path, described, key, DefectKind::Type, message);
            return Ok(());
        }
        let mut chosen = None;
        for typed in &matching {
            let admitted = match typed.condition {
                None => Some(true),
                Some(predicate) => {
                    let scope = self.scope(holder, Some(value));
                    self.decide(&scope, predicate)?
                }
            };
            if admitted != Some(false) {
                chosen = Some(*typed);
                break;
            }
        }
        let Some(typed) = chosen else {
            let condition = matching[0].condition.unwrap_or("");
            let message = format!("a {} value needs {condition}", view.name());
            self.defect(object, path, described, key, DefectKind::Version, message);
            return Ok(());
        };
        self.indirect(
            object,
            holder,
            described,
            key,
            typed,
            reference.is_some(),
            path,
        )?;
        self.values(object, holder, described, key, typed, value, path)?;
        let target = match typed.kind {
            Kind::NameTree | Kind::NumberTree => Some(Target::Tree(typed)),
            _ if typed.links.is_empty() => None,
            _ => match self.choose(typed.links, value) {
                Some(at) => Some(Target::Object(at)),
                None => {
                    self.report.unchecked += 1;
                    None
                }
            },
        };
        match (target, reference) {
            (Some(target), Some(number)) => self.enqueue(number, target),
            (Some(target), None) => stack.push((raw, target, path.to_owned())),
            (None, _) => {}
        }
        Ok(())
    }

    #[expect(clippy::too_many_arguments, reason = "the location of one value")]
    fn indirect(
        &mut self,
        object: Option<u32>,
        holder: &G::Value,
        described: &Object,
        key: &Key,
        typed: &Typed,
        indirect: bool,
        path: &str,
    ) -> Result<(), G::Error> {
        let (must_be_indirect, applies) = match typed.indirect {
            Condition::Never => return Ok(()),
            Condition::Always => (true, Some(true)),
            Condition::When(predicate) => {
                let must_be_indirect = if predicate.starts_with("fn:MustBeIndirect") {
                    true
                } else if predicate.starts_with("fn:MustBeDirect") {
                    false
                } else {
                    self.report.unchecked += 1;
                    return Ok(());
                };
                let scope = self.scope(holder, None);
                (must_be_indirect, self.decide(&scope, predicate)?)
            }
        };
        if applies == Some(true) && must_be_indirect != indirect {
            let message = if must_be_indirect {
                format!("/{} must be an indirect reference", key.name)
            } else {
                format!("/{} must be direct", key.name)
            };
            self.defect(object, path, described, key, DefectKind::Indirect, message);
        }
        Ok(())
    }

    #[expect(clippy::too_many_arguments, reason = "the location of one value")]
    fn values(
        &mut self,
        object: Option<u32>,
        holder: &G::Value,
        described: &Object,
        key: &Key,
        typed: &Typed,
        value: &G::Value,
        path: &str,
    ) -> Result<(), G::Error> {
        if typed.values.is_empty() {
            return Ok(());
        }
        let view = self.graph.view(value);
        let scope = self.scope(holder, Some(value));
        let (mut gated, mut undecided) = (false, false);
        for entry in typed.values {
            match admits(&scope, entry, &view)? {
                Admit::Yes => return Ok(()),
                Admit::No => {}
                Admit::Gated => gated = true,
                Admit::Unknown => undecided = true,
            }
        }
        let list = typed.values.join(", ");
        if gated {
            let message = format!("{view} is newer than PDF {}: {list}", self.version);
            self.defect(object, path, described, key, DefectKind::Version, message);
        } else if undecided {
            self.report.unchecked += 1;
        } else {
            let message = format!("{view} is not admitted by {list}");
            self.defect(object, path, described, key, DefectKind::Value, message);
        }
        Ok(())
    }

    /// The object type among `links` that a value is, chosen by the keys
    /// whose values tell the types apart, such as `/Type` and `/Subtype`.
    fn choose(&self, links: &[Link], value: &G::Value) -> Option<u16> {
        if let [only] = links {
            return Some(only.object);
        }
        if !matches!(self.graph.view(value), View::Dictionary | View::Stream) {
            return None;
        }
        // The most telling keys matched, then the fewest required keys
        // missing.
        let mut best: Option<(u16, usize, usize)> = None;
        let mut tied = false;
        for link in links {
            let target = link.target();
            let Some(score) = self.score(target, value) else {
                continue;
            };
            let missing = self.missing(target, value);
            match best {
                Some((_, top, fewest)) if (score, fewest) == (top, missing) => tied = true,
                Some((_, top, fewest)) if score < top || (score == top && missing > fewest) => {}
                _ => {
                    best = Some((link.object, score, missing));
                    tied = false;
                }
            }
        }
        match best {
            Some((_, 0, _)) if tied => None,
            Some((at, _, _)) => Some(at),
            None => None,
        }
    }

    /// How many keys that `described` always requires a dictionary lacks,
    /// leaving out the keys it may inherit.
    fn missing(&self, described: &Object, dict: &G::Value) -> usize {
        let lacks = |key: &&Key| {
            key.required == Condition::Always
                && !key.inheritable
                && !key.name.ends_with('*')
                && self.introduced(key)
                && self.graph.get(dict, key.name.as_bytes()).is_none()
        };
        described.keys.iter().filter(lacks).count()
    }

    /// How many telling keys of `described` a dictionary matches, or `None`
    /// when one of them contradicts it.
    fn score(&self, described: &Object, dict: &G::Value) -> Option<usize> {
        let mut score = 0;
        for key in described.keys {
            let names: Option<Vec<&str>> = key
                .types
                .iter()
                .filter(|typed| typed.kind == Kind::Name)
                .flat_map(|typed| typed.values.iter().map(|entry| literal(entry)))
                .collect();
            let Some(names) = names.filter(|names| !names.is_empty()) else {
                continue;
            };
            if key.name.ends_with('*') {
                continue;
            }
            match self
                .graph
                .get(dict, key.name.as_bytes())
                .map(|value| self.graph.view(value))
            {
                Some(View::Name(name)) if names.iter().any(|wanted| wanted.as_bytes() == name) => {
                    score += 1;
                }
                Some(View::Name(_)) => return None,
                None if key.required == Condition::Always => return None,
                _ => {}
            }
        }
        Some(score)
    }

    /// Checks a node of a name or number tree and queues its kids and its
    /// leaves.
    fn tree<'v>(
        &mut self,
        object: Option<u32>,
        node: &'v G::Value,
        typed: &'static Typed,
        path: &str,
        stack: &mut Vec<Frame<'v, G::Value>>,
    ) -> Result<(), G::Error> {
        self.order(object, node, typed.kind, path)?;
        if let Some(kids) = self.graph.get(node, b"Kids") {
            for item in self.items(kids, &join(path, "Kids"))? {
                match item {
                    Item::Reference(_, number) => self.enqueue(number, Target::Tree(typed)),
                    Item::Direct(_, value, at) => stack.push((value, Target::Tree(typed), at)),
                }
            }
        }
        if typed.links.is_empty() {
            return Ok(());
        }
        let leaves = if typed.kind == Kind::NameTree {
            "Names"
        } else {
            "Nums"
        };
        let Some(array) = self.graph.get(node, leaves.as_bytes()) else {
            return Ok(());
        };
        for item in self.items(array, &join(path, leaves))? {
            match item {
                Item::Reference(at, number) if at % 2 == 1 => {
                    let Some(value) = self.graph.resolve(number)? else {
                        continue;
                    };
                    match self.choose(typed.links, &value) {
                        Some(chosen) => self.enqueue(number, Target::Object(chosen)),
                        None => self.report.unchecked += 1,
                    }
                }
                Item::Direct(at, value, path) if at % 2 == 1 => {
                    match self.choose(typed.links, value) {
                        Some(chosen) => stack.push((value, Target::Object(chosen), path)),
                        None => self.report.unchecked += 1,
                    }
                }
                _ => {}
            }
        }
        Ok(())
    }

    fn invariant(
        &mut self,
        object: Option<u32>,
        path: &str,
        clause: &str,
        spec: &'static str,
        message: String,
    ) {
        if self.report.defects.len() >= MAX_DEFECTS {
            self.report.truncated = true;
            return;
        }
        self.report.defects.push(Defect {
            object,
            path: path.to_owned(),
            clause: clause.to_owned(),
            spec,
            kind: DefectKind::Invariant,
            message,
        });
    }

    /// Checks that page tree node `number` counts its kids' pages and that
    /// each kid names it as its `/Parent`. A kid without a `/Type` that
    /// tells is left to the model check.
    fn pages(&mut self, number: u32, node: &G::Value) -> Result<(), G::Error> {
        let Some(kids) = self.graph.get(node, b"Kids") else {
            return Ok(());
        };
        let mut sum = Some(0_i64);
        for item in self.items(kids, "Kids")? {
            let Item::Reference(at, kid) = item else {
                sum = None;
                continue;
            };
            let Some(value) = self.graph.resolve(kid)? else {
                sum = None;
                continue;
            };
            let count = match self.graph.get(&value, b"Type").map(|t| self.graph.view(t)) {
                Some(View::Name(b"Page")) => Some(1),
                Some(View::Name(b"Pages")) => {
                    match self.graph.get(&value, b"Count").map(|c| self.graph.view(c)) {
                        Some(View::Integer(count)) => Some(count),
                        _ => None,
                    }
                }
                _ => None,
            };
            sum = sum.zip(count).map(|(sum, count)| sum.saturating_add(count));
            let parent = self
                .graph
                .get(&value, b"Parent")
                .map(|p| self.graph.view(p));
            if count.is_some() && parent != Some(View::Reference(number)) {
                let found = match parent {
                    Some(View::Reference(other)) => format!("{other} 0 R"),
                    Some(other) => other.to_string(),
                    None => "nothing".to_owned(),
                };
                self.invariant(
                    Some(kid),
                    "Parent",
                    "PageTreeNode/Kids",
                    "7.7.3.2",
                    format!("kid {at} of page tree node {number} names {found} as its /Parent"),
                );
            }
        }
        let count = self.graph.get(node, b"Count").map(|c| self.graph.view(c));
        if let (Some(sum), Some(View::Integer(count))) = (sum, count)
            && sum != count
        {
            self.invariant(
                Some(number),
                "Count",
                "PageTreeNode/Count",
                "7.7.3.2",
                format!("/Count is {count}, but the kids hold {sum} pages"),
            );
        }
        Ok(())
    }

    /// Checks that each structure element kid of structure node `number`
    /// names it as its `/P`, and that a root's `/ParentTreeNextKey` is
    /// greater than every key of its parent tree.
    fn structure(&mut self, number: u32, node: &G::Value, name: &str) -> Result<(), G::Error> {
        if let Some(kids) = self.graph.get(node, b"K") {
            let items = match self.graph.view(kids) {
                View::Reference(kid) => vec![Item::Reference(0, kid)],
                _ => self.items(kids, "K")?,
            };
            for item in items {
                let Item::Reference(at, kid) = item else {
                    continue;
                };
                let Some(value) = self.graph.resolve(kid)? else {
                    continue;
                };
                let kind = self.graph.get(&value, b"Type").map(|t| self.graph.view(t));
                let element = match kind {
                    Some(View::Name(b"StructElem")) => true,
                    None => self.graph.get(&value, b"S").is_some(),
                    _ => false,
                };
                let parent = self.graph.get(&value, b"P").map(|p| self.graph.view(p));
                if element && parent != Some(View::Reference(number)) {
                    let found = match parent {
                        Some(View::Reference(other)) => format!("{other} 0 R"),
                        Some(other) => other.to_string(),
                        None => "nothing".to_owned(),
                    };
                    self.invariant(
                        Some(kid),
                        "P",
                        &format!("{name}/K"),
                        "14.7.2",
                        format!("kid {at} of structure node {number} names {found} as its /P"),
                    );
                }
            }
        }
        if name != "StructTreeRoot" {
            return Ok(());
        }
        let next = self
            .graph
            .get(node, b"ParentTreeNextKey")
            .map(|n| self.graph.view(n));
        let Some(View::Integer(next)) = next else {
            return Ok(());
        };
        let Some(tree) = self.graph.get(node, b"ParentTree") else {
            return Ok(());
        };
        let resolved;
        let tree = match self.graph.view(tree) {
            View::Reference(at) => match self.graph.resolve(at)? {
                Some(value) => {
                    resolved = value;
                    &*resolved
                }
                None => return Ok(()),
            },
            _ => tree,
        };
        let greatest = if let Some(nums) = self.graph.get(tree, b"Nums") {
            self.graph
                .elements(nums)
                .iter()
                .step_by(2)
                .filter_map(|key| match self.graph.view(key) {
                    View::Integer(key) => Some(key),
                    _ => None,
                })
                .max()
        } else {
            self.graph
                .get(tree, b"Limits")
                .and_then(|limits| self.graph.elements(limits).get(1))
                .and_then(|high| match self.graph.view(high) {
                    View::Integer(high) => Some(high),
                    _ => None,
                })
        };
        if let Some(greatest) = greatest
            && greatest >= next
        {
            self.invariant(
                Some(number),
                "ParentTreeNextKey",
                "StructTreeRoot/ParentTreeNextKey",
                "14.7.5.4",
                format!("/ParentTreeNextKey is {next}, but the parent tree holds key {greatest}"),
            );
        }
        Ok(())
    }

    /// Checks that an annotation whose normal appearance is a dictionary of
    /// states has an `/AS`. An `/AS` that names no normal state is allowed:
    /// it draws nothing, as an unchecked box often does.
    fn appearance_state(&mut self, number: u32, annotation: &G::Value, name: &str) {
        let normal = self
            .graph
            .get(annotation, b"AP")
            .and_then(|ap| self.graph.get(ap, b"N"));
        if normal.is_some_and(|normal| self.graph.view(normal) == View::Dictionary)
            && self.graph.get(annotation, b"AS").is_none()
        {
            self.invariant(
                Some(number),
                "AS",
                &format!("{name}/AS"),
                "12.5.5",
                "the normal appearance has states, but the annotation has no /AS".to_owned(),
            );
        }
    }

    /// Checks that the keys of a name or number tree node ascend strictly
    /// and that a leaf's `/Limits` are its first and last keys.
    fn order(
        &mut self,
        object: Option<u32>,
        node: &G::Value,
        kind: Kind,
        path: &str,
    ) -> Result<(), G::Error> {
        let (leaves, tree, spec) = if kind == Kind::NameTree {
            ("Names", "NameTree", "7.9.6")
        } else {
            ("Nums", "NumberTree", "7.9.7")
        };
        let Some(array) = self.graph.get(node, leaves.as_bytes()) else {
            return Ok(());
        };
        let resolved;
        let array = match self.graph.view(array) {
            View::Reference(number) => match self.graph.resolve(number)? {
                Some(value) => {
                    resolved = value;
                    &*resolved
                }
                None => return Ok(()),
            },
            _ => array,
        };
        if !matches!(self.graph.view(array), View::Array(_)) {
            return Ok(());
        }
        let keys: Vec<View<'_>> = self
            .graph
            .elements(array)
            .iter()
            .step_by(2)
            .map(|key| self.graph.view(key))
            .collect();
        if let Some(at) = keys.windows(2).position(|pair| !ascends(pair[0], pair[1])) {
            self.invariant(
                object,
                &join(path, &format!("{leaves}/{}", 2 * (at + 1))),
                &format!("{tree}/{leaves}"),
                spec,
                format!("key {} does not follow key {}", keys[at + 1], keys[at]),
            );
        }
        let limits = self.graph.get(node, b"Limits");
        if let (Some(limits), Some(first), Some(last)) = (limits, keys.first(), keys.last())
            && matches!(self.graph.view(limits), View::Array(2))
        {
            let bounds = self.graph.elements(limits);
            let (low, high) = (self.graph.view(&bounds[0]), self.graph.view(&bounds[1]));
            if !same(low, *first) || !same(high, *last) {
                self.invariant(
                    object,
                    &join(path, "Limits"),
                    &format!("{tree}/Limits"),
                    spec,
                    format!("/Limits are [{low} {high}], but the keys run from {first} to {last}"),
                );
            }
        }
        Ok(())
    }

    /// The elements of an array, by position. The direct elements of an
    /// array that is itself indirect are left out, since nothing keeps that
    /// array alive past this call.
    fn items<'v>(
        &self,
        array: &'v G::Value,
        path: &str,
    ) -> Result<Vec<Item<'v, G::Value>>, G::Error> {
        let mut out = Vec::new();
        match self.graph.view(array) {
            View::Array(_) => {
                for (at, item) in self.graph.elements(array).iter().enumerate() {
                    out.push(match self.graph.view(item) {
                        View::Reference(number) => Item::Reference(at, number),
                        _ => Item::Direct(at, item, join(path, &at.to_string())),
                    });
                }
            }
            View::Reference(number) => {
                if let Some(resolved) = self.graph.resolve(number)?
                    && matches!(self.graph.view(&resolved), View::Array(_))
                {
                    for (at, item) in self.graph.elements(&resolved).iter().enumerate() {
                        if let View::Reference(number) = self.graph.view(item) {
                            out.push(Item::Reference(at, number));
                        }
                    }
                }
            }
            _ => {}
        }
        Ok(out)
    }
}

/// An element of an array, with its position.
enum Item<'v, V> {
    Direct(usize, &'v V, String),
    Reference(usize, u32),
}

/// Whether tree key `next` may follow `key`: strictly greater, as bytes for
/// names and as integers for numbers. Keys of another type are left to the
/// model check.
fn ascends(key: View<'_>, next: View<'_>) -> bool {
    match (key, next) {
        (View::String(key), View::String(next)) => key < next,
        (View::Integer(key), View::Integer(next)) => key < next,
        _ => true,
    }
}

/// Whether a `/Limits` bound names `key`, where both are of a key's type.
fn same(bound: View<'_>, key: View<'_>) -> bool {
    match (bound, key) {
        (View::String(_) | View::Integer(_), View::String(_) | View::Integer(_)) => bound == key,
        _ => true,
    }
}

fn join(path: &str, key: &str) -> String {
    if path.is_empty() {
        key.to_owned()
    } else {
        format!("{path}/{key}")
    }
}

/// Whether a value of the document can be of `kind`.
const fn matches(kind: Kind, view: &View<'_>) -> bool {
    match kind {
        Kind::Array | Kind::Matrix | Kind::Rectangle => matches!(view, View::Array(_)),
        Kind::Bitmask | Kind::Integer => matches!(view, View::Integer(_)),
        Kind::Number => matches!(view, View::Integer(_) | View::Real(_)),
        Kind::Boolean => matches!(view, View::Boolean(_)),
        Kind::Null => matches!(view, View::Null),
        Kind::Name => matches!(view, View::Name(_)),
        Kind::Date | Kind::String | Kind::StringAscii | Kind::StringByte | Kind::StringText => {
            matches!(view, View::String(_))
        }
        Kind::Dictionary | Kind::NameTree | Kind::NumberTree => matches!(view, View::Dictionary),
        Kind::Stream => matches!(view, View::Stream),
    }
}

const WRAPPERS: [&str; 5] = [
    "fn:SinceVersion",
    "fn:BeforeVersion",
    "fn:IsPDFVersion",
    "fn:Deprecated",
    "fn:Extension",
];

/// The literal of a possible value: the value itself, or the value inside
/// version and extension wrappers. `None` for a constraint such as
/// `fn:Eval(...)`.
fn literal(entry: &str) -> Option<&str> {
    if !entry.starts_with("fn:") {
        return Some(entry);
    }
    let mut expr = parse(entry)?;
    loop {
        match expr {
            Expr::Call(name, mut args) if WRAPPERS.contains(&name) && args.len() == 2 => {
                expr = args.pop()?;
            }
            Expr::Word(text) => return Some(text),
            _ => return None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Admit {
    Yes,
    No,
    /// The value is admitted only by another version.
    Gated,
    Unknown,
}

/// Whether one possible value admits a value of the document.
fn admits<G: Graph>(
    scope: &Scope<'_, '_, G>,
    entry: &str,
    view: &View<'_>,
) -> Result<Admit, G::Error> {
    if !entry.starts_with("fn:") {
        return admits_expr(scope, &Expr::Word(entry), view);
    }
    match parse(entry) {
        Some(expr) => admits_expr(scope, &expr, view),
        None => Ok(Admit::Unknown),
    }
}

fn admits_expr<G: Graph>(
    scope: &Scope<'_, '_, G>,
    expr: &Expr<'_>,
    view: &View<'_>,
) -> Result<Admit, G::Error> {
    let scalar_view = matches!(
        view,
        View::Boolean(_) | View::Integer(_) | View::Real(_) | View::Name(_) | View::String(_)
    );
    match expr {
        Expr::Word(text) | Expr::Path(text) if !text.starts_with('@') => Ok(if !scalar_view {
            Admit::Unknown
        } else if equals(view, text) {
            Admit::Yes
        } else {
            Admit::No
        }),
        Expr::Call(name, args) if WRAPPERS.contains(name) && args.len() == 2 => {
            let inner = admits_expr(scope, &args[1], view)?;
            if inner != Admit::Yes {
                return Ok(inner);
            }
            let gate = args[0].text().and_then(version);
            let open = match *name {
                "fn:SinceVersion" => gate.map(|at| scope.version >= at),
                "fn:BeforeVersion" => gate.map(|at| scope.version < at),
                "fn:IsPDFVersion" => gate.map(|at| scope.version == at),
                _ => Some(true),
            };
            Ok(match open {
                Some(true) => Admit::Yes,
                Some(false) => Admit::Gated,
                None => Admit::Unknown,
            })
        }
        _ => Ok(match scope.eval(expr)? {
            Val::Bool(true) => Admit::Yes,
            Val::Bool(false) => Admit::No,
            _ => Admit::Unknown,
        }),
    }
}

/// Whether a scalar of the document equals a literal of the model.
fn equals(view: &View<'_>, literal: &str) -> bool {
    match (scalar(view), view) {
        (Val::Num(value), _) => literal
            .parse::<f64>()
            .is_ok_and(|wanted| (value - wanted).abs() < 1e-9),
        (Val::Bool(value), _) => literal == if value { "true" } else { "false" },
        (_, View::String(bytes)) => {
            literal
                .strip_prefix('(')
                .and_then(|rest| rest.strip_suffix(')'))
                .unwrap_or(literal)
                .as_bytes()
                == *bytes
        }
        (_, View::Name(bytes)) => literal.as_bytes() == *bytes,
        _ => false,
    }
}

#[cfg(test)]
mod tests;
