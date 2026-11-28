//! The expressions of the model's predicates (`fn:...`), evaluated against
//! one dictionary of a document.
//!
//! The predicates come from the generated descriptors, never from the
//! document, so parsing them may recurse.

use super::{Graph, View};
use crate::{Key, Object, Since, Version};

/// The value of an expression. `Unknown` stands for what the check cannot
/// decide: a predicate it does not implement, or a path it cannot follow.
#[derive(Clone, Debug, PartialEq)]
pub(super) enum Val {
    Unknown,
    Absent,
    Present,
    Bool(bool),
    Num(f64),
    Name(Vec<u8>),
}

impl Val {
    pub(super) const fn truth(&self) -> Option<bool> {
        match self {
            Self::Bool(value) => Some(*value),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum Op {
    Or,
    And,
    Eq,
    Ne,
    Lt,
    Le,
    Gt,
    Ge,
    Add,
    Sub,
    Mul,
    Mod,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Token<'s> {
    Word(&'s str),
    Open,
    Close,
    Comma,
    Op(Op),
}

#[derive(Debug)]
pub(super) enum Expr<'s> {
    /// A number, a name, `true`, or `false`, as the model writes it.
    Word(&'s str),
    /// A key path such as `@Subtype`, `Encrypt::@V`, or
    /// `trailer::Catalog::PieceInfo`.
    Path(&'s str),
    Call(&'s str, Vec<Self>),
    Neg(Box<Self>),
    Binary(Op, Box<Self>, Box<Self>),
}

impl<'s> Expr<'s> {
    /// The text of a word or a path, which functions such as `fn:IsPresent`
    /// read as a key path and value lists read as a literal.
    pub(super) const fn text(&self) -> Option<&'s str> {
        match self {
            Self::Word(text) | Self::Path(text) => Some(text),
            _ => None,
        }
    }
}

fn tokens(source: &str) -> Option<Vec<Token<'_>>> {
    let bytes = source.as_bytes();
    #[expect(
        clippy::disallowed_methods,
        reason = "bounded by the length of one static predicate of the generated model, never of the document"
    )]
    let mut out = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        let byte = bytes[at];
        let two = source.get(at..at + 2).unwrap_or("");
        let (token, width) = match byte {
            b' ' | b'\t' => {
                at += 1;
                continue;
            }
            b'(' => (Token::Open, 1),
            b')' => (Token::Close, 1),
            b',' => (Token::Comma, 1),
            _ if two == "==" => (Token::Op(Op::Eq), 2),
            _ if two == "!=" => (Token::Op(Op::Ne), 2),
            _ if two == "<=" => (Token::Op(Op::Le), 2),
            _ if two == ">=" => (Token::Op(Op::Ge), 2),
            _ if two == "&&" => (Token::Op(Op::And), 2),
            _ if two == "||" => (Token::Op(Op::Or), 2),
            b'<' => (Token::Op(Op::Lt), 1),
            b'>' => (Token::Op(Op::Gt), 1),
            b'+' => (Token::Op(Op::Add), 1),
            b'-' => (Token::Op(Op::Sub), 1),
            b'*' => (Token::Op(Op::Mul), 1),
            _ if is_word(byte) => {
                let end = scan_word(bytes, at);
                let word = &source[at..end];
                let token = if word == "mod" {
                    Token::Op(Op::Mod)
                } else {
                    Token::Word(word)
                };
                (token, end - at)
            }
            _ => return None,
        };
        #[expect(
            clippy::disallowed_methods,
            reason = "the tokens of one predicate of the generated model, never of the document"
        )]
        out.push(token);
        at += width;
    }
    Some(out)
}

const fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'@' | b':' | b'#' | b'\'')
}

/// Scans one word token starting at `at` (where `bytes[at]` satisfies
/// [`is_word`]), handling hyphenated names (`Identity-H`) and `@0*` wildcards.
fn scan_word(bytes: &[u8], at: usize) -> usize {
    let mut end = at;
    while end < bytes.len() {
        let next = bytes[end];
        // A hyphen joins names such as `Identity-H`.
        let joins = next == b'-'
            && bytes[at].is_ascii_alphabetic()
            && bytes.get(end + 1).is_some_and(u8::is_ascii_alphabetic);
        if next == b'*' && bytes[at] == b'@' && bytes[at + 1..end].iter().all(u8::is_ascii_digit) {
            end += 1;
            break;
        }
        if !is_word(next) && !joins {
            break;
        }
        end += 1;
    }
    end
}

/// Parses one predicate, or `None` for text the parser does not know.
pub(super) fn parse(source: &str) -> Option<Expr<'_>> {
    let tokens = tokens(source)?;
    let mut parser = Parser { tokens, at: 0 };
    let expr = parser.or()?;
    (parser.at == parser.tokens.len()).then_some(expr)
}

struct Parser<'s> {
    tokens: Vec<Token<'s>>,
    at: usize,
}

