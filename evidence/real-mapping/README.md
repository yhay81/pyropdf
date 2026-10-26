# How Python sees reals that keep their lexemes

Evidence for the decision #11. [probe.py](probe.py) prototypes the three
options and asks each the questions in the issue's scope;
[results.txt](results.txt) is its output, from:

```bash
uv run --with numpy python evidence/real-mapping/probe.py
```

The source real is `35.68123456789012345`, which has more digits than an
`f64` keeps, as yhay81/pylopdf#370 asks a preservation test to use.

## Findings

| Question | A: `float` subclass | B: plain `float` | C: value type |
| --- | --- | --- | --- |
| `isinstance(x, float)` | yes | yes | no |
| Equality and hash agree with `float` | yes | yes | yes, by hand |
| Arithmetic | yes, returns a plain `float` | yes | no, unless every operator is written |
| `json.dumps` | yes, as the `f64` | yes, as the `f64` | `TypeError` |
| `pickle` keeps the lexeme | yes, with `__reduce__` | no | yes |
| NumPy | `float64` | `float64` | `object` array |
| Exact `Decimal` view | yes | no: the `f64`'s expansion | yes |
| Assigned back to the same slot | lexeme | lexeme, when the store compares the `f64` with the kept real | lexeme |
| Copied to another slot or document | lexeme | `f64`'s shortest form: digits lost | lexeme |

Assigning a `float` writes its shortest round-trip form (`0.1`), and
assigning a `Decimal` writes its exact digits in positional form (`1E-7`
becomes `0.0000001`), in every option.

## Recommendation

Option A: `pyropdf.Real`, an immutable `float` subclass with `lexeme`
(`bytes | None`) and `to_decimal()`.

- It is a `float`, so pylopdf code, `json`, and NumPy need no change, and
  the codemod adds no rule. pylopdf's `isinstance(x, (int, float))` checks
  (`_geometry.py`, `_form.py`, `_widgets.py`, and others) accept it; C
  fails every one of them. The one `type(x) is float` test, a fast path in
  `_geometry.py`, sends a `Real` to the general path, which still accepts
  it.
- It keeps the lexeme through copies, between documents, and through
  `pickle`, which B cannot. B loses the source digits as soon as a value
  moves, and its `Decimal` view is not the source decimal.
- Arithmetic returns a plain `float`, so a computed value never carries a
  stale lexeme: the change is visible in the type.
- Equality and hashing are the `float`'s, as the store's equality compares
  `f64` values. `Real(b"1.0") == Real(b"1.00")` is true, and code that must
  tell the two apart compares `lexeme`.
- C is the most explicit, but it breaks `json`, NumPy, and arithmetic, so
  every pylopdf caller would need `float(x)`.

The rules for assignment, for #32:

- a `Real` writes its lexeme when it has one, and otherwise its `f64` in
  shortest form;
- a `float` writes its shortest form, and an `int` writes an integer;
- a `Decimal` writes its exact digits in positional form, and a
  non-finite `Decimal` or `float` raises, as pylopdf refuses `nan`.

Until I7 (#16) reports the span of a lone real, a `Real` read from bytes
has no lexeme unless the store kept one, so it writes its `f64`; #32's
acceptance already says so. The decision changes nothing in the store.
