"""The registry of limit codes, limits.toml, generates the files that name the codes.

The registry is the one source of limit codes (DESIGN.md section 9).
tools/generate_limits.py writes the Rust enum, the Python mapping, and the
table of the security guide from it, and the first test fails when one of
them is stale.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args

import pytest

from pyropdf import _limit_codes

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Any

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "tools" / "generate_limits.py"
REGISTRY = Path("limits.toml")
GENERATED = (
    Path("crates/pyropdf-store/src/budget/code.rs"),
    Path("python/pyropdf/_limit_codes.py"),
    Path("docs/limit-codes.md"),
)
COMMAND = "uv run python tools/generate_limits.py"

# The codes of pylopdf at 884af0dbf21973cfd434a9eca4e769de62fcc25c that pyropdf
# has, which keep their names: opening and the structure of the document,
# decoding streams, the data handed to the interpreter, content operations,
# the low-level object model, saving, pixmaps, and passwords.
PYLOPDF_CODES = (
    "file_size",
    "page_count",
    "object_count",
    "object_depth",
    "decompressed_size",
    "page_content_size",
    "total_decompressed_size",
    "decompression_unverifiable",
    "stream_filter_count",
    "interpretation_size",
    "content_operation_count",
    "content_stream_size",
    "content_input_size",
    "content_element_count",
    "stream_data_size",
    "object_value_depth",
    "pdf_output_size",
    "pixmap_output_size",
    "password_input_size",
)

# One code that follows every rule of the registry.
VALID = '[codes.file_size]\nmessage = "the input is too large"\nunit = "bytes"\nreached = "The input is too large."\n'


def generate(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run the generator on a repository root, from that root."""
    command = [sys.executable, str(GENERATOR), "--root", str(root), *args]
    return subprocess.run(command, capture_output=True, check=False, cwd=root, text=True)  # noqa: S603 - fixed arguments


def copy_registry(root: Path) -> Path:
    """Copy the registry and the generated files to another root, and return it."""
    for path in (REGISTRY, *GENERATED):
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / path, root / path)
    return root


def test_the_generated_files_are_current() -> None:
    result = generate(ROOT, "--check")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("path", GENERATED, ids=Path.as_posix)
def test_the_check_fails_when_a_generated_file_is_stale(tmp_path: Path, path: Path) -> None:
    root = copy_registry(tmp_path)
    with (root / path).open("a", encoding="utf-8") as file:
        file.write("\n")
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr == f"{path.as_posix()} is stale; run `{COMMAND}`\n"


@pytest.mark.parametrize("path", GENERATED, ids=Path.as_posix)
def test_the_check_fails_when_a_generated_file_is_missing(tmp_path: Path, path: Path) -> None:
    root = copy_registry(tmp_path)
    (root / path).unlink()
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr == f"{path.as_posix()} is stale; run `{COMMAND}`\n"