impl<'s> Parser<'s> {
    fn peek(&self) -> Option<Token<'s>> {
        self.tokens.get(self.at).copied()
    }

    fn eat(&mut self, token: Token<'s>) -> bool {
        let found = self.peek() == Some(token);
        if found {
            self.at += 1;
        }
        found
    }

    fn binary(
        &mut self,
        ops: &[Op],
        next: fn(&mut Self) -> Option<Expr<'s>>,
        repeat: bool,
    ) -> Option<Expr<'s>> {
        let mut left = next(self)?;
        while let Some(Token::Op(op)) = self.peek() {
            if !ops.contains(&op) {
                break;
            }
            self.at += 1;
            let right = next(self)?;
            left = Expr::Binary(op, Box::new(left), Box::new(right));
            if !repeat {
                break;
            }
        }
        Some(left)
    }

    fn or(&mut self) -> Option<Expr<'s>> {
        self.binary(&[Op::Or], Self::and, true)
    }

    fn and(&mut self) -> Option<Expr<'s>> {
        self.binary(&[Op::And], Self::compare, true)
    }

    fn compare(&mut self) -> Option<Expr<'s>> {
        let ops = [Op::Eq, Op::Ne, Op::Lt, Op::Le, Op::Gt, Op::Ge];
        self.binary(&ops, Self::sum, false)
    }

    fn sum(&mut self) -> Option<Expr<'s>> {
        self.binary(&[Op::Add, Op::Sub], Self::product, true)
    }

    fn product(&mut self) -> Option<Expr<'s>> {
        self.binary(&[Op::Mul, Op::Mod], Self::unary, true)
    }

    fn unary(&mut self) -> Option<Expr<'s>> {
        if self.eat(Token::Op(Op::Sub)) {
            return Some(Expr::Neg(Box::new(self.unary()?)));
        }
        match self.peek()? {
            Token::Open => {
                self.at += 1;
                let inner = self.or()?;
                self.eat(Token::Close).then_some(inner)
            }
            Token::Word(word) => {
                self.at += 1;
                if word.starts_with("fn:") {
                    if !self.eat(Token::Open) {
                        return None;
                    }
                    let mut args = Vec::new();
                    if !self.eat(Token::Close) {
                        loop {
                            #[expect(
                                clippy::disallowed_methods,
                                reason = "the arguments of one call in a predicate of the generated model"
                            )]
                            args.push(self.or()?);
                            if self.eat(Token::Close) {
                                break;
                            }
                            if !self.eat(Token::Comma) {
                                return None;
                            }
                        }
                    }
                    Some(Expr::Call(word, args))
                } else if word.starts_with('@') || word.contains("::") {
                    Some(Expr::Path(word))
                } else {
                    Some(Expr::Word(word))
                }
            }
            _ => None,
        }
    }
}

/// A PDF version written as `major.minor`.
pub(super) fn version(text: &str) -> Option<Version> {
    let (major, minor) = text.split_once('.')?;
    Some(Version {
        major: major.parse().ok()?,
        minor: minor.parse().ok()?,
    })
}

/// How a predicate expression is evaluated.
///
/// `Predicate` evaluates boolean guards (`IsRequired`, `DefaultValue`,
/// type/link conditions, `MustBeDirect`/`MustBeIndirect`, `RequiredValue`
/// triggers): an inapplicable version gate or ordering comparison against an
/// absent operand evaluates to `false`.
///
/// `Constraint` evaluates `PossibleValues` `fn:Eval(...)` value constraints:
/// a 2-argument version-gated sub-constraint (such as
/// `fn:BeforeVersion(1.3, fn:Eval(@Colors<=4))`) applies only when the
/// version gate is open and is vacuously `true` otherwise, and an ordering
/// comparison against an absent sibling (such as `@min <= @max` when `/max`
/// is omitted) evaluates to `Val::Unknown` rather than `false`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum EvalMode {
    Predicate,
    Constraint,
}

/// What a predicate is evaluated against: the dictionary that holds the
/// key, and the key's value.
pub(super) struct Scope<'g, 'v, G: Graph> {
    pub graph: &'g G,
    pub version: Version,
    pub dict: Option<&'v G::Value>,
    pub subject: Option<&'v G::Value>,
}

/// The value of a scalar of the document.
pub(super) fn scalar(view: &View<'_>) -> Val {
    match view {
        View::Null => Val::Absent,
        View::Boolean(value) => Val::Bool(*value),
        #[expect(clippy::cast_precision_loss, reason = "compared as numbers")]
        View::Integer(value) => Val::Num(*value as f64),
        View::Real(value) => Val::Num(*value),
        #[expect(
            clippy::disallowed_methods,
            reason = "copies one name or string that the graph read within the budget, for one comparison"
        )]
        View::Name(bytes) | View::String(bytes) => Val::Name(bytes.to_vec()),
        _ => Val::Present,
    }
}

/// Maximum number of entries inspected inside an `/Extensions` developer array.
const MAX_EXTENSION_ENTRIES: usize = 64;

impl<G: Graph> Scope<'_, '_, G> {
    /// Whether the model admits this key at the document's effective version.
    pub(super) fn introduced(
        &self,
        described: &Object,
        key: &Key,
    ) -> Result<Option<bool>, G::Error> {
        match key.since {
            Since::Version(since) => Ok(Some(self.version >= since)),
            Since::When(predicate) => match parse(predicate) {
                Some(expr) => Ok(self
                    .eval_since(&expr, described.name == "MeasureGEO")?
                    .truth()),
                None => Ok(None),
            },
        }
    }

