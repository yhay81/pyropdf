//! The structural check (DESIGN.md section 8): a walk from the trailer
//! through the descriptors that reports each departure with its object, its
//! key, the Arlington clause, and the specification clause.
//!
//! The walk is forward and iterative. It visits an indirect object at most
//! [`MAX_TARGETS`] times, once per object type it is reached as, and asks the
//! [`Graph`] to charge every visit, so a deep or cyclic graph stays within
//! the operation's budget (F6). Predicates that the check does not decide
//! are counted in [`Report::unchecked`] instead of being reported.

mod author;
mod expr;
mod interpret;

pub use interpret::{Interpretation, interpret, interpret_reference, tree_target};

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

/// Maximum direct-dictionary nesting inspected inside a `/ParentTree` node.
const MAX_DIRECT_TREE_DEPTH: usize = 64;

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
    /// Resolve a stored reference, including its generation when the graph
    /// records one. A reference chain and its budget belong to this graph.
    fn target(&self, reference: &Self::Value) -> Result<Option<Self::Owned>, Self::Error> {
        match self.view(reference) {
            View::Reference(number) => self.resolve(number),
            _ => Ok(None),
        }
    }
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
    let mut walk = Walk::new(graph);
    walk.object(None, graph.trailer(), Target::Object(trailer))?;
    let size = match graph
        .get(graph.trailer(), b"Size")
        .map(|size| graph.view(size))
    {
        // A `/Size` that is not positive is a value defect already.
        Some(View::Integer(size)) if size > 0 => Some(size),
        _ => None,
    };
    finish(walk, size)
}

/// Whether the document's effective version and extensions admit creation
/// of an object with this descriptor. Existing deprecated objects can still
/// be checked and edited.
///
/// # Errors
///
/// Returns graph resolution and budget errors.
pub fn authorable<G: Graph>(graph: &G, model: &Object, value: &G::Value) -> Result<bool, G::Error> {
    let mut walk = Walk::new(graph);
    for key in model.keys {
        if !key.deprecated.is_some_and(|v| graph.version() >= v)
            && walk.introduced(value, model, key)? == Some(true)
        {
            return Ok(true);
        }
    }
    Ok(false)
}

/// Check a standalone object and the graph reached through its declared links
/// before publishing an authored replacement. Broken or ambiguous links are
/// errors in an authored object, including one unreachable from the trailer.
///
/// # Errors
///
/// Returns a graph error when resolution or the operation budget refuses work.
pub fn check_authored<G: Graph>(
    graph: &G,
    name: &str,
    number: u32,
    value: &G::Value,
) -> Result<Option<Report>, G::Error> {
    let Some(at) = OBJECTS
        .binary_search_by(|item| item.name.cmp(name))
        .ok()
        .and_then(|at| u16::try_from(at).ok())
    else {
        return Ok(None);
    };
    let mut walk = Walk::new(graph);
    walk.authoring = true;
    graph.visit(true)?;
    walk.object(Some(number), value, Target::Object(at))?;
    finish(walk, None).map(Some)
}

fn finish<G: Graph>(mut walk: Walk<'_, G>, size: Option<i64>) -> Result<Report, G::Error> {
    let mut met = HashSet::new();
    while let Some((number, target)) = walk.queue.pop() {
        #[expect(
            clippy::disallowed_methods,
            reason = "one number per object visited, and the graph charges each first visit below"
        )]
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
        walk.graph.visit(first)?;
        walk.report.objects += usize::from(first);
        if let Some(value) = walk.graph.resolve(number)? {
            walk.object(Some(number), &value, target)?;
        }
    }
    let parent_trees = std::mem::take(&mut walk.parent_trees);
    for mut check in parent_trees {
        let mut seen = HashSet::new();
        while let Some(node_number) = check.pending.pop() {
            #[expect(
                clippy::disallowed_methods,
                reason = "each node is deduplicated and bounded by the graph's visit budget below"
            )]
            let fresh = seen.insert(node_number);
            if !fresh {
                continue;
            }
            walk.graph.visit(false)?;
            if let Some(value) = walk.graph.resolve(node_number)? {
                inspect_parent_tree_node(
                    walk.graph,
                    &value,
                    &mut check.greatest,
                    &mut check.pending,
                    0,
                )?;
            }
        }
        walk.check_parent_tree_next_key(check.root, check.next, check.greatest);
    }
    Ok(walk.report)
}

