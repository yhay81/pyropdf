//! Interpret one entry of a standalone descriptor using the same predicates
//! as the structural check. Only descriptor keys, never an input-sized set
//! of dictionary entries or array elements, are consulted to choose a link.

use super::expr::{Expr, Op, Scope, parse};
use super::{Admit, Graph, View, Walk, admits, matches, possible};
use crate::{Condition, Key, Kind, Link, Object, Typed};

/// Meaning of an entry in its holder. A default is a literal of the model.
#[derive(Debug)]
pub enum Interpretation {
    Absent,
    Default(Kind, &'static str),
    Value(Kind, Option<&'static str>),
    Refused(&'static str),
}

/// Interpret the resolved value of a declared key in one version.
///
/// # Errors
///
/// Returns budget and resolution errors from the graph.
pub fn interpret<G: Graph>(
    graph: &G,
    holder: &G::Value,
    key: &'static Key,
    value: Option<&G::Value>,
) -> Result<Interpretation, G::Error> {
    let mut walk = Walk::new(graph);
    let scope = walk.scope(holder, value);
    let introduced = walk.introduced(holder, key)?;
    if introduced == Some(false) {
        return Ok(if value.is_some() {
            Interpretation::Refused("the key is newer than the document")
        } else {
            Interpretation::Absent
        });
    }
    let Some(value) = value else {
        for ty in key.types {
            if let Some(default) = ty.default
                && let Some(literal) = default_value(&scope, default)?
            {
                return Ok(Interpretation::Default(ty.kind, literal));
            }
        }
        let required = match key.required {
            Condition::When(test) => walk.decide(&scope, test)? == Some(true),
            condition => condition == Condition::Always,
        };
        return Ok(
            if required && !key.deprecated.is_some_and(|v| graph.version() >= v) {
                Interpretation::Refused("the required value is absent")
            } else {
                Interpretation::Absent
            },
        );
    };
    let view = graph.view(value);
    for ty in key.types {
        let compatible = matches(ty.kind, &view)
            || !matches!(ty.kind, Kind::NameTree | Kind::NumberTree)
                && ty
                    .links
                    .iter()
                    .any(|link| matches(link.target().container(), &view));
        if !compatible || !admitted(&mut walk, &scope, ty, &view)? {
            continue;
        }
        if ty.links.is_empty() || matches!(ty.kind, Kind::NameTree | Kind::NumberTree) {
            return Ok(Interpretation::Value(ty.kind, None));
        }
        let Some(name) = linked(&mut walk, &scope, ty.links, value)? else {
            return Ok(Interpretation::Refused(
                "no unique linked object type matches",
            ));
        };
        return Ok(Interpretation::Value(ty.kind, Some(name)));
    }
    Ok(Interpretation::Refused(
        "the value does not match its admitted types or values",
    ))
}

fn admitted<G: Graph>(
    walk: &mut Walk<'_, G>,
    scope: &Scope<'_, '_, G>,
    ty: &Typed,
    view: &View<'_>,
) -> Result<bool, G::Error> {
    if let Some(gate) = ty.condition
        && walk.decide(scope, gate)? != Some(true)
    {
        return Ok(false);
    }
    if ty.values.is_empty() {
        return Ok(true);
    }
    // Semantic constraints that need rendering or external data remain
    // unchecked by the structural checker. They do not change the PDF type.
    Ok(matches!(
        possible(scope, ty.values, view)?,
        Admit::Yes | Admit::Unknown
    ))
}

fn default_value<G: Graph>(
    scope: &Scope<'_, '_, G>,
    default: &'static str,
) -> Result<Option<&'static str>, G::Error> {
    if !default.starts_with("fn:") {
        return Ok(Some(default));
    }
    match parse(default) {
        Some(expr) => default_expr(scope, &expr),
        None => Ok(None),
    }
}

fn default_expr<G: Graph>(
    scope: &Scope<'_, '_, G>,
    expr: &Expr<'static>,
) -> Result<Option<&'static str>, G::Error> {
    match expr {
        Expr::Call("fn:Eval", args) if args.len() == 1 => default_expr(scope, &args[0]),
        Expr::Binary(Op::Or, left, right) => match default_expr(scope, left)? {
            Some(literal) => Ok(Some(literal)),
            None => default_expr(scope, right),
        },
        Expr::Call("fn:DefaultValue", args) if args.len() == 2 => {
            Ok((scope.eval(&args[0])?.truth() == Some(true))
                .then(|| args[1].text())
                .flatten())
        }
        Expr::Call(_, args) if args.len() == 2 => Ok((scope.eval(expr)?.truth() == Some(true))
            .then(|| args[1].text())
            .flatten()),
        _ => Ok(None),
    }
}

/// Choose the declared type of a resolved leaf value in a name or number tree.
///
/// # Errors
///
/// Returns a resolution or budget error from the graph.
pub fn tree_target<G: Graph>(
    graph: &G,
    declared: &Typed,
    holder: &G::Value,
    value: &G::Value,
) -> Result<Option<&'static str>, G::Error> {
    let mut walk = Walk::new(graph);
    let scope = walk.scope(holder, Some(value));
    linked(&mut walk, &scope, declared.links, value)
}