    /// Evaluates a `SinceVersion` expression, where bare version words such as
    /// `2.0` test `self.version >= 2.0`.
    fn eval_since(&self, expr: &Expr<'_>, require_declaration: bool) -> Result<Val, G::Error> {
        Ok(match expr {
            Expr::Word(word) => match version(word) {
                Some(gate) => Val::Bool(self.version >= gate),
                None => self.eval(expr)?,
            },
            // A legacy GEO measure is valid only with the declared Adobe extension.
            // The ordinary evaluator leaves undeclared extensions undecided.
            Expr::Call("fn:Extension", args)
                if require_declaration
                    && args.first().and_then(Expr::text) == Some("ADBE_Extn3") =>
            {
                match self.eval(expr)? {
                    Val::Unknown => Val::Bool(false),
                    result => result,
                }
            }
            Expr::Call("fn:Eval", args) if args.len() == 1 => {
                self.eval_since(&args[0], require_declaration)?
            }
            Expr::Binary(op @ (Op::Or | Op::And), left, right) => {
                let left = self.eval_since(left, require_declaration)?;
                if *op == Op::And && left == Val::Bool(false) {
                    return Ok(left);
                }
                if *op == Op::Or && left == Val::Bool(true) {
                    return Ok(left);
                }
                let right = self.eval_since(right, require_declaration)?;
                match (left.truth(), right.truth()) {
                    (_, Some(value)) if value == (*op == Op::Or) => Val::Bool(value),
                    (Some(_), Some(value)) => Val::Bool(value),
                    _ => Val::Unknown,
                }
            }
            _ => self.eval(expr)?,
        })
    }

    pub(super) fn eval(&self, expr: &Expr<'_>) -> Result<Val, G::Error> {
        self.eval_with(expr, EvalMode::Predicate)
    }

    pub(super) fn eval_constraint(&self, expr: &Expr<'_>) -> Result<Val, G::Error> {
        self.eval_with(expr, EvalMode::Constraint)
    }