struct ParentTreeCheck {
    root: u32,
    next: i64,
    greatest: Option<i64>,
    pending: Vec<u32>,
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
    authoring: bool,
    queue: Vec<(u32, Target)>,
    visited: HashSet<(u32, usize)>,
    targets: HashMap<u32, usize>,
    /// The page tree nodes and structure elements whose kids are checked,
    /// once each although a node is checked as more than one type.
    counted: HashSet<u32>,
    /// Deferred `/ParentTreeNextKey` checks whose `/ParentTree` reaches
    /// indirect nodes, run after the main walk validates object budgets.
    parent_trees: Vec<ParentTreeCheck>,
}

/// A direct value waiting to be checked as `Target`, at `String`.
type Frame<'v, V> = (&'v V, Target, String);

impl<'g, G: Graph> Walk<'g, G> {
    fn new(graph: &'g G) -> Self {
        Self {
            graph,
            version: graph.version(),
            report: Report::default(),
            authoring: false,
            queue: Vec::new(),
            visited: HashSet::new(),
            targets: HashMap::new(),
            counted: HashSet::new(),
            parent_trees: Vec::new(),
        }
    }

    #[expect(
        clippy::disallowed_methods,
        reason = "each call follows a reference of a value that the graph read within the budget, \
                  and an object is queued as MAX_TARGETS types at most"
    )]
    fn enqueue(&mut self, number: u32, target: Target) {
        let count = self.targets.entry(number).or_default();
        if *count < MAX_TARGETS && self.visited.insert((number, target.id())) {
            *count += 1;
            self.queue.push((number, target));
        }
    }

    /// Whether object `number` is new to `counted`, which it joins: the walk
    /// checks a node's kids once, although it checks the node as more than
    /// one type.
    #[expect(
        clippy::disallowed_methods,
        reason = "one number per object visited, and the graph charges each first visit"
    )]
    fn first_count(&mut self, number: u32) -> bool {
        self.counted.insert(number)
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
        #[expect(
            clippy::disallowed_methods,
            reason = "the report keeps MAX_DEFECTS defects at most, checked above"
        )]
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
        if self.authoring
            && path.is_empty()
            && let Some(number) = object
        {
            self.backlinks(number, dict, described.name)?;
        }
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
            && self.first_count(number)
        {
            self.pages(number, dict)?;
        }
        if described.name.starts_with("Annot")
            && (!path.is_empty() || object.is_some_and(|number| self.first_count(number)))
        {
            self.appearance_state(object, path, dict, described.name)?;
        }
        if matches!(described.name, "StructTreeRoot" | "StructElem")
            && let Some(number) = object
            && path.is_empty()
            && self.first_count(number)
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
        #[expect(
            clippy::disallowed_methods,
            reason = "the repeating keys of one descriptor of the generated model"
        )]
        let repeating: Vec<&'static Key> = described
            .keys
            .iter()
            .filter(|key| key.name.len() > 1 && key.name.ends_with('*'))
            .collect();
        for key in described.keys {
            let Ok(at) = key.name.parse::<usize>() else {
                continue;
            };
            if at >= items.len()
                && key.required == Condition::Always
                && !key.deprecated.is_some_and(|dep| self.version >= dep)
                && self.introduced(array, described, key)? == Some(true)
            {
                let message = format!("element {at} is required");
                self.defect(object, path, described, key, DefectKind::Required, message);
            }
        }
        let repeat_start = repeating
            .first()
            .and_then(|key| key.name.trim_end_matches('*').parse::<usize>().ok());
        let revision_pairs = matches!(
            described.name,
            "ArrayOfAttributeRevisions" | "ArrayOfClassNamesRevisions"
        );
        let mut after_payload = false;
        for (at, item) in items.iter().enumerate() {
            let named = described.key(&at.to_string());
            let repeated = if revision_pairs {
                // Each attribute or class may have an integer revision. Omitting
                // one does not shift the type of the next payload (14.7.6.3).
                let owned = self.graph.target(item)?;
                let view = self.graph.view(owned.as_deref().unwrap_or(item));
                let revision = after_payload && matches!(view, View::Integer(_));
                let payload = described.key("0*");
                after_payload =
                    payload.is_some_and(|key| key.types.iter().any(|ty| matches(ty.kind, &view)));
                if revision {
                    described.key("1*")
                } else {
                    payload
                }
            } else {
                repeat_start.and_then(|start| {
                    let offset = at.checked_sub(start)?;
                    repeating.get(offset % repeating.len()).copied()
                })
            };
            let key = named.or(repeated).or_else(|| described.key("*"));
            if let Some(key) = key {
                let at = join(path, &at.to_string());
                self.value(object, array, described, key, item, &at, stack)?;
            }
        }
        Ok(())
    }

    /// Whether the file's version and declared extensions introduce `key`.
    fn introduced(
        &mut self,
        holder: &G::Value,
        described: &Object,
        key: &Key,
    ) -> Result<Option<bool>, G::Error> {
        let decided = self.scope(holder, None).introduced(described, key)?;
        if decided.is_none() {
            self.report.unchecked += 1;
        }
        Ok(decided)
    }

    fn required(
        &mut self,
        object: Option<u32>,
        dict: &G::Value,
        described: &Object,
        key: &Key,
        path: &str,
    ) -> Result<(), G::Error> {
        if key.deprecated.is_some_and(|dep| self.version >= dep) {
            return Ok(());
        }
        if self.introduced(dict, described, key)? != Some(true) {
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
        if !required
            || (key.inheritable
                && self
                    .inheritable(dict, key.name.as_bytes(), |_| Some(()))?
                    .is_some())
        {
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

    /// Interpret the first non-null value of an inheritable key on the
    /// object or its parents. A null indirect value is also omitted.
    fn inheritable<T>(
        &self,
        dict: &G::Value,
        key: &[u8],
        read: impl Fn(View<'_>) -> Option<T>,
    ) -> Result<Option<T>, G::Error> {
        let mut parent: Option<G::Owned> = None;
        for depth in 0..=MAX_INHERITANCE {
            let current = parent.as_deref().unwrap_or(dict);
            if let Some(raw) = self.graph.get(current, key) {
                let resolved;
                let view = match self.graph.view(raw) {
                    View::Reference(number) => {
                        resolved = self.graph.resolve(number)?;
                        resolved
                            .as_deref()
                            .map_or(View::Null, |value| self.graph.view(value))
                    }
                    view => view,
                };
                if view != View::Null {
                    return Ok(read(view));
                }
            }
            let Some(View::Reference(number)) = self
                .graph
                .get(current, b"Parent")
                .map(|value| self.graph.view(value))
            else {
                break;
            };
            if depth == MAX_INHERITANCE {
                break;
            }
            parent = self.graph.resolve(number)?;
            if parent.is_none() {
                break;
            }
        }
        Ok(None)
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
                owned = if self.authoring {
                    self.graph.target(raw)?
                } else {
                    self.graph.resolve(number)?
                };
                match &owned {
                    Some(value) => value,
                    None => {
                        if self.authoring {
                            self.defect(
                                object,
                                path,
                                described,
                                key,
                                DefectKind::Indirect,
                                format!("{path} contains an unresolved reference to {number}"),
                            );
                        }
                        return Ok(());
                    }
                }
            }
            None => raw,
        };
        let view = self.graph.view(value);
        if view == View::Null {
            return Ok(());
        }
        if self.introduced(holder, described, key)? == Some(false) {
            let needed = match key.since {
                Since::Version(since) => format!("PDF {since}"),
                Since::When(predicate) => predicate.to_owned(),
            };
            let message = format!(
                "/{} is {needed}; the file is PDF {}",
                key.name, self.version
            );
            self.defect(object, path, described, key, DefectKind::Version, message);
        }
        #[expect(
            clippy::disallowed_methods,
            reason = "the types of one key of the generated model"
        )]
        let matching: Vec<&'static Typed> = key
            .types
            .iter()
            .filter(|typed| matches(typed.kind, &view))
            .collect();
        if matching.is_empty() {
            #[expect(
                clippy::disallowed_methods,
                reason = "the types of one key of the generated model"
            )]
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
        let authored = if self.authoring {
            match interpret_reference(self.graph, holder, described, key, Some(value), reference)? {
                Interpretation::Refused(reason) => {
                    self.defect(
                        object,
                        path,
                        described,
                        key,
                        DefectKind::Value,
                        reason.to_owned(),
                    );
                    return Ok(());
                }
                Interpretation::Value(_, Some(name)) => typed
                    .links
                    .iter()
                    .find(|link| link.target().name == name)
                    .map(|link| link.object),
                _ => None,
            }
        } else {
            None
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
            _ if self.authoring => authored.map(Target::Object),
            _ => match self.choose(typed.links, value)? {
                Some(at) => Some(Target::Object(at)),
                None => {
                    self.report.unchecked += 1;
                    None
                }
            },
        };
        match (target, reference) {
            (Some(target), Some(number)) => self.enqueue(number, target),
            #[expect(
                clippy::disallowed_methods,
                reason = "one frame per direct value of the object, which the graph read within the budget"
            )]
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
        let decision = possible(&scope, typed.values, &view)?;
        if decision == Admit::Yes {
            return Ok(());
        }
        let list = typed.values.join(", ");
        if decision == Admit::Gated {
            let message = format!("{view} is newer than PDF {}: {list}", self.version);
            self.defect(object, path, described, key, DefectKind::Version, message);
        } else if decision == Admit::Unknown {
            self.report.unchecked += 1;
        } else {
            let message = format!("{view} is not admitted by {list}");
            self.defect(object, path, described, key, DefectKind::Value, message);
        }
        Ok(())
    }

    /// The object type among `links` that a value is, chosen by the keys
    /// whose values tell the types apart, such as `/Subtype` and `/ShadingType`.
    fn choose(&self, links: &[Link], value: &G::Value) -> Result<Option<u16>, G::Error> {
        if let [only] = links {
            return Ok(Some(only.object));
        }
        if !matches!(self.graph.view(value), View::Dictionary | View::Stream) {
            return Ok(None);
        }
        // A merged field and widget is checked as an annotation through the page,
        // and as a signature field through AcroForm so that its V is visited.
        if let Some(sig) = links.iter().find(|link| link.target().name == "FieldSig")
            && [b"FT".as_slice(), b"V", b"DV"]
                .iter()
                .any(|key| self.graph.get(value, key).is_some())
            && self.inheritable(value, b"FT", |view| {
                Some(matches!(view, View::Name(b"Sig")))
            })? == Some(true)
        {
            return Ok(Some(sig.object));
        }
        if let Some(button) = self.button_field(links, value)? {
            return Ok(Some(button));
        }
        // The most telling keys matched, then the fewest required keys
        // missing.
        let mut best: Option<(u16, usize, usize)> = None;
        let mut tied = false;
        for link in links {
            let target = link.target();
            let Some(score) = self.score(target, value)? else {
                continue;
            };
            let missing = self.missing(target, value)?;
            match best {
                Some((_, top, fewest)) if (score, fewest) == (top, missing) => tied = true,
                Some((_, top, fewest)) if score < top || (score == top && missing > fewest) => {}
                _ => {
                    best = Some((link.object, score, missing));
                    tied = false;
                }
            }
        }
        Ok(match best {
            Some((_, 0, _)) if tied => None,
            Some((at, _, _)) => Some(at),
            None => None,
        })
    }

    fn button_field(&self, links: &[Link], value: &G::Value) -> Result<Option<u16>, G::Error> {
        // All button subtypes have /FT /Btn. ISO 32000-2 Table 229
        // distinguishes them by bits 16 (Radio) and 17 (Pushbutton).
        // A widget-only kid has no field keys and is checked as AnnotWidget.
        if links
            .iter()
            .any(|link| link.target().name == "FieldBtnRadio")
            && [b"FT".as_slice(), b"Ff", b"T", b"Kids"]
                .iter()
                .any(|key| self.graph.get(value, key).is_some())
            && self.inheritable(value, b"FT", |view| {
                Some(matches!(view, View::Name(b"Btn")))
            })? == Some(true)
        {
            let flags = self
                .inheritable(value, b"Ff", |view| match view {
                    View::Integer(flags) => Some(flags),
                    _ => None,
                })?
                .unwrap_or(0);
            let subtype = if flags & (1 << 16) != 0 {
                "FieldBtnPush"
            } else if flags & (1 << 15) != 0 {
                "FieldBtnRadio"
            } else {
                "FieldBtnCheckbox"
            };
            if let Some(link) = links.iter().find(|link| link.target().name == subtype) {
                return Ok(Some(link.object));
            }
        }
        Ok(None)
    }

    /// How many keys that `described` always requires a dictionary lacks,
    /// leaving out the keys it may inherit.
    fn missing(&self, described: &Object, dict: &G::Value) -> Result<usize, G::Error> {
        let scope = self.scope(dict, None);
        let mut missing = 0;
        for key in described.keys {
            if key.required != Condition::Always
                || key.inheritable
                || key.name.ends_with('*')
                || key.deprecated.is_some_and(|dep| self.version >= dep)
                || self.graph.get(dict, key.name.as_bytes()).is_some()
            {
                continue;
            }
            let active = scope.introduced(described, key)? == Some(true);
            missing += usize::from(active);
        }
        Ok(missing)
    }

    /// How many telling keys of `described` a dictionary matches, or `None`
    /// when one of them contradicts it.
    fn score(&self, described: &Object, dict: &G::Value) -> Result<Option<usize>, G::Error> {
        let mut score = 0;
        for key in described.keys {
            if key.name.ends_with('*') {
                continue;
            }
            // A fixed integer identifies a type, such as ShadingType or
            // FunctionType. Multi-valued constraints such as bit depths
            // must be validated after selection: a missing or invalid bit
            // depth must not hide the defects of an otherwise known type.
            let mut integers = key
                .types
                .iter()
                .filter(|typed| typed.kind == Kind::Integer && typed.values.len() == 1)
                .filter_map(|typed| literal(typed.values[0])?.parse::<i64>().ok())
                .peekable();
            #[expect(
                clippy::disallowed_methods,
                reason = "the values of one key of the generated model"
            )]
            let names: Option<Vec<&str>> = key
                .types
                .iter()
                .filter(|typed| typed.kind == Kind::Name)
                .flat_map(|typed| typed.values.iter().map(|entry| literal(entry)))
                .collect();
            if integers.peek().is_none() && names.as_ref().is_none_or(Vec::is_empty) {
                continue;
            }
            let raw = self.graph.get(dict, key.name.as_bytes());
            let resolved;
            let view = match raw {
                Some(value) if matches!(self.graph.view(value), View::Reference(_)) => {
                    resolved = self.graph.target(value)?;
                    resolved.as_deref().map(|v| self.graph.view(v))
                }
                other => other.map(|v| self.graph.view(v)),
            };
            if integers.peek().is_some()
                && let Some(View::Integer(actual)) = view
            {
                if !integers.any(|expected| expected == actual) {
                    return Ok(None);
                }
                score += 1;
            }
            let Some(names) = names.filter(|names| !names.is_empty()) else {
                continue;
            };
            match view {
                Some(View::Name(name)) if names.iter().any(|wanted| wanted.as_bytes() == name) => {
                    score += 1;
                }
                Some(View::Name(_)) => return Ok(None),
                None if key.required == Condition::Always => return Ok(None),
                _ => {}
            }
        }
        Ok(Some(score))
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
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "one frame per direct value of the object, which the graph read within the budget"
                    )]
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
                    match self.choose(typed.links, &value)? {
                        Some(chosen) => self.enqueue(number, Target::Object(chosen)),
                        None => self.report.unchecked += 1,
                    }
                }
                Item::Direct(at, value, path) if at % 2 == 1 => {
                    match self.choose(typed.links, value)? {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "one frame per direct value of the object, which the graph read within the budget"
                        )]
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
        #[expect(
            clippy::disallowed_methods,
            reason = "the report keeps MAX_DEFECTS defects at most, checked above"
        )]
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
            match self.graph.view(kids) {
                View::Array(_) => {
                    for (at, item) in self.graph.elements(kids).iter().enumerate() {
                        self.graph.visit(false)?;
                        if let View::Reference(kid) = self.graph.view(item)
                            && let Some(value) = self.graph.target(item)?
                        {
                            self.check_structure_kid(number, name, at, kid, &value);
                        }
                    }
                }
                View::Reference(target_id) => {
                    self.graph.visit(false)?;
                    if let Some(resolved) = self.graph.target(kids)? {
                        if matches!(self.graph.view(&resolved), View::Array(_)) {
                            for (at, item) in self.graph.elements(&resolved).iter().enumerate() {
                                self.graph.visit(false)?;
                                if let View::Reference(kid) = self.graph.view(item)
                                    && let Some(value) = self.graph.target(item)?
                                {
                                    self.check_structure_kid(number, name, at, kid, &value);
                                }
                            }
                        } else {
                            self.check_structure_kid(number, name, 0, target_id, &resolved);
                        }
                    }
                }
                _ => {}
            }
        }
        if name != "StructTreeRoot" {
            return Ok(());
        }
        let Some(raw_next) = self.graph.get(node, b"ParentTreeNextKey") else {
            return Ok(());
        };
        let mut next_owned = None;
        let Some(View::Integer(next)) =
            deref(self.graph, raw_next, &mut next_owned)?.map(|n| self.graph.view(n))
        else {
            return Ok(());
        };
        let Some(raw_tree) = self.graph.get(node, b"ParentTree") else {
            return Ok(());
        };
        let mut greatest = None;
        let mut pending = Vec::new();
        match self.graph.view(raw_tree) {
            View::Reference(at) => {
                #[expect(
                    clippy::disallowed_methods,
                    reason = "one indirect parent-tree root per visited StructTreeRoot"
                )]
                pending.push(at);
            }
            View::Dictionary => {
                inspect_parent_tree_node(self.graph, raw_tree, &mut greatest, &mut pending, 0)?;
            }
            _ => return Ok(()),
        }
        if pending.is_empty() {
            self.check_parent_tree_next_key(number, next, greatest);
        } else {
            #[expect(
                clippy::disallowed_methods,
                reason = "at most one deferred parent-tree check per visited StructTreeRoot"
            )]
            self.parent_trees.push(ParentTreeCheck {
                root: number,
                next,
                greatest,
                pending,
            });
        }
        Ok(())
    }

    fn check_structure_kid(
        &mut self,
        parent_number: u32,
        parent_name: &str,
        at: usize,
        kid: u32,
        value: &G::Value,
    ) {
        let kind = self.graph.get(value, b"Type").map(|t| self.graph.view(t));
        let element = match kind {
            Some(View::Name(b"StructElem")) => true,
            None => self.graph.get(value, b"S").is_some(),
            _ => false,
        };
        let parent = self.graph.get(value, b"P").map(|p| self.graph.view(p));
        if element && parent != Some(View::Reference(parent_number)) {
            let found = match parent {
                Some(View::Reference(other)) => format!("{other} 0 R"),
                Some(other) => other.to_string(),
                None => "nothing".to_owned(),
            };
            self.invariant(
                Some(kid),
                "P",
                &format!("{parent_name}/K"),
                "14.7.2",
                format!("kid {at} of structure node {parent_number} names {found} as its /P"),
            );
        }
    }

    fn check_parent_tree_next_key(&mut self, number: u32, next: i64, greatest: Option<i64>) {
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
    }

    /// Checks that an annotation whose appearance dictionary contains any
    /// state subdictionary (`/N`, `/R`, or `/D`) has a non-null `/AS`
    /// (ISO 32000-2 Table 166 and 12.5.5). An `/AS` that names no state is
    /// allowed: it draws nothing, as an unchecked box often does.
    fn appearance_state(
        &mut self,
        object: Option<u32>,
        path: &str,
        annotation: &G::Value,
        name: &str,
    ) -> Result<(), G::Error> {
        let Some(raw_ap) = self.graph.get(annotation, b"AP") else {
            return Ok(());
        };
        let mut ap_owned = None;
        let Some(ap) = deref(self.graph, raw_ap, &mut ap_owned)? else {
            return Ok(());
        };
        if self.graph.view(ap) != View::Dictionary {
            return Ok(());
        }
        let mut has_states = false;
        for entry_key in [b"N".as_slice(), b"R".as_slice(), b"D".as_slice()] {
            if let Some(raw_entry) = self.graph.get(ap, entry_key) {
                let mut entry_owned = None;
                if let Some(entry) = deref(self.graph, raw_entry, &mut entry_owned)?
                    && self.graph.view(entry) == View::Dictionary
                {
                    has_states = true;
                    break;
                }
            }
        }
        if !has_states {
            return Ok(());
        }
        let has_as = match self.graph.get(annotation, b"AS") {
            Some(raw_as) => {
                let mut as_owned = None;
                deref(self.graph, raw_as, &mut as_owned)?
                    .is_some_and(|value| self.graph.view(value) != View::Null)
            }
            None => false,
        };
        if !has_as {
            self.invariant(
                object,
                &join(path, "AS"),
                &format!("{name}/AS"),
                "12.5.5",
                "an appearance entry has states, but the annotation has no /AS".to_owned(),
            );
        }
        Ok(())
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
        #[expect(
            clippy::disallowed_methods,
            reason = "half the elements of one array that the graph read within the budget"
        )]
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
                    #[expect(
                        clippy::disallowed_methods,
                        reason = "one item per element of an array that the graph read within the budget"
                    )]
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
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "one item per element of an array that the graph read within the budget"
                            )]
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

