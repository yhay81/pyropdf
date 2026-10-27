//! The expressions of the model's predicates (`fn:...`), evaluated against
//! one dictionary of a document.
//!
//! The predicates come from the generated descriptors, never from the
//! document, so parsing them may recurse.

use super::{Graph, View};
use crate::Version;

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
    let mut out = Vec::new();
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
                let mut end = at;
                while end < bytes.len() {
                    let next = bytes[end];
                    // A hyphen joins names such as `Identity-H`.
                    let joins = next == b'-'
                        && bytes[at].is_ascii_alphabetic()
                        && bytes.get(end + 1).is_some_and(u8::is_ascii_alphabetic);
                    if !is_word(next) && !joins {
                        break;
                    }
                    end += 1;
                }
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
        out.push(token);
        at += width;
    }
    Some(out)
}

const fn is_word(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'@' | b':' | b'#' | b'\'')
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
        View::Name(bytes) | View::String(bytes) => Val::Name(bytes.to_vec()),
        _ => Val::Present,
    }
}

impl<G: Graph> Scope<'_, '_, G> {
    pub(super) fn eval(&self, expr: &Expr<'_>) -> Result<Val, G::Error> {
        Ok(match expr {
            Expr::Word("true") => Val::Bool(true),
            Expr::Word("false") => Val::Bool(false),
            Expr::Word(word) => word
                .parse::<f64>()
                .map_or_else(|_| Val::Name(word.as_bytes().to_vec()), Val::Num),
            Expr::Path(path) => self.path(path, |value| {
                value.map_or(Val::Absent, |value| scalar(&self.graph.view(value)))
            })?,
            Expr::Neg(inner) => match self.eval(inner)? {
                Val::Num(value) => Val::Num(-value),
                _ => Val::Unknown,
            },
            Expr::Binary(op, left, right) => self.binary(*op, left, right)?,
            Expr::Call(name, args) => self.call(name, args)?,
        })
    }

    fn binary(&self, op: Op, left: &Expr<'_>, right: &Expr<'_>) -> Result<Val, G::Error> {
        let left = self.eval(left)?;
        if op == Op::And && left == Val::Bool(false) {
            return Ok(left);
        }
        if op == Op::Or && left == Val::Bool(true) {
            return Ok(left);
        }
        let right = self.eval(right)?;
        Ok(match op {
            Op::And | Op::Or => match (left.truth(), right.truth()) {
                (_, Some(value)) if value == (op == Op::Or) => Val::Bool(value),
                (Some(_), Some(value)) => Val::Bool(value),
                _ => Val::Unknown,
            },
            Op::Eq | Op::Ne => match (&left, &right) {
                (Val::Unknown | Val::Present, _) | (_, Val::Unknown | Val::Present) => Val::Unknown,
                (Val::Absent, _) | (_, Val::Absent) => Val::Bool(op == Op::Ne),
                (Val::Num(a), Val::Num(b)) => Val::Bool(((a - b).abs() < 1e-9) == (op == Op::Eq)),
                _ => Val::Bool((left == right) == (op == Op::Eq)),
            },
            Op::Lt | Op::Le | Op::Gt | Op::Ge => match (&left, &right) {
                (Val::Num(a), Val::Num(b)) => Val::Bool(match op {
                    Op::Lt => a < b,
                    Op::Le => a <= b,
                    Op::Gt => a > b,
                    _ => a >= b,
                }),
                (Val::Absent, _) | (_, Val::Absent) => Val::Bool(false),
                _ => Val::Unknown,
            },
            Op::Add | Op::Sub | Op::Mul | Op::Mod => match (&left, &right) {
                (Val::Num(a), Val::Num(b)) => Val::Num(match op {
                    Op::Add => a + b,
                    Op::Sub => a - b,
                    Op::Mul => a * b,
                    _ if *b == 0.0 => return Ok(Val::Unknown),
                    _ => a % b,
                }),
                _ => Val::Unknown,
            },
        })
    }

    fn call(&self, name: &str, args: &[Expr<'_>]) -> Result<Val, G::Error> {
        let path = || args.first().and_then(Expr::text);
        Ok(match (name, args) {
            ("fn:Eval" | "fn:IsRequired", [inner]) | ("fn:Deprecated", [_, inner]) => {
                self.eval(inner)?
            }
            ("fn:Not", [inner]) => match self.eval(inner)?.truth() {
                Some(value) => Val::Bool(!value),
                None => Val::Unknown,
            },
            ("fn:MustBeDirect" | "fn:MustBeIndirect", []) => Val::Bool(true),
            ("fn:MustBeDirect" | "fn:MustBeIndirect", [inner]) => self.eval(inner)?,
            ("fn:IsPresent", [_]) => self.present(path())?,
            ("fn:IsPresent", [_, then]) => match self.present(path())? {
                Val::Bool(false) => Val::Bool(true),
                Val::Bool(true) => self.eval(then)?,
                other => other,
            },
            ("fn:SinceVersion" | "fn:BeforeVersion" | "fn:IsPDFVersion", [at, rest @ ..]) => {
                let Some(gate) = at.text().and_then(version) else {
                    return Ok(Val::Unknown);
                };
                let open = match name {
                    "fn:SinceVersion" => self.version >= gate,
                    "fn:BeforeVersion" => self.version < gate,
                    _ => self.version == gate,
                };
                match rest {
                    [] => Val::Bool(open),
                    [_] if !open => Val::Bool(false),
                    [inner] => match self.eval(inner)? {
                        Val::Bool(value) => Val::Bool(value),
                        Val::Unknown => Val::Unknown,
                        _ => Val::Bool(true),
                    },
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
                let wanted = self.eval(wanted)?;
                self.path(path().unwrap_or(""), |value| {
                    let Some(value) = value else {
                        return Val::Absent;
                    };
                    if !matches!(self.graph.view(value), View::Array(_)) {
                        return Val::Unknown;
                    }
                    let found = self.graph.elements(value).iter().any(|item| {
                        match (scalar(&self.graph.view(item)), &wanted) {
                            (Val::Num(a), Val::Num(b)) => (a - b).abs() < 1e-9,
                            (a, b) => a == *b,
                        }
                    });
                    Val::Bool(found)
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
        let mut segments: Vec<&str> = path.split("::").collect();
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
        then: impl FnOnce(Option<&G::Value>) -> Val,
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
            return Ok(then(None));
        };
        if let View::Reference(number) = self.graph.view(child) {
            let resolved = self.graph.resolve(number)?;
            return match (&resolved, rest.is_empty()) {
                (Some(resolved), false) => self.walk(resolved, rest, then),
                (resolved, _) => Ok(then(resolved.as_deref())),
            };
        }
        if rest.is_empty() {
            Ok(then(Some(child)))
        } else {
            self.walk(child, rest, then)
        }
    }
}
