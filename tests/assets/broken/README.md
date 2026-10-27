# Broken on purpose

`generate.py` writes these files with no library. Each is one valid page with
one defect that the structural check (`Document.check`) must report, one file
per kind of defect:

| File | Defect | Clause |
| --- | --- | --- |
| `type.pdf` | the page tree's `/Count` is a string | `PageTreeNodeRoot/Count` |
| `required.pdf` | the page has no `/MediaBox`, and no ancestor gives one | `PageObject/MediaBox` |
| `version.pdf` | a PDF 1.3 file has the 1.4 key `/Lang` | `Catalog/Lang` |
| `indirect.pdf` | the catalog's `/Pages` is direct | `Catalog/Pages` |
| `value.pdf` | `/PageLayout /Sideways` | `Catalog/PageLayout` |
| `trailer.pdf` | the trailer's `/Size` is 0 | `FileTrailer/Size` |
| `count.pdf` | the page tree's `/Count` is 2 for one page | `PageTreeNode/Count` |
| `parent.pdf` | the page's `/Parent` is the catalog | `PageTreeNode/Kids` |
| `order.pdf` | a name tree's keys run `(b)`, `(a)` | `NameTree/Names` |
| `limits.pdf` | a number tree's `/Limits` miss its last key | `NumberTree/Limits` |

`cycle.pdf` is a page tree whose root lists itself among its kids, which
the walk visits once. `chain(depth)` builds a page tree that deep for the
tests, without committing it.