fn deref<'a, G: Graph>(
    graph: &G,
    raw: &'a G::Value,
    slot: &'a mut Option<G::Owned>,
) -> Result<Option<&'a G::Value>, G::Error> {
    if let View::Reference(_) = graph.view(raw) {
        *slot = graph.target(raw)?;
        Ok(slot.as_deref())
    } else {
        Ok(Some(raw))
    }
}

/// Reads the keys and bounds of one `/ParentTree` node, queuing indirect
/// `/Kids` for the iterative walk and descending directly nested kids up to
/// [`MAX_DIRECT_TREE_DEPTH`].
fn inspect_parent_tree_node<G: Graph>(
    graph: &G,
    node: &G::Value,
    greatest: &mut Option<i64>,
    pending: &mut Vec<u32>,
    depth: usize,
) -> Result<(), G::Error> {
    if depth > MAX_DIRECT_TREE_DEPTH || graph.view(node) != View::Dictionary {
        return Ok(());
    }
    if let Some(raw_nums) = graph.get(node, b"Nums") {
        let mut owned = None;
        if let Some(nums) = deref(graph, raw_nums, &mut owned)?
            && matches!(graph.view(nums), View::Array(_))
        {
            for key in graph.elements(nums).iter().step_by(2) {
                let mut key_owned = None;
                if let Some(resolved) = deref(graph, key, &mut key_owned)?
                    && let View::Integer(value) = graph.view(resolved)
                {
                    *greatest = Some(greatest.map_or(value, |cur| cur.max(value)));
                }
            }
        }
    }
    if let Some(raw_limits) = graph.get(node, b"Limits") {
        let mut owned = None;
        if let Some(limits) = deref(graph, raw_limits, &mut owned)?
            && matches!(graph.view(limits), View::Array(_))
        {
            for bound in graph.elements(limits).iter().take(2) {
                let mut bound_owned = None;
                if let Some(resolved) = deref(graph, bound, &mut bound_owned)?
                    && let View::Integer(value) = graph.view(resolved)
                {
                    *greatest = Some(greatest.map_or(value, |cur| cur.max(value)));
                }
            }
        }
    }
    if let Some(raw_kids) = graph.get(node, b"Kids") {
        let mut owned = None;
        if let Some(kids) = deref(graph, raw_kids, &mut owned)?
            && matches!(graph.view(kids), View::Array(_))
        {
            for kid in graph.elements(kids) {
                match graph.view(kid) {
                    View::Reference(number) => {
                        #[expect(
                            clippy::disallowed_methods,
                            reason = "follows references from a budgeted array; the deferred walk deduplicates and charges each node"
                        )]
                        pending.push(number);
                    }
                    View::Dictionary => {
                        inspect_parent_tree_node(graph, kid, greatest, pending, depth + 1)?;
                    }
                    _ => {}
                }
            }
        }
    }
    Ok(())
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
    /// The condition of a RequiredValue holds, making the value mandatory.
    Required(bool),
}