fn linked<G: Graph>(
    walk: &mut Walk<'_, G>,
    scope: &Scope<'_, '_, G>,
    links: &[Link],
    value: &G::Value,
) -> Result<Option<&'static str>, G::Error> {
    if let Some(at) = walk.button_field(links, value)? {
        return Ok(Some(crate::OBJECTS[usize::from(at)].name));
    }
    let mut best = None;
    let mut score = None;
    let mut tied = false;
    for link in links {
        if let Some(gate) = link.condition
            && walk.decide(scope, gate)? != Some(true)
        {
            continue;
        }
        let target = link.target();
        let container = match walk.graph.view(value) {
            View::Array(_) => Kind::Array,
            View::Dictionary => Kind::Dictionary,
            View::Stream => Kind::Stream,
            _ => continue,
        };
        if target.container() != container {
            continue;
        }
        let rank = match walk.graph.view(value) {
            View::Array(len) => array_score(walk.graph, target, value, len)?,
            View::Dictionary | View::Stream => match walk.score(target, value)? {
                Some(matched) => Some((matched, usize::MAX - walk.missing(target, value)?)),
                None => None,
            },
            _ => None,
        };
        let Some(rank) = rank else { continue };
        if score == Some(rank) {
            tied = true;
        } else if score.is_none_or(|old| rank > old) {
            score = Some(rank);
            best = Some(target.name);
            tied = false;
        }
    }
    Ok(if tied { None } else { best })
}

/// Fixed array positions discriminate, for example, calibrated color spaces
/// at 0 and explicit destinations at 1. A repeating tail is never scanned.
fn array_score<G: Graph>(
    graph: &G,
    target: &Object,
    array: &G::Value,
    len: usize,
) -> Result<Option<(usize, usize)>, G::Error> {
    let mut score = 0;
    let mut fixed = 0;
    let mut repeat = false;
    let scope = Scope {
        graph,
        version: graph.version(),
        dict: Some(array),
        subject: None,
    };
    for key in target.keys {
        let Ok(at) = key.name.parse::<usize>() else {
            if key.name.ends_with('*') {
                repeat = true;
                continue;
            }
            return Ok(None);
        };
        fixed = fixed.max(at + 1);
        let Some(element) = graph.elements(array).get(at) else {
            if key.required == Condition::Always {
                return Ok(None);
            }
            continue;
        };
        let resolved;
        let element = match graph.view(element) {
            View::Reference(_) => {
                resolved = graph.target(element)?;
                let Some(value) = resolved.as_deref() else {
                    return Ok(None);
                };
                value
            }
            _ => element,
        };
        let view = graph.view(element);
        let mut matched = None;
        for ty in key.types.iter().filter(|ty| matches(ty.kind, &view)) {
            if ty.values.is_empty() {
                matched = Some(0);
                break;
            }
            for literal in ty.values {
                if admits(&scope, literal, &view)? == Admit::Yes {
                    matched = Some(1);
                    break;
                }
            }
            if matched.is_some() {
                break;
            }
        }
        let Some(weight) = matched else {
            return Ok(None);
        };
        score += weight;
    }
    Ok((repeat || len <= fixed).then_some((score, fixed)))
}