    fn eval_with(&self, expr: &Expr<'_>, mode: EvalMode) -> Result<Val, G::Error> {
        Ok(match expr {
            Expr::Word("true") => Val::Bool(true),
            Expr::Word("false") => Val::Bool(false),
            #[expect(
                clippy::disallowed_methods,
                reason = "copies one word of a predicate of the generated model"
            )]
            Expr::Word(word) => word.parse::<f64>().map_or_else(
                |_| Val::Name(word.trim_matches('\'').as_bytes().to_vec()),
                Val::Num,
            ),
            Expr::Path(path) => self.path(path, |value| {
                value.map_or(Val::Absent, |value| scalar(&self.graph.view(value)))
            })?,
            Expr::Neg(inner) => match self.eval_with(inner, mode)? {
                Val::Num(value) => Val::Num(-value),
                _ => Val::Unknown,
            },
            Expr::Binary(op, left, right) => self.binary(*op, left, right, mode)?,
            Expr::Call(name, args) => self.call(name, args, mode)?,
        })
    }

    fn binary(
        &self,
        op: Op,
        left: &Expr<'_>,
        right: &Expr<'_>,
        mode: EvalMode,
    ) -> Result<Val, G::Error> {
        let left = self.eval_with(left, mode)?;
        if op == Op::And && left == Val::Bool(false) {
            return Ok(left);
        }
        if op == Op::Or && left == Val::Bool(true) {
            return Ok(left);
        }
        let right = self.eval_with(right, mode)?;
        Ok(eval_binary_val(op, &left, &right, mode))
    }

    fn call(&self, name: &str, args: &[Expr<'_>], mode: EvalMode) -> Result<Val, G::Error> {
        let path = || args.first().and_then(Expr::text);
        Ok(match (name, args) {
            ("fn:Eval", [inner]) | ("fn:Deprecated", [_, inner]) => self.eval_with(inner, mode)?,
            ("fn:IsRequired", [inner]) => self.eval_with(inner, EvalMode::Predicate)?,
            ("fn:Not", [inner]) => eval_not(&self.eval_with(inner, mode)?),
            ("fn:MustBeDirect" | "fn:MustBeIndirect", []) => Val::Bool(true),
            ("fn:MustBeDirect" | "fn:MustBeIndirect", [inner]) => {
                self.eval_with(inner, EvalMode::Predicate)?
            }
            ("fn:IsPresent", [_]) => self.present(path())?,
            ("fn:IsPresent", [_, then]) => match self.present(path())? {
                Val::Bool(false) => Val::Bool(true),
                Val::Bool(true) => self.eval_with(then, mode)?,
                other => other,
            },
            ("fn:SinceVersion" | "fn:BeforeVersion" | "fn:IsPDFVersion", [at, rest @ ..]) => {
                let Some(gate) = at.text().and_then(version) else {
                    return Ok(Val::Unknown);
                };
                let open = version_gate_open(name, self.version, gate);
                match rest {
                    [] => Val::Bool(open),
                    [_] if !open => apply_version_gate(false, None, mode),
                    [inner] => apply_version_gate(true, Some(self.eval_with(inner, mode)?), mode),
                    _ => Val::Unknown,
                }
            }
            ("fn:Extension", [ext, rest @ ..]) => {
                let Some(ext_name) = ext.text() else {
                    return Ok(Val::Unknown);
                };
                match rest {
                    [] => match self.extension(ext_name, None)? {
                        Some(active) => Val::Bool(active),
                        None => Val::Unknown,
                    },
                    [second] => {
                        if let Some(gate) = second.text().and_then(version) {
                            if self.version < gate {
                                Val::Bool(false)
                            } else {
                                match self.extension(ext_name, None)? {
                                    Some(active) => Val::Bool(active),
                                    None => Val::Unknown,
                                }
                            }
                        } else {
                            match self.extension(ext_name, None)? {
                                Some(false) => Val::Bool(false),
                                Some(true) => match self.eval_with(second, mode)? {
                                    Val::Bool(value) => Val::Bool(value),
                                    Val::Unknown => Val::Unknown,
                                    _ => Val::Bool(true),
                                },
                                None => Val::Unknown,
                            }
                        }
                    }
                    _ => Val::Unknown,
                }
            }
            ("fn:ArrayLength", [_]) => self.path(path().unwrap_or(""), |value| {
                match value.map(|value| self.graph.view(value)) {
                    #[expect(clippy::cast_precision_loss, reason = "a length")]
                    Some(View::Array(len)) => Val::Num(len as f64),
                    Some(_) => Val::Unknown,
                    None => Val::Absent,
                }
            })?,
            ("fn:StringLength", [_]) => self.path(path().unwrap_or(""), |value| {
                match value.map(|value| self.graph.view(value)) {
                    #[expect(clippy::cast_precision_loss, reason = "a length")]
                    Some(View::String(bytes)) => Val::Num(bytes.len() as f64),
                    Some(_) => Val::Unknown,
                    None => Val::Absent,
                }
            })?,
            ("fn:RectWidth" | "fn:RectHeight", [_]) => {
                let first = usize::from(name == "fn:RectHeight");
                self.path(path().unwrap_or(""), |value| {
                    value.map_or(Val::Absent, |value| self.extent(value, first))
                })?
            }
            ("fn:Contains", [_, wanted]) => {
                let wanted = self.eval_with(wanted, mode)?;
                if matches!(wanted, Val::Unknown | Val::Present) {
                    return Ok(Val::Unknown);
                }
                self.try_path(path().unwrap_or(""), |value| {
                    let Some(value) = value else {
                        return Ok(Val::Bool(false));
                    };
                    match self.graph.view(value) {
                        View::Null => Ok(Val::Bool(false)),
                        View::Array(_) => {
                            for item in self.graph.elements(value) {
                                let mut owned = None;
                                let Some(resolved) = self.deref_once(item, &mut owned)? else {
                                    continue;
                                };
                                let matched = match (scalar(&self.graph.view(resolved)), &wanted) {
                                    (Val::Num(a), Val::Num(b)) => (a - b).abs() < 1e-9,
                                    (a, b) => a == *b,
                                };
                                if matched {
                                    return Ok(Val::Bool(true));
                                }
                            }
                            Ok(Val::Bool(false))
                        }
                        view @ (View::Boolean(_)
                        | View::Integer(_)
                        | View::Real(_)
                        | View::Name(_)
                        | View::String(_)) => {
                            let matched = match (scalar(&view), &wanted) {
                                (Val::Num(a), Val::Num(b)) => (a - b).abs() < 1e-9,
                                (a, b) => a == *b,
                            };
                            Ok(Val::Bool(matched))
                        }
                        _ => Ok(Val::Unknown),
                    }
                })?
            }
            ("fn:BitSet" | "fn:BitClear", [bit]) => self.bits(bit, bit, name == "fn:BitSet")?,
            ("fn:BitsSet" | "fn:BitsClear", [low, high]) => {
                self.bits(low, high, name == "fn:BitsSet")?
            }
            _ => Val::Unknown,
        })
    }

    fn present(&self, path: Option<&str>) -> Result<Val, G::Error> {
        let Some(path) = path else {
            return Ok(Val::Unknown);
        };
        self.path(path, |value| {
            let present = value.is_some_and(|value| !matches!(self.graph.view(value), View::Null));
            Val::Bool(present)
        })
    }

    /// The width (`first` 0) or height (`first` 1) of a rectangle.
    fn extent(&self, value: &G::Value, first: usize) -> Val {
        if !matches!(self.graph.view(value), View::Array(4)) {
            return Val::Unknown;
        }
        let items = self.graph.elements(value);
        match (
            scalar(&self.graph.view(&items[first])),
            scalar(&self.graph.view(&items[first + 2])),
        ) {
            (Val::Num(a), Val::Num(b)) => Val::Num((b - a).abs()),
            _ => Val::Unknown,
        }
    }

    /// Whether the bits `low` to `high` of the subject, counted from 1, are
    /// all set (`set`) or all clear.
    fn bits(&self, low: &Expr<'_>, high: &Expr<'_>, set: bool) -> Result<Val, G::Error> {
        let (Val::Num(low), Val::Num(high)) = (self.eval(low)?, self.eval(high)?) else {
            return Ok(Val::Unknown);
        };
        let Some(View::Integer(value)) = self.subject.map(|value| self.graph.view(value)) else {
            return Ok(Val::Unknown);
        };
        if !(1.0..=32.0).contains(&low) || !(low..=32.0).contains(&high) {
            return Ok(Val::Unknown);
        }
        #[expect(
            clippy::cast_possible_truncation,
            clippy::cast_sign_loss,
            reason = "1 to 32"
        )]
        let (low, high) = (low as u32, high as u32);
        let all = (low..=high).all(|bit| ((value >> (bit - 1)) & 1 == 1) == set);
        Ok(Val::Bool(all))
    }

    /// Follows `path` and hands its last value, resolved, to `then`.
    fn path(
        &self,
        path: &str,
        then: impl FnOnce(Option<&G::Value>) -> Val,
    ) -> Result<Val, G::Error> {
        self.try_path(path, |value| Ok(then(value)))
    }

    /// Follows `path` and hands its last value, resolved, to a fallible `then`.
    fn try_path(
        &self,
        path: &str,
        then: impl FnOnce(Option<&G::Value>) -> Result<Val, G::Error>,
    ) -> Result<Val, G::Error> {
        #[expect(
            clippy::disallowed_methods,
            reason = "the segments of one path in a predicate of the generated model"
        )]
        let mut segments: Vec<&str> = path.split("::").collect();
        if let [subject] = segments.as_slice()
            && subject.starts_with('@')
            && subject.ends_with('*')
            && let Some(value) = self.subject
        {
            return then(Some(value));
        }
        let start = match segments.first() {
            Some(&"trailer") => {
                segments.remove(0);
                if segments.first() == Some(&"Catalog") {
                    segments[0] = "Root";
                }
                self.graph.trailer()
            }
            Some(&"parent") => return Ok(Val::Unknown),
            _ => match self.dict {
                Some(dict) => dict,
                None => return Ok(Val::Unknown),
            },
        };
        if segments.is_empty() {
            return Ok(Val::Unknown);
        }
        self.walk(start, &segments, then)
    }

    fn walk(
        &self,
        current: &G::Value,
        segments: &[&str],
        then: impl FnOnce(Option<&G::Value>) -> Result<Val, G::Error>,
    ) -> Result<Val, G::Error> {
        let Some((segment, rest)) = segments.split_first() else {
            return Ok(Val::Unknown);
        };
        let key = segment.trim_start_matches('@');
        let child = match self.graph.view(current) {
            View::Dictionary | View::Stream => self.graph.get(current, key.as_bytes()),
            View::Array(_) => key
                .parse::<usize>()
                .ok()
                .and_then(|at| self.graph.elements(current).get(at)),
            _ => None,
        };
        let Some(child) = child else {
            return then(None);
        };
        if let View::Reference(_) = self.graph.view(child) {
            let resolved = self.graph.target(child)?;
            return match (&resolved, rest.is_empty()) {
                (Some(resolved), false) => self.walk(resolved, rest, then),
                (resolved, _) => then(resolved.as_deref()),
            };
        }
        if rest.is_empty() {
            then(Some(child))
        } else {
            self.walk(child, rest, then)
        }
    }

    /// Checks whether developer or ISO extension `name` is explicitly declared in
    /// `Catalog["Extensions"]`, returning `None` when `/Extensions` does not
    /// declare it (since PDF files may use extension features without a
    /// `/Extensions` dictionary).
    pub(super) fn extension(
        &self,
        name: &str,
        candidate: Option<&str>,
    ) -> Result<Option<bool>, G::Error> {
        let Some(root_raw) = self.graph.get(self.graph.trailer(), b"Root") else {
            return Ok(None);
        };
        let mut root_owned = None;
        let Some(catalog) = self.deref_once(root_raw, &mut root_owned)? else {
            return Ok(None);
        };
        let Some(ext_raw) = self.graph.get(catalog, b"Extensions") else {
            return Ok(None);
        };
        // ETSI EN 319 142-1 §5.6 registers these PDF 1.7 declarations for the
        // CAdES subfilter. ETSI_PAdES is the model name, not a catalog key.
        // ISO 32000-1 §7.12.1 requires /Extensions, its developer extension
        // dictionaries, and their entries to be direct objects, so indirect
        // /Extensions or vendor dicts intentionally do not satisfy this check
        // (do not deref_once here; see PR #1089 and test_signature_structures.py).
        if name == "ETSI_PAdES" && candidate == Some("ETSI.CAdES.detached") {
            let base = Version { major: 1, minor: 7 };
            let present = self.version >= base
                && matches!(self.graph.view(ext_raw), View::Dictionary)
                && [(b"ESIC".as_slice(), 2), (b"ADBE".as_slice(), 8)]
                    .iter()
                    .any(|(prefix, level)| {
                        self.graph.get(ext_raw, prefix).is_some_and(|entry| {
                            matches!(self.graph.view(entry), View::Dictionary)
                                && self.matches_dev_dict(entry, Some(base), *level, false)
                        })
                    });
            return Ok(present.then_some(true));
        }
        let mut ext_owned = None;
        let Some(extensions) = self.deref_once(ext_raw, &mut ext_owned)? else {
            return Ok(None);
        };
        if !matches!(self.graph.view(extensions), View::Dictionary) {
            return Ok(None);
        }
        let declared = match name {
            "ADBE_Extn3" => self.has_dev_extension(
                extensions,
                b"ADBE",
                Some(Version { major: 1, minor: 7 }),
                3,
                false,
            )?,
            "ISO_21812" => self.has_dev_extension(extensions, b"GTSm", None, 1, true)?,
            _ if name.starts_with("ISO_TS_") => {
                let Ok(code) = name["ISO_TS_".len()..].parse::<i64>() else {
                    return Ok(None);
                };
                self.has_dev_extension(
                    extensions,
                    b"ISO_",
                    Some(Version { major: 2, minor: 0 }),
                    code,
                    true,
                )?
            }
            _ => {
                if let Some(raw_vendor) = self.graph.get(extensions, name.as_bytes()) {
                    let mut vendor_owned = None;
                    if let Some(vendor) = self.deref_once(raw_vendor, &mut vendor_owned)? {
                        matches!(self.graph.view(vendor), View::Dictionary)
                            || matches!(self.graph.view(vendor), View::Array(len) if len > 0)
                    } else {
                        false
                    }
                } else {
                    false
                }
            }
        };
        Ok(declared.then_some(true))
    }

    fn deref_once<'a>(
        &self,
        raw: &'a G::Value,
        slot: &'a mut Option<G::Owned>,
    ) -> Result<Option<&'a G::Value>, G::Error> {
        if let View::Reference(_) = self.graph.view(raw) {
            *slot = self.graph.target(raw)?;
            Ok(slot.as_deref())
        } else {
            Ok(Some(raw))
        }
    }

    fn has_dev_extension(
        &self,
        extensions: &G::Value,
        prefix: &[u8],
        wanted_base: Option<Version>,
        wanted_level: i64,
        exact_level: bool,
    ) -> Result<bool, G::Error> {
        let Some(entry_raw) = self.graph.get(extensions, prefix) else {
            return Ok(false);
        };
        let mut entry_owned = None;
        let Some(entry) = self.deref_once(entry_raw, &mut entry_owned)? else {
            return Ok(false);
        };
        match self.graph.view(entry) {
            View::Dictionary => {
                Ok(self.matches_dev_dict(entry, wanted_base, wanted_level, exact_level))
            }
            View::Array(_) => {
                for item_raw in self
                    .graph
                    .elements(entry)
                    .iter()
                    .take(MAX_EXTENSION_ENTRIES)
                {
                    let mut item_owned = None;
                    if let Some(item) = self.deref_once(item_raw, &mut item_owned)?
                        && matches!(self.graph.view(item), View::Dictionary)
                        && self.matches_dev_dict(item, wanted_base, wanted_level, exact_level)
                    {
                        return Ok(true);
                    }
                }
                Ok(false)
            }
            _ => Ok(false),
        }
    }

    fn matches_dev_dict(
        &self,
        dict: &G::Value,
        wanted_base: Option<Version>,
        wanted_level: i64,
        exact_level: bool,
    ) -> bool {
        let Some(View::Integer(level)) = self
            .graph
            .get(dict, b"ExtensionLevel")
            .map(|v| self.graph.view(v))
        else {
            return false;
        };
        let level_ok = if exact_level {
            level == wanted_level
        } else {
            level >= wanted_level
        };
        if !level_ok {
            return false;
        }
        let Some(wanted) = wanted_base else {
            return true;
        };
        let Some(View::Name(base_bytes)) = self
            .graph
            .get(dict, b"BaseVersion")
            .map(|v| self.graph.view(v))
        else {
            return false;
        };
        std::str::from_utf8(base_bytes).ok().and_then(version) == Some(wanted)
    }
}

