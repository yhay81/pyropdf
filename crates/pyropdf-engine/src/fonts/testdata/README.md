# Shaping test fonts

Subsets made with fontTools' `pyftsubset --layout-features='*'` for the strings of `fonts/shape/tests.rs`, all under the SIL Open Font License 1.1 ([OFL.txt](OFL.txt)):

- `NotoSansJP-subset.otf`: Noto Sans JP Regular (CFF), for Latin and Japanese.
- `NotoSansHebrew-subset.ttf`: Noto Sans Hebrew Regular.
- `NotoSansArabic-Variable-subset.ttf`: Noto Sans Arabic, a variable font shaped at its default instance.

`variable_faces.py` writes the `variable-*` faces, for what an instance of a variable face does, and `NotoSansArabic-Variable-subset-instances.txt`, what fontTools' instancer makes of the Arabic face, which the instance tests compare with. The faces are made here and fall under pyropdf's MIT license ([LICENSE](../../../../../LICENSE)).

`colour_faces.py` writes the `colour-*` faces, one for each table that can hold a colour glyph, with a square as their only drawing. They are made here and fall under pyropdf's MIT license ([LICENSE](../../../../../LICENSE)).
