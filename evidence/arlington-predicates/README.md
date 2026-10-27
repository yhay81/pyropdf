# Arlington predicates

The inventory behind the recommendation in #216: which predicates of the
Arlington PDF Model the structural check of
[DESIGN.md](../../DESIGN.md) section 8 evaluates first.

```bash
python3 evidence/arlington-predicates/inventory.py > evidence/arlington-predicates/results/inventory.md
```

The script reads the model that `tools/arlington/model` vendors (#215) and
needs only the standard library. It fails on a function that its class
table does not list, so a new pin of the model updates the table.

## Results at d148fee5a0cb6dcacadf58c09ed842e32847024d

[results/inventory.md](results/inventory.md) has the full table.

- 45 functions make 4,729 calls in 2,193 predicate cells, a cell being one
  column of one key, in 1,759 of the model's 3,985 keys.
- `Eval` (1,155 calls), `SinceVersion` (1,075), and `Extension` (737) make
  63% of the calls.
- `Eval` wraps an expression over the object's keys (`@Key`), its parents
  (`parent::`), and paths from the trailer, so every class needs the
  expression language first.

Share of predicate cells decided by each cumulative set of classes. The
three advisory functions, `Ignore`, `IsMeaningful`, and
`ImplementationDependent`, report nothing and count as decided.

| Set | Cells | Share |
| --- | ---: | ---: |
| expressions | 647 | 29.5% |
| + versions | 1,740 | 79.3% |
| + storage | 1,862 | 84.9% |
| + values | 2,149 | 98.0% |
| + document | 2,184 | 99.6% |
| + lexical | 2,186 | 99.7% |
| + content | 2,193 | 100.0% |

## Recommendation

Implement **expressions, versions, storage, and values** first. They decide
98.0% of the predicate cells, and none of them reads beyond the object being
checked, the objects that its expression's paths name, the file's version,
and `/Extensions`:

- **expressions**: `Eval`, `Not`, `IsRequired`, `IsPresent`,
  `DefaultValue`, `RequiredValue`, and `Contains`. The expression language
  itself: comparisons, `&&`, `||`, arithmetic, and `@Key`, `parent::`, and
  `trailer::` paths. Each path walk is bounded by the operation's budget
  (F6).
- **versions**: `SinceVersion`, `BeforeVersion`, `IsPDFVersion`,
  `Deprecated`, and `Extension`. They are decided against the file's
  version, the later of the header and the catalog's `/Version`, and against
  the catalog's `/Extensions`.
- **storage**: `MustBeDirect`, `MustBeIndirect`, `AlwaysUnencrypted`, and
  `IsEncryptedWrapper`. The store already knows whether a value is direct
  and what the security handler encrypts.
- **values**: `ArrayLength`, `BitsClear`, `BitClear`, `BitSet`, `BitsSet`,
  `RectWidth`, `RectHeight`, `StringLength`, `ArraySortAscending`, and
  `IsLastInNumberFormatArray`. Arithmetic on one value.

Leave for later, with each cell counted as unchecked in the report, so the
report says how much it did not decide:

- **document**, 35 cells: `NotStandard14Font` 12, `InNameTree` 11,
  `InKeyMap` 5, `FileSize` 3, `IsFieldName` 2, and one each of
  `NumberOfPages`, `PageProperty`, `NoCycle`, `IsPDFTagged`,
  `KeyNameIsColorant`, `HasSpotColorants`, and `HasProcessColorants`. Each
  needs a lookup across the document, such as a name tree, the page tree,
  the form's field names, or a bounded cycle search. They fit a second
  step once the reverse index (#173) exists.
- **lexical**, 2 cells: `IsHexString` needs the written form of a string,
  which hayro-syntax 0.8 does not keep (`object/string.rs` decodes hex
  strings into bytes).
- **content**, 7 cells: `FontHasLatinChars`, `ImageIsStructContentItem`,
  and `PageContainsStructContentItems` read content streams or font
  programs, which is engine work and not the structural check's.

The owner's choice is recorded in #216; nothing builds on this
recommendation before it.