/// Three-valued Kleene negation for `fn:Not(...)`.
fn eval_not(value: &Val) -> Val {
    match value.truth() {
        Some(flag) => Val::Bool(!flag),
        None => Val::Unknown,
    }
}

/// Pure binary evaluation over evaluated [`Val`] operands in `mode`.
#[inline]
fn eval_binary_val(op: Op, left: &Val, right: &Val, mode: EvalMode) -> Val {
    match op {
        Op::And | Op::Or => eval_logic_val(op, left, right),
        Op::Eq | Op::Ne => eval_eq_val(op, left, right),
        Op::Lt | Op::Le | Op::Gt | Op::Ge => eval_order_val(op, left, right, mode),
        Op::Add | Op::Sub | Op::Mul | Op::Mod => eval_arith_val(op, left, right),
    }
}

fn eval_logic_val(op: Op, left: &Val, right: &Val) -> Val {
    match (left.truth(), right.truth()) {
        (Some(value), _) if value == (op == Op::Or) => Val::Bool(value),
        (_, Some(value)) if value == (op == Op::Or) => Val::Bool(value),
        (Some(_), Some(value)) => Val::Bool(value),
        _ => Val::Unknown,
    }
}

fn eval_eq_val(op: Op, left: &Val, right: &Val) -> Val {
    match (left, right) {
        (Val::Unknown | Val::Present, _) | (_, Val::Unknown | Val::Present) => Val::Unknown,
        (Val::Absent, _) | (_, Val::Absent) => Val::Bool(op == Op::Ne),
        (Val::Num(a), Val::Num(b)) => Val::Bool(((a - b).abs() < 1e-9) == (op == Op::Eq)),
        (Val::Bool(a), Val::Bool(b)) => Val::Bool((a == b) == (op == Op::Eq)),
        (Val::Name(a), Val::Name(b)) => Val::Bool((a == b) == (op == Op::Eq)),
        _ => Val::Bool(op == Op::Ne),
    }
}