def test_the_check_fails_when_the_registry_changes(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    registry = (root / REGISTRY).read_text(encoding="utf-8")
    (root / REGISTRY).write_text(registry.replace("default = 16", "default = 17"), encoding="utf-8")
    result = generate(root, "--check")
    assert result.returncode == 1
    assert result.stderr.splitlines() == [
        f"crates/pyropdf-store/src/budget/code.rs is stale; run `{COMMAND}`",
        f"docs/limit-codes.md is stale; run `{COMMAND}`",
    ]


def test_the_generator_writes_what_the_check_expects(tmp_path: Path) -> None:
    root = copy_registry(tmp_path)
    for path in GENERATED:
        (root / path).unlink()
    result = generate(root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f"wrote {path.as_posix()}" for path in GENERATED]
    for path in GENERATED:
        assert (root / path).read_text(encoding="utf-8") == (ROOT / path).read_text(encoding="utf-8")
    assert generate(root, "--check").returncode == 0
    # A second run finds nothing to write.
    assert generate(root).stdout == ""


@pytest.mark.parametrize(
    ("registry", "problem"),
    [
        ("", "limits.toml: the registry has no codes"),
        ("[codes", "limits.toml: "),
        (VALID + "[other]\n", "limits.toml: unknown top-level keys: other"),
        (VALID.replace("file_size", "FileSize"), "FileSize: a code is lowercase words joined by underscores"),
        (VALID.replace("unit =", "units ="), "file_size: unknown fields: units"),
        (VALID.replace('reached = "The input is too large."\n', ""), "file_size: missing fields: reached"),
        (VALID.replace("too large", "too large."), "file_size: message does not follow the rule"),
        (VALID.replace("The input", "the input"), "file_size: reached does not follow the rule"),
        (VALID.replace('"bytes"', '"Bytes"'), "file_size: unit does not follow the rule"),
        (VALID.replace("too large.", "too large | wide."), "file_size: reached holds a quote, a backslash, a bar"),
        (VALID + "default = 0\n", "file_size: default is a positive integer that fits in 64 bits: 0"),
        (VALID + "default = true\n", "file_size: default is a positive integer that fits in 64 bits: True"),
        (VALID.replace('unit = "bytes"\n', "default = 1\n"), "file_size: a code with a default has a unit"),
        (VALID.replace("file_size", "x_1") + VALID.replace("file_size", "x1"), "x1: its Rust variant X1"),
    ],
)
def test_the_generator_refuses_a_registry_that_breaks_its_rules(tmp_path: Path, registry: str, problem: str) -> None:
    (tmp_path / REGISTRY).write_text(registry, encoding="utf-8")
    result = generate(tmp_path, "--check")
    assert result.returncode == 1
    assert problem in result.stderr


def test_the_registry_keeps_the_pylopdf_codes_that_phase_0_needs() -> None:
    codes = get_args(_limit_codes.LimitCode)
    assert [code for code in PYLOPDF_CODES if code not in codes] == []


def test_the_python_mapping_follows_the_registry() -> None:
    registry = tomllib.loads((ROOT / REGISTRY).read_text(encoding="utf-8"))["codes"]
    assert tuple(_limit_codes.LIMIT_CODES) == tuple(registry)
    assert get_args(_limit_codes.LimitCode) == tuple(registry)
    for code, reached in _limit_codes.LIMIT_CODES.items():
        assert reached == registry[code]["reached"]


def test_the_python_mapping_is_read_only() -> None:
    mapping: Any = _limit_codes.LIMIT_CODES
    with pytest.raises(TypeError):
        mapping["file_size"] = "changed"


_NestedDef = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_UNRESOLVABLE = ast.Tuple(elts=[], ctx=ast.Load())


@dataclass(frozen=True)
class _Scope:
    kind: Literal["module", "function", "class"]
    name: str
    pos_params: tuple[str, ...]
    all_params: frozenset[str]
    defaults: Mapping[str, ast.expr]
    bindings: Mapping[str, tuple[ast.expr, ...]]


@dataclass(frozen=True)
class _CallSite:
    call: ast.Call
    scopes: tuple[_Scope, ...]


@dataclass(frozen=True)
class _ModuleCalls:
    limit_calls: tuple[_CallSite, ...]
    helper_calls: Mapping[str, tuple[_CallSite, ...]]
    function_counts: Counter[str]


def _function_signature(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[tuple[str, ...], frozenset[str], dict[str, ast.expr]]:
    pos = tuple(arg.arg for arg in (*node.args.posonlyargs, *node.args.args))
    kwonly = tuple(arg.arg for arg in node.args.kwonlyargs)
    defaults = dict(zip(pos[len(pos) - len(node.args.defaults) :], node.args.defaults, strict=True))
    defaults.update({name: expr for name, expr in zip(kwonly, node.args.kw_defaults, strict=True) if expr is not None})
    return pos, frozenset((*pos, *kwonly)), defaults


def _outer_header_nodes(node: _NestedDef) -> list[ast.AST]:
    if isinstance(node, ast.ClassDef):
        return [*node.decorator_list, *node.bases, *node.keywords]
    defaults = [expr for expr in node.args.kw_defaults if expr is not None]
    returns = [node.returns] if node.returns is not None else []
    return [*node.decorator_list, *node.args.defaults, *defaults, *returns]


def _own_nodes(body: Sequence[ast.stmt]) -> tuple[list[ast.AST], list[_NestedDef]]:
    own: list[ast.AST] = []
    children: list[_NestedDef] = []
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            children.append(current)
            stack.extend(reversed(_outer_header_nodes(current)))
            continue
        own.append(current)
        stack.extend(reversed(list(ast.iter_child_nodes(current))))
    return own, children


def _scope_bindings(nodes: Sequence[ast.AST]) -> dict[str, tuple[ast.expr, ...]]:
    raw: dict[str, list[ast.expr]] = {}
    direct_stores: set[int] = set()
    for node in nodes:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            direct_stores.add(id(node.targets[0]))
            raw.setdefault(node.targets[0].id, []).append(node.value)
        elif (
            isinstance(node, (ast.AnnAssign, ast.NamedExpr))
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            direct_stores.add(id(node.target))
            raw.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            raw.setdefault(node.name, []).append(_UNRESOLVABLE)
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and id(node) not in direct_stores:
            raw.setdefault(node.id, []).append(_UNRESOLVABLE)
    return {name: tuple(values) for name, values in raw.items()}


def _is_limit_error(func: ast.expr) -> bool:
    return (isinstance(func, ast.Name) and func.id == "LimitError") or (
        isinstance(func, ast.Attribute) and func.attr == "LimitError"
    )


def _collect_module(tree: ast.Module) -> _ModuleCalls:
    limit_calls: list[_CallSite] = []
    helper_calls: dict[str, list[_CallSite]] = {}
    function_counts: Counter[str] = Counter()
    pending: list[tuple[ast.Module | _NestedDef, tuple[_Scope, ...]]] = [(tree, ())]
    while pending:
        node, parents = pending.pop()
        own, children = _own_nodes(node.body)
        bindings = _scope_bindings(own)
        if isinstance(node, ast.Module):
            scope = _Scope("module", "<module>", (), frozenset(), {}, bindings)
        elif isinstance(node, ast.ClassDef):
            scope = _Scope("class", node.name, (), frozenset(), {}, bindings)
        else:
            function_counts[node.name] += 1
            pos_params, all_params, defaults = _function_signature(node)
            scope = _Scope("function", node.name, pos_params, all_params, defaults, bindings)
        chain = (*parents, scope)
        for item in own:
            if not isinstance(item, ast.Call):
                continue
            site = _CallSite(item, chain)
            if _is_limit_error(item.func):
                limit_calls.append(site)
            elif isinstance(item.func, ast.Name):
                helper_calls.setdefault(item.func.id, []).append(site)
        pending.extend((child, chain) for child in reversed(children))
    return _ModuleCalls(
        tuple(limit_calls),
        {name: tuple(sites) for name, sites in helper_calls.items()},
        function_counts,
    )


def _call_argument(call: ast.Call, scope: _Scope, param: str) -> ast.expr | None:
    if any(kw.arg is None for kw in call.keywords):
        return None
    kw_matches = [kw.value for kw in call.keywords if kw.arg == param]
    if param in scope.pos_params:
        index = scope.pos_params.index(param)
        if any(isinstance(arg, ast.Starred) for arg in call.args[: index + 1]):
            return None
        if index < len(call.args):
            return None if kw_matches else call.args[index]
    if len(kw_matches) == 1:
        return kw_matches[0]
    return scope.defaults.get(param)


def _limit_error_code_arg(call: ast.Call) -> ast.expr | None:
    if any(kw.arg is None for kw in call.keywords):
        return None
    if call.args:
        if isinstance(call.args[0], ast.Starred) or any(kw.arg == "code" for kw in call.keywords):
            return None
        return call.args[0]
    kw_matches = [kw.value for kw in call.keywords if kw.arg == "code"]
    return kw_matches[0] if len(kw_matches) == 1 else None


def _resolve_name(
    expr: ast.Name,
    scopes: tuple[_Scope, ...],
    seen: frozenset[tuple[int, str]],
    module: _ModuleCalls,
    label: str,
) -> tuple[list[tuple[ast.expr, tuple[_Scope, ...], frozenset[tuple[int, str]]]], str | None]:
    for depth in range(len(scopes) - 1, -1, -1):
        scope = scopes[depth]
        if scope.kind == "class" and depth != len(scopes) - 1:
            continue
        key = (id(scope), expr.id)
        if key in seen:
            return [], f"{label}:{expr.lineno}: cyclic LimitError code binding {expr.id!r}"
        if expr.id in scope.bindings:
            next_scopes = scopes[: depth + 1]
            return [(rhs, next_scopes, seen | {key}) for rhs in scope.bindings[expr.id]], None
        if scope.kind == "function" and expr.id in scope.all_params:
            sites = module.helper_calls.get(scope.name, ())
            if module.function_counts[scope.name] != 1 or not sites:
                msg = f"{label}:{expr.lineno}: helper {scope.name!r} parameter {expr.id!r} has no traceable callers"
                return [], msg
            items: list[tuple[ast.expr, tuple[_Scope, ...], frozenset[tuple[int, str]]]] = []
            for site in sites:
                arg = _call_argument(site.call, scope, expr.id)
                if arg is None:
                    return [], f"{label}:{site.call.lineno}: call to {scope.name!r} does not pass traceable {expr.id!r}"
                items.append((arg, site.scopes, seen | {key}))
            return items, None
    return [], f"{label}:{expr.lineno}: untraceable LimitError code {expr.id!r}"


def _extract_limit_error_codes(source: str, label: str = "<module>") -> tuple[set[str], list[str]]:
    """Return all resolved ``LimitError`` code strings and any resolution errors in ``source``."""
    module = _collect_module(ast.parse(source, filename=label))
    resolved: set[str] = set()
    errors: list[str] = []
    for site in module.limit_calls:
        code_arg = _limit_error_code_arg(site.call)
        if code_arg is None:
            errors.append(f"{label}:{site.call.lineno}: LimitError call missing traceable code argument")
            continue
        work: list[tuple[ast.expr, tuple[_Scope, ...], frozenset[tuple[int, str]]]] = [
            (code_arg, site.scopes, frozenset())
        ]
        while work:
            expr, scopes, seen = work.pop()
            if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                resolved.add(expr.value)
            elif isinstance(expr, ast.Name):
                next_items, error = _resolve_name(expr, scopes, seen, module, label)
                if error is not None:
                    errors.append(error)
                else:
                    work.extend(next_items)
            else:
                lineno = getattr(expr, "lineno", site.call.lineno)
                errors.append(f"{label}:{lineno}: non-literal LimitError code {ast.unparse(expr)!r}")
    return resolved, errors


def test_every_python_limit_error_site_uses_a_registered_code() -> None:
    registered = frozenset(get_args(_limit_codes.LimitCode))
    package = ROOT / "python" / "pyropdf"
    all_codes: set[str] = set()
    problems: list[str] = []
    for path in sorted(package.rglob("*.py")):
        label = path.relative_to(ROOT).as_posix()
        codes, errors = _extract_limit_error_codes(path.read_text(encoding="utf-8"), label)
        problems.extend(errors)
        problems.extend(f"{label}: unregistered LimitError code {code!r}" for code in sorted(codes - registered))
        all_codes.update(codes)
    assert problems == []
    assert {
        "font_subset_appearances",
        "optional_content_count",
        "optional_content_expression",
        "page_fonts_entries",
        "page_fonts_forms",
        "page_fonts_tree",
        "page_label_input_size",
        "page_label_ranges",
        "page_label_text_size",
        "page_label_tree_depth",
        "page_label_tree_edges",
        "sanitize_action_chain",
        "sanitize_forms",
    }.issubset(all_codes)


def test_the_python_limit_error_check_resolves_constants_and_helpers() -> None:
    source = """
_MODULE_CODE = "file_size"

def _make_error(code: str, msg: str) -> LimitError:
    return LimitError(code, msg)

def check(flag: bool) -> None:
    if flag:
        local: str = "page_count"
        raise LimitError(local, "too many")
    if not flag:
        raise LimitError(_MODULE_CODE, "too big")
    raise _make_error("object_count", "objects")
"""
    codes, errors = _extract_limit_error_codes(source)
    assert errors == []
    assert codes == {"file_size", "page_count", "object_count"}


@pytest.mark.parametrize(
    ("source", "expected_code", "expected_error"),
    [
        ('raise LimitError("not_in_registry", "bad")', "not_in_registry", None),
        ('_BAD = "not_in_registry"\nraise LimitError(_BAD, "bad")', "not_in_registry", None),
        (
            'def _helper(code: str) -> None:\n    raise LimitError(code, "bad")\n_helper("not_in_registry")',
            "not_in_registry",
            None,
        ),
        ('raise LimitError(f"page_{1}", "dynamic")', None, "non-literal LimitError code"),
        ('raise LimitError(get_code(), "dynamic")', None, "non-literal LimitError code"),
        ('raise LimitError(unknown_var, "dynamic")', None, "untraceable LimitError code 'unknown_var'"),
        ("def _unused(code: str) -> None:\n    raise LimitError(code, 'x')", None, "has no traceable callers"),
        ("raise LimitError()", None, "missing traceable code argument"),
    ],
)
def test_the_python_limit_error_check_catches_unknown_or_dynamic_codes(
    source: str,
    expected_code: str | None,
    expected_error: str | None,
) -> None:
    registered = frozenset(get_args(_limit_codes.LimitCode))
    codes, errors = _extract_limit_error_codes(source)
    if expected_code is not None:
        assert errors == []
        assert codes - registered == {expected_code}
    if expected_error is not None:
        assert any(expected_error in error for error in errors), errors