/// Combine the alternatives of one PossibleValues cell. An active
/// RequiredValue overrides the ordinary alternatives regardless of order.
fn possible<G: Graph>(
    scope: &Scope<'_, '_, G>,
    entries: &[&str],
    view: &View<'_>,
) -> Result<Admit, G::Error> {
    let mut decision = Admit::No;
    for entry in entries {
        match admits(scope, entry, view)? {
            Admit::Required(false) => return Ok(Admit::No),
            Admit::Yes | Admit::Required(true) => decision = Admit::Yes,
            Admit::Gated if decision != Admit::Yes => decision = Admit::Gated,
            Admit::Unknown if decision == Admit::No => decision = Admit::Unknown,
            _ => {}
        }
    }
    Ok(decision)
}

/// Whether one possible value admits a value of the document.
fn admits<G: Graph>(
    scope: &Scope<'_, '_, G>,
    entry: &str,
    view: &View<'_>,
) -> Result<Admit, G::Error> {
    if entry == "*" {
        return Ok(Admit::Yes);
    }
    if let Some(literal) = entry
        .strip_prefix('[')
        .and_then(|rest| rest.strip_suffix(']'))
        && let View::Array(len) = view
        && let Some(value) = scope.subject
    {
        let tokens = literal.split_ascii_whitespace();
        if *len != tokens.clone().count() {
            return Ok(Admit::No);
        }
        for (item, token) in scope.graph.elements(value).iter().zip(tokens) {
            if !equals(&scope.graph.view(item), token) {
                return Ok(Admit::No);
            }
        }
        return Ok(Admit::Yes);
    }
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
        Expr::Call("fn:RequiredValue", args) if args.len() == 2 => {
            let candidate = admits_expr(scope, &args[1], view)?;
            Ok(if scope.eval(&args[0])?.truth() == Some(true) {
                Admit::Required(candidate == Admit::Yes)
            } else {
                candidate
            })
        }
        Expr::Call(name, args) if WRAPPERS.contains(name) && args.len() == 2 => {
            let inner = admits_expr(scope, &args[1], view)?;
            if !matches!(inner, Admit::Yes | Admit::Required(_)) {
                return Ok(inner);
            }
            let gate = args[0].text().and_then(version);
            let open = match *name {
                "fn:SinceVersion" => gate.map(|at| scope.version >= at),
                "fn:BeforeVersion" => gate.map(|at| scope.version < at),
                "fn:IsPDFVersion" => gate.map(|at| scope.version == at),
                "fn:Extension" => match args[0].text() {
                    Some(ext_name) => scope.extension(ext_name, args[1].text())?,
                    None => None,
                },
                _ => Some(true),
            };
            Ok(match open {
                Some(true) => inner,
                // A version gate on a requirement states when the value
                // becomes mandatory, not when that value first exists. R=6
                // is required for V=5 in PDF 2.0 and occurs in Adobe's
                // earlier PDF 1.7 extension level 8 as well.
                Some(false) if inner == Admit::Required(true) => Admit::Unknown,
                Some(false) if inner == Admit::Required(false) => Admit::No,
                Some(false) => Admit::Gated,
                None => Admit::Unknown,
            })
        }
        _ => Ok(match scope.eval_constraint(expr)? {
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
                .or_else(|| {
                    literal
                        .strip_prefix('\'')
                        .and_then(|rest| rest.strip_suffix('\''))
                })
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