fn eval_order_val(op: Op, left: &Val, right: &Val, mode: EvalMode) -> Val {
    match (left, right) {
        (Val::Num(a), Val::Num(b)) => Val::Bool(match op {
            Op::Lt => a < b,
            Op::Le => a <= b,
            Op::Gt => a > b,
            _ => a >= b,
        }),
        (Val::Absent, _) | (_, Val::Absent) => match mode {
            EvalMode::Predicate => Val::Bool(false),
            EvalMode::Constraint => Val::Unknown,
        },
        _ => Val::Unknown,
    }
}

fn eval_arith_val(op: Op, left: &Val, right: &Val) -> Val {
    match (left, right) {
        (Val::Num(a), Val::Num(b)) => Val::Num(match op {
            Op::Add => a + b,
            Op::Sub => a - b,
            Op::Mul => a * b,
            _ if *b == 0.0 => return Val::Unknown,
            _ => a % b,
        }),
        _ => Val::Unknown,
    }
}

/// Decides whether a version gate (`fn:SinceVersion`, `fn:BeforeVersion`,
/// `fn:IsPDFVersion`) is open at `current`.
fn version_gate_open(name: &str, current: Version, gate: Version) -> bool {
    match name {
        "fn:SinceVersion" => current >= gate,
        "fn:BeforeVersion" => current < gate,
        _ => current == gate,
    }
}

