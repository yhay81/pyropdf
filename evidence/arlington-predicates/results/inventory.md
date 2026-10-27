# Arlington predicates at d148fee5a0cb6dcacadf58c09ed842e32847024d

45 functions, 4729 calls, 2193 predicate cells in 1759 keys.

| Function | Calls | Keys | Class | Columns (cells) |
| --- | ---: | ---: | --- | --- |
| `Eval` | 1155 | 1084 | expressions | SpecialCase 470, SinceVersion 392, PossibleValues 265, DefaultValue 6 |
| `SinceVersion` | 1075 | 402 | versions | Link 209, PossibleValues 101, Required 64, SpecialCase 44, Type 26, IndirectReference 4 |
| `Extension` | 737 | 692 | versions | SinceVersion 647, PossibleValues 22, Link 19, SpecialCase 18, DefaultValue 5, Type 1, Required 1 |
| `ArrayLength` | 353 | 197 | values | SpecialCase 192, PossibleValues 5, Required 2 |
| `IsPresent` | 305 | 205 | expressions | Required 89, SpecialCase 89, IndirectReference 41 |
| `IsRequired` | 192 | 192 | expressions | Required 192 |
| `IsPDFVersion` | 169 | 83 | versions | Link 43, SpecialCase 18, Required 12, PossibleValues 10 |
| `BitsClear` | 154 | 50 | values | SpecialCase 50 |
| `MustBeDirect` | 103 | 103 | storage | IndirectReference 103 |
| `Not` | 76 | 63 | expressions | SpecialCase 40, Required 24 |
| `Deprecated` | 64 | 43 | versions | PossibleValues 43, Type 1 |
| `Ignore` | 57 | 55 | advisory | SpecialCase 55 |
| `IsMeaningful` | 57 | 56 | advisory | SpecialCase 56 |
| `BeforeVersion` | 34 | 31 | versions | SpecialCase 9, IndirectReference 9, Required 7, DefaultValue 4, PossibleValues 2 |
| `RectWidth` | 28 | 28 | values | Required 24, SpecialCase 4 |
| `RectHeight` | 28 | 28 | values | Required 24, SpecialCase 4 |
| `StringLength` | 13 | 11 | values | SpecialCase 10, PossibleValues 1 |
| `AlwaysUnencrypted` | 13 | 13 | storage | SpecialCase 13 |
| `InNameTree` | 12 | 10 | document | SpecialCase 7, PossibleValues 2, Required 2 |
| `BitClear` | 12 | 10 | values | SpecialCase 10 |
| `RequiredValue` | 12 | 5 | expressions | PossibleValues 5 |
| `NotStandard14Font` | 12 | 12 | document | Required 12 |
| `MustBeIndirect` | 10 | 10 | storage | IndirectReference 10 |
| `Contains` | 9 | 5 | expressions | Required 3, SpecialCase 2, PossibleValues 1 |
| `BitSet` | 5 | 4 | values | SpecialCase 4 |
| `InKeyMap` | 5 | 5 | document | SpecialCase 5 |
| `FontHasLatinChars` | 4 | 4 | content | Required 4 |
| `IsEncryptedWrapper` | 3 | 2 | storage | Required 2, SpecialCase 1 |
| `BitsSet` | 3 | 1 | values | SpecialCase 1 |
| `FileSize` | 3 | 3 | document | PossibleValues 3 |
| `IsLastInNumberFormatArray` | 3 | 3 | values | SpecialCase 3 |
| `ImplementationDependent` | 3 | 3 | advisory | DefaultValue 3 |
| `IsFieldName` | 2 | 2 | document | SpecialCase 2 |
| `DefaultValue` | 2 | 1 | expressions | DefaultValue 1 |
| `IsHexString` | 2 | 2 | lexical | SpecialCase 2 |
| `ArraySortAscending` | 2 | 2 | values | SpecialCase 2 |
| `KeyNameIsColorant` | 2 | 1 | document | SpecialCase 1 |
| `PageProperty` | 2 | 1 | document | PossibleValues 1 |
| `ImageIsStructContentItem` | 2 | 2 | content | Required 2 |
| `HasSpotColorants` | 1 | 1 | document | Required 1 |
| `HasProcessColorants` | 1 | 1 | document | Required 1 |
| `IsPDFTagged` | 1 | 1 | document | Required 1 |
| `NoCycle` | 1 | 1 | document | SpecialCase 1 |
| `PageContainsStructContentItems` | 1 | 1 | content | Required 1 |
| `NumberOfPages` | 1 | 1 | document | PossibleValues 1 |

## Classes

- **expressions**: The expression language: comparisons and arithmetic over the object's own keys and its parents (`@Key`, `parent::`), and the functions that only wrap an expression.
- **versions**: The PDF version of the file and the extensions that `/Extensions` declares.
- **storage**: Whether a value is stored directly or as an indirect object, and whether encryption applies to it.
- **values**: Arithmetic on one value: lengths, bits, rectangle sizes, sort order.
- **document**: Lookups across the document: name trees, destinations, the page count, the file size, field names, cycles, tagging, fonts and colorants named in dictionaries.
- **lexical**: The lexical form of the source: whether a string was written in hex. hayro-syntax 0.8 decodes hex strings without keeping the form.
- **content**: Content streams or font programs: marked content and the glyphs a font holds.
- **advisory**: Advisory functions that report nothing: what a reader ignores, what is meaningful, what an implementation chooses.

## Cells decided by each cumulative set

| Set | Cells decided | Share |
| --- | ---: | ---: |
| expressions | 647 | 29.5% |
| expressions + versions | 1740 | 79.3% |
| expressions + versions + storage | 1862 | 84.9% |
| expressions + versions + storage + values | 2149 | 98.0% |
| expressions + versions + storage + values + document | 2184 | 99.6% |
| expressions + versions + storage + values + document + lexical | 2186 | 99.7% |
| expressions + versions + storage + values + document + lexical + content | 2193 | 100.0% |

## Cells left by the first four classes, by function

| Function | Cells |
| --- | ---: |
| `NotStandard14Font` | 12 |
| `InNameTree` | 11 |
| `InKeyMap` | 5 |
| `FontHasLatinChars` | 4 |
| `FileSize` | 3 |
| `IsFieldName` | 2 |
| `IsHexString` | 2 |
| `ImageIsStructContentItem` | 2 |
| `HasSpotColorants` | 1 |
| `HasProcessColorants` | 1 |
| `IsPDFTagged` | 1 |
| `KeyNameIsColorant` | 1 |
| `NoCycle` | 1 |
| `PageContainsStructContentItems` | 1 |
| `NumberOfPages` | 1 |
| `PageProperty` | 1 |
