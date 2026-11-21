"""Occurrence facts from the corrected ISO 32000-2:2020 Annex L matrix (July 2024).

The columns are parents. Only the permitted and conditional children are listed.
See https://pdf-issues.pdfa.org/32000-2-2020/clauseAnnexL.html.
"""

from __future__ import annotations

# Parents with identical columns share a rule set. Content represents an MCR or OBJR.
_GROUPS = (
    (
        "StructTreeRoot",
        {
            "exactly one": "Document",
        },
    ),
    (
        "Document DocumentFragment",
        {
            "many": (
                "Annot Artifact Aside Div Document DocumentFragment FENote Figure Form Formula Hn L Link "
                "NonStruct P Part Sect Table Title"
            ),
            "optional": "H",
        },
    ),
    (
        "Part",
        {
            "role-specific": (
                "Annot Artifact Aside Caption Div Document DocumentFragment FENote Figure Form Formula H Hn L "
                "Lbl Link NonStruct P Part Sect Sub Table Title"
            ),
        },
    ),
    (
        "Div",
        {
            "role-specific": (
                "Annot Artifact Aside Caption Div Document DocumentFragment Em FENote Figure Form Formula H Hn L "
                "LBody LI Lbl Link NonStruct P Part RB RP RT Ruby Sect Span Strong Sub TBody TD TFoot TH THead "
                "TR Table Title WP WT Warichu"
            ),
        },
    ),
    (
        "Sect",
        {
            "many": (
                "Annot Artifact Aside Caption Div DocumentFragment FENote Figure Form Formula Hn L Lbl Link "
                "NonStruct P Part Sect Table Title"
            ),
            "optional": "H",
        },
    ),
    (
        "Aside",
        {
            "many": (
                "Annot Artifact Content Div Document DocumentFragment FENote Figure Form Formula Hn L Lbl Link "
                "NonStruct P Part Sect Table"
            ),
            "optional": "Caption H",
        },
    ),
    (
        "NonStruct",
        {
            "role-specific": (
                "Annot Artifact Aside Caption Content Div Document DocumentFragment Em FENote Figure Form "
                "Formula H Hn L LBody LI Lbl Link NonStruct P Part RB RP RT Ruby Sect Span Strong Sub TBody TD "
                "TFoot TH THead TR Table Title WP WT Warichu"
            ),
        },
    ),
    (
        "Title",
        {
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula L Lbl Link NonStruct P Part Ruby "
                "Span Strong Table Warichu"
            ),
            "optional": "Caption",
        },
    ),
    (
        "Sub",
        {
            "many": (
                "Annot Artifact Content Em FENote Figure Form Formula L Lbl Link NonStruct Ruby Span Strong Warichu"
            ),
        },
    ),
    (
        "P",
        {
            "many": (
                "Annot Artifact Content Em FENote Figure Form Formula L Lbl Link NonStruct Ruby Span Strong Sub "
                "Table Warichu"
            ),
        },
    ),
    (
        "Hn H",
        {
            "many": (
                "Annot Artifact Content Em FENote Figure Form Formula Lbl Link NonStruct Ruby Span Strong Sub Warichu"
            ),
            "optional": "Sect",
        },
    ),
    (
        "Lbl",
        {
            "many": (
                "Annot Artifact Content Em FENote Figure Form Formula Link NonStruct Ruby Span Strong Sub Warichu"
            ),
        },
    ),
    (
        "Em Strong Span",
        {
            "many": (
                "Annot Artifact Content Em FENote Figure Form Formula Lbl Link NonStruct Ruby Span Strong Sub Warichu"
            ),
        },
    ),
    (
        "Link",
        {
            "grouping only": "Aside Caption DocumentFragment Form H Hn L P Part Table Title",
            "many": (
                "Annot Artifact Content Div Em FENote Figure Formula Lbl NonStruct Ruby Sect Span Strong Sub Warichu"
            ),
        },
    ),
    (
        "Annot",
        {
            "grouping only": "Aside Caption DocumentFragment Form H Hn L P Part Table Title",
            "many": (
                "Annot Artifact Content Div Em FENote Figure Formula Lbl Link NonStruct Ruby Sect Span Strong "
                "Sub Warichu"
            ),
        },
    ),
    (
        "Form",
        {
            "grouping only": "Figure Formula L Part Table",
            "many": "Artifact Content Div FENote Lbl NonStruct",
            "optional": "Caption",
        },
    ),
    (
        "Ruby",
        {
            "many": "Content NonStruct",
            "ruby sequence": "RB RP RT",
        },
    ),
    (
        "RB RT RP WT",
        {
            "many": "Annot Artifact Em Form Link NonStruct Span Strong Sub",
            "role-specific": "Content",
        },
    ),
    (
        "Warichu",
        {
            "many": "Content NonStruct",
            "warichu sequence": "WP WT",
        },
    ),
    (
        "WP",
        {
            "many": "Annot Artifact Em Figure Form Link NonStruct Span Strong Sub",
            "role-specific": "Content",
        },
    ),
    (
        "FENote",
        {
            "grouping only": "Caption DocumentFragment",
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula L Lbl Link NonStruct P Part Ruby "
                "Sect Span Strong Sub Table Warichu"
            ),
        },
    ),
    (
        "L",
        {
            "many": "Artifact L LI NonStruct",
            "optional": "Caption",
        },
    ),
    (
        "LI",
        {
            "many": "Artifact Content Div LBody Lbl NonStruct",
        },
    ),
    (
        "LBody",
        {
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula Hn L Link NonStruct P Part Ruby "
                "Sect Span Strong Sub Table Warichu"
            ),
            "optional": "Caption H",
        },
    ),
    (
        "Table",
        {
            "many": "Artifact NonStruct TBody TR",
            "optional": "Caption TFoot THead",
        },
    ),
    (
        "TR",
        {
            "many": "Artifact NonStruct TD TH",
        },
    ),
    (
        "TH TD",
        {
            "many": (
                "Annot Artifact Content Div Em FENote Figure Form Formula Hn L Lbl Link NonStruct P Ruby Sect "
                "Span Strong Table Warichu"
            ),
            "optional": "H",
        },
    ),
    (
        "THead TBody TFoot",
        {
            "many": "Artifact NonStruct TR",
        },
    ),
    (
        "Caption",
        {
            "grouping only": "DocumentFragment",
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula Hn L Lbl Link NonStruct P Part "
                "Ruby Sect Span Strong Sub Table Warichu"
            ),
            "optional": "H",
        },
    ),
    (
        "Figure",
        {
            "grouping only": "Sub",
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula Hn L Lbl Link NonStruct P Part "
                "Ruby Sect Span Strong Table Warichu"
            ),
            "optional": "Caption H",
        },
    ),
    (
        "Formula",
        {
            "many": (
                "Annot Artifact Aside Content Div Em FENote Figure Form Formula Hn L Lbl Link NonStruct P Part "
                "Ruby Span Strong Sub Table Warichu"
            ),
            "optional": "Caption H",
        },
    ),
    (
        "Artifact",
        {
            "many": (
                "Annot Artifact Aside Content Div Document DocumentFragment Em FENote Figure Form Formula Hn L "
                "LBody LI Lbl Link NonStruct P Part RB RP RT Ruby Sect Span Strong Sub TBody TD TFoot TH THead "
                "TR Table Title WP WT Warichu"
            ),
            "optional": "Caption H",
        },
    ),
)

_RULES: dict[str, dict[str, str]] = {
    parent: {child: rule for rule, children in groups.items() for child in children.split()}
    for parents, groups in _GROUPS
    for parent in parents.split()
}


def _name(role: str) -> str:
    if role.startswith("H") and role[1:].isascii() and role[1:].isdigit() and role[1] != "0":
        return "Hn"
    return "" if role == "Hn" else role


def occurrence(parent: str, child: str, count: int) -> tuple[str, bool | None]:
    """Resolve unconditional numerical bounds and identify conditional relationships."""
    parent, child = _name(parent), _name(child)
    if parent not in _RULES or (child != "content item" and child not in _RULES):
        return "unmapped role", None
    rule = _RULES[parent].get("Content" if child == "content item" else child, "forbidden")
    if rule == "exactly one":
        return rule, count == 1
    if rule == "optional":
        return rule, count <= 1
    if rule == "forbidden":
        return rule, count == 0
    if rule == "many":
        return rule, True
    return rule, None