/// Applies the 2-argument version-gate semantics in `Predicate` vs
/// `Constraint` mode.
fn apply_version_gate(open: bool, inner: Option<Val>, mode: EvalMode) -> Val {
    if !open {
        return Val::Bool(mode == EvalMode::Constraint);
    }
    match inner {
        Some(Val::Bool(value)) => Val::Bool(value),
        Some(Val::Unknown) => Val::Unknown,
        _ => Val::Bool(true),
    }
}

#[cfg(kani)]
mod kani_proofs {
    use super::*;

    fn any_logic_val() -> Val {
        let tag: u8 = kani::any();
        kani::assume(tag < 5);
        match tag {
            0 => Val::Unknown,
            1 => Val::Absent,
            2 => Val::Present,
            3 => Val::Bool(false),
            _ => Val::Bool(true),
        }
    }

    /// Compares non-`Name`, non-`Num` results without dragging `Vec<u8>` or
    /// `f64` equality into CBMC.
    fn assert_same_tri(a: &Val, b: &Val) {
        let rank = |v: &Val| -> u8 {
            match v {
                Val::Unknown => 0,
                Val::Absent => 1,
                Val::Present => 2,
                Val::Bool(false) => 3,
                Val::Bool(true) => 4,
                Val::Num(_) | Val::Name(_) => panic!("unexpected non-logic Val"),
            }
        };
        assert_eq!(rank(a), rank(b));
    }

    /// Proves Kleene 3-valued logic laws for `eval_binary_val` (`And`, `Or`)
    /// and `eval_not` across all truth-valued and non-boolean [`Val`] variants.
    #[kani::proof]
    fn kani_kleene_three_valued_logic_laws() {
        let a = any_logic_val();
        let b = any_logic_val();
        let mode = if kani::any() {
            EvalMode::Predicate
        } else {
            EvalMode::Constraint
        };

        // Commutativity of And and Or.
        assert_same_tri(
            &eval_binary_val(Op::And, &a, &b, mode),
            &eval_binary_val(Op::And, &b, &a, mode),
        );
        assert_same_tri(
            &eval_binary_val(Op::Or, &a, &b, mode),
            &eval_binary_val(Op::Or, &b, &a, mode),
        );

        // Short-circuit absorption laws.
        assert_same_tri(
            &eval_binary_val(Op::And, &Val::Bool(false), &b, mode),
            &Val::Bool(false),
        );
        assert_same_tri(
            &eval_binary_val(Op::And, &a, &Val::Bool(false), mode),
            &Val::Bool(false),
        );
        assert_same_tri(
            &eval_binary_val(Op::Or, &Val::Bool(true), &b, mode),
            &Val::Bool(true),
        );
        assert_same_tri(
            &eval_binary_val(Op::Or, &a, &Val::Bool(true), mode),
            &Val::Bool(true),
        );

        // De Morgan's laws.
        assert_same_tri(
            &eval_not(&eval_binary_val(Op::And, &a, &b, mode)),
            &eval_binary_val(Op::Or, &eval_not(&a), &eval_not(&b), mode),
        );
        assert_same_tri(
            &eval_not(&eval_binary_val(Op::Or, &a, &b, mode)),
            &eval_binary_val(Op::And, &eval_not(&a), &eval_not(&b), mode),
        );

        // Double negation agrees with `Val::truth()`.
        let expected_double = a.truth().map_or(Val::Unknown, Val::Bool);
        assert_same_tri(&eval_not(&eval_not(&a)), &expected_double);
    }

    /// Proves equality/inequality complementarity and absent-operand ordering
    /// semantics across `Predicate` vs `Constraint` mode (#1158).
    #[kani::proof]
    #[kani::unwind(2)]
    fn kani_equality_and_absent_ordering_invariants() {
        let a = any_logic_val();
        let b = any_logic_val();

        let eq = eval_eq_val(Op::Eq, &a, &b);
        let ne = eval_eq_val(Op::Ne, &a, &b);
        match (&a, &b) {
            (Val::Unknown | Val::Present, _) | (_, Val::Unknown | Val::Present) => {
                assert_same_tri(&eq, &Val::Unknown);
                assert_same_tri(&ne, &Val::Unknown);
            }
            _ => match (eq.truth(), ne.truth()) {
                (Some(e), Some(n)) => assert_eq!(e, !n),
                _ => panic!("expected decided Bool for non-Unknown/Present Eq/Ne"),
            },
        }

        // Absent ordering semantics (#1158): Predicate => false, Constraint => Unknown.
        let pick: u8 = kani::any();
        kani::assume(pick < 4);
        let op = match pick {
            0 => Op::Lt,
            1 => Op::Le,
            2 => Op::Gt,
            _ => Op::Ge,
        };
        if matches!(&a, Val::Absent) || matches!(&b, Val::Absent) {
            assert_same_tri(
                &eval_order_val(op, &a, &b, EvalMode::Predicate),
                &Val::Bool(false),
            );
            assert_same_tri(
                &eval_order_val(op, &a, &b, EvalMode::Constraint),
                &Val::Unknown,
            );
        } else {
            assert_same_tri(
                &eval_order_val(op, &a, &b, EvalMode::Predicate),
                &Val::Unknown,
            );
            assert_same_tri(
                &eval_order_val(op, &a, &b, EvalMode::Constraint),
                &Val::Unknown,
            );
        }
    }

    /// Proves version-gate partition, monotonicity, and `Predicate` vs
    /// `Constraint` vacuous-truth semantics.
    #[kani::proof]
    fn kani_version_gate_monotonicity() {
        let v1 = Version {
            major: kani::any(),
            minor: kani::any(),
        };
        let v2 = Version {
            major: kani::any(),
            minor: kani::any(),
        };
        let gate = Version {
            major: kani::any(),
            minor: kani::any(),
        };

        let since1 = version_gate_open("fn:SinceVersion", v1, gate);
        let before1 = version_gate_open("fn:BeforeVersion", v1, gate);
        let exact1 = version_gate_open("fn:IsPDFVersion", v1, gate);
        assert_ne!(since1, before1);
        if exact1 {
            assert!(since1 && !before1);
        }

        if v1 <= v2 {
            let since2 = version_gate_open("fn:SinceVersion", v2, gate);
            let before2 = version_gate_open("fn:BeforeVersion", v2, gate);
            assert!(!since1 || since2);
            assert!(!before2 || before1);
        }

        assert_same_tri(
            &apply_version_gate(false, None, EvalMode::Constraint),
            &Val::Bool(true),
        );
        assert_same_tri(
            &apply_version_gate(false, None, EvalMode::Predicate),
            &Val::Bool(false),
        );
    }

    /// Proves `scan_word` (hyphenated names and `@0*` wildcards) always
    /// advances strictly within `bytes` (`at < end <= bytes.len()`) without panicking.
    #[kani::proof]
    #[kani::unwind(8)]
    fn kani_expr_scan_word_bounds_no_panic() {
        let raw: [u8; 6] = kani::any();
        let len: usize = kani::any();
        let at: usize = kani::any();
        kani::assume(len <= raw.len() && at < len);
        kani::assume(is_word(raw[at]));
        let end = scan_word(&raw[..len], at);
        assert!(end > at && end <= len);
    }

    /// Proves `version` parses `major.minor` cleanly without panicking on
    /// arbitrary bounded ASCII strings.
    #[kani::proof]
    #[kani::unwind(6)]
    fn kani_expr_version_parse_no_panic() {
        let raw: [u8; 4] = kani::any();
        let len: usize = kani::any();
        kani::assume(len <= raw.len());
        for b in &raw[..len] {
            kani::assume(b.is_ascii());
        }
        let text = std::str::from_utf8(&raw[..len]).expect("ASCII is valid UTF-8");
        let _ = version(text);
    }
}
