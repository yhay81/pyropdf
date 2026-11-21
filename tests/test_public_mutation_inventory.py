"""Classify the public Python operations against the save-and-reopen harness.

Each mutation names its executable case and the PDF dependencies behind that
choice. A setter and its deleter are different entry points. Collection mixins
are expanded through the primitive that actually writes, so a new override
still needs classification. The generated Arlington types use View's four
writing protocols for every schema key.
"""

from __future__ import annotations

import inspect
import operator
from collections.abc import MutableMapping, MutableSequence
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from test_preservation import EDITS
from test_preservation_operations import nothing

import pyropdf

if TYPE_CHECKING:
    from collections.abc import Iterator

# Each line groups only operations with the stated dependency. An '=' suffix
# denotes a property assignment and 'del:' denotes a property deletion.
MUTATIONS = (
    (
        "sign_revision",
        "Adopt the signed incremental revision, binding the field and its byte ranges.",
        "Document",
        "sign",
    ),
    (
        "timestamp_revision",
        "Adopt the RFC 3161 timestamp revision and verify its byte-range imprint.",
        "Document",
        "timestamp",
    ),
    ("catalog_entry", "Name and scalar dictionary entries rewrite their owner.", "Dictionary", "__setitem__"),
    (
        "geospatial_registration",
        "Replace only the owner's registration, retaining independent controls and shared dictionaries.",
        "Dictionary",
        "geospatial_measure=",
    ),
    ("delete_entry", "Removing a dictionary key preserves its siblings.", "Dictionary", "__delitem__"),
    (
        "array_insert_delete",
        "Array edits retain the order and values of the surviving elements.",
        "Array",
        "insert __setitem__ __delitem__",
    ),
    ("replace_object", "Replace an indirect value without changing its identity.", "Objects", "__setitem__"),
    ("add_object", "Allocate a fresh indirect object.", "Objects", "add"),
    ("rewrite_stream", "Write stream bytes and maintain Length, Filter and DecodeParms.", "Stream", "write"),
    ("rewrite_stream", "Allocate stream data and its encoding dictionary.", "Objects", "add_stream"),
    (
        "typed_set",
        "Attribute assignment delegates to the same schema-checked indexed assignment.",
        "typed.View",
        "__setattr__ __setitem__",
    ),
    ("typed_delete", "Both spellings remove the selected described key.", "typed.View", "__delattr__ __delitem__"),
    ("new_page", "Insert a new leaf into the page tree.", "Pages", "new"),
    ("move_page", "Reorder the page tree and update its label ranges.", "Pages", "move"),
    ("delete_page", "Remove a leaf and prune destinations, structure and other referents.", "Pages", "__delitem__"),
    ("rotate", "Store the new display rotation on the page.", "Page", "rotation="),
    ("assign_operations", "Write the page's new content stream.", "Page", "operations="),
    ("canvas_path", "A clean canvas exit writes buffered operators and resources as one transaction.", "Page", "draw"),
    ("canvas_path", "Commit the buffered content on clean exit, discard on failure.", "Canvas", "__enter__ __exit__"),
    (
        "canvas_path",
        "Path construction and painting append geometry to the same content stream.",
        "Canvas",
        "path line rect circle ellipse polygon bezier draw",
    ),
    (
        "canvas_path",
        "These commands build the path consumed by Canvas.draw.",
        "PathBuilder",
        "move_to line_to curve_to quad_to close rect ellipse circle polygon sector zigzag wave",
    ),
    ("canvas_text", "Both lay out glyphs and maintain embedded fonts, widths and ToUnicode.", "Canvas", "text textbox"),
    ("canvas_image", "Register image resources and add a placement to page content.", "Canvas", "image"),
    ("add_attachment", "Insert a file specification in the embedded-files name tree.", "Attachments", "add"),
    ("delete_attachment", "Prune the name tree and release unreferenced streams.", "Attachments", "__delitem__"),
    ("write_attachment", "Replace the embedded stream; update size and checksum.", "Attachment", "write"),
    ("add_annotation", "Create the annotation, page Annots entry and appearance.", "Annotations", "add"),
    (
        "add_screen",
        "A Screen adds a self-targeted rendition, its clip and its embedded file to the annotation.",
        "Annotations",
        "add",
    ),
    ("add_screen", "Validate and commit changes to the linked rendition.", "RenditionAction", "edit_rendition"),
    ("delete_annotation", "Detach annotation and owned appearance from page Annots.", "Annotations", "__delitem__"),
    (
        "edit_annotation",
        "Common annotation edits preserve the handle and settle its appearance.",
        "Annotation",
        (
            "rect= contents= del:contents author= del:author subject= del:subject name= del:name icon= "
            "color= del:color interior_color= del:interior_color opacity= flags= border= del:border "
            "clouds= del:clouds blend_mode= del:blend_mode vertices= strokes= line_endings= rotation= "
            "language= del:language"
        ),
    ),
    (
        "edit_annotation",
        "Writes in the transaction settle appearances once and publish one history step.",
        "Document",
        "transaction",
    ),
    (
        "edit_annotation",
        "Entering and leaving the transaction groups these same writes.",
        "Transaction",
        "__enter__ __exit__",
    ),
    (
        "flatten_annotations",
        "Flatten appearances into page resources and prune annotations and structure.",
        "Document",
        "flatten",
    ),
    ("flatten_annotations", "The page form calls the same flattener restricted to one page.", "Page", "flatten"),
    (
        "flatten_widgets",
        "Flatten widget appearances into page resources and remove their field and structure memberships.",
        "Page",
        "flatten",
    ),
    ("add_field", "Create the field, attach widgets and prepare their appearances.", "Widgets", "add"),
    ("delete_field", "Unregister fields and remove their widgets from page annotations.", "Form", "__delitem__"),
    ("delete_widget", "Remove the widget and its dependent field membership.", "Widgets", "__delitem__"),
    (
        "fill_field",
        "Field edits redraw the field's widgets and settle their appearances.",
        "Field",
        "flags= choices= label= del:label max_length= del:max_length default_value= del:default_value value= reset",
    ),
    ("fill_field", "Bind the font used to regenerate field appearances.", "Form", "font="),
    (
        "fill_field",
        "Widget style setters redraw its appearance and maintain font resources.",
        "Widget",
        (
            "rect= flags= border= del:border border_color= del:border_color background_color= "
            "del:background_color caption= del:caption rotation= text_color= font= font_size="
        ),
    ),
    (
        "import_form_data",
        "FDF and XFDF decode independently then share the atomic field/appearance update.",
        "Form",
        "import_fdf import_xfdf",
    ),
    (
        "structure_role_map",
        "Map a custom role to a standard one; update or retire the RoleMap dictionary.",
        "RoleMap",
        "__setitem__ __delitem__ update clear",
    ),
    (
        "structure_role_map",
        "Replace or retire the root's mapping of custom roles.",
        "StructureTree",
        "role_map= del:role_map",
    ),
    (
        "structure_role_map",
        "Set or remove element dictionary values while keeping its parent and siblings.",
        "StructureElement",
        (
            "role= title= del:title language= del:language alt= del:alt actual_text= del:actual_text "
            "expanded= del:expanded"
        ),
    ),
    (
        "structure_role_map",
        "Insert or reorder children in their parent's K reading order.",
        "StructureChildren",
        "add move",
    ),
    (
        "structure_role_map",
        "Root-level elements likewise update the root's K reading order.",
        "StructureTree",
        "add move",
    ),
    (
        "shared_action_trigger",
        "Replace the catalog action; retire only unshared action objects.",
        "Document",
        "open_action= del:open_action",
    ),
    (
        "shared_action_trigger",
        "Replace or remove page AA events and release unshared actions.",
        "PageTriggers",
        "__setitem__ __delitem__ update clear",
    ),
    (
        "shared_action_trigger",
        "Page-level assignment and deletion operate on the same AA dictionary.",
        "Page",
        "triggers= del:triggers",
    ),
    (
        "viewer_preferences",
        "Catalog preference writes honor the indirect dictionary and remove it on deletion.",
        "Document",
        "viewer_preferences= del:viewer_preferences",
    ),
    (
        "article_beads",
        "Edit thread Info through the same direct or indirect dictionary.",
        "Thread",
        (
            "title= del:title author= del:author subject= del:subject keywords= del:keywords created= "
            "del:created modified= del:modified"
        ),
    ),
    ("article_beads", "Maintain the circular N/V chain, first bead, and page B order.", "Beads", "move"),
    ("article_beads", "Convert display geometry to the bead's page-space R entry.", "Bead", "rect="),
    (
        "collection",
        "Replace catalog Collection and retire only unreferenced old objects.",
        "Document",
        "collection= del:collection",
    ),
    ("undo", "Republish the previous version of every edited object.", "History", "undo"),
    ("redo", "Republish the saved forward version.", "History", "redo"),
    (
        "action_triggers",
        "Widget and screen AA use the same handle-bound action writer; preserve any field actions.",
        "ActionTriggers",
        "__setitem__ __delitem__ update clear",
    ),
    (
        "action_triggers",
        "Set or delete the screen annotation's A/AA and preserve shared action references.",
        "Annotation",
        "action= del:action triggers= del:triggers",
    ),
    (
        "action_triggers",
        "The widget wraps ActionTriggers and retains field-level scripts.",
        "Widget",
        "triggers= del:triggers",
    ),
    (
        "action_triggers",
        "Field-format scripts and widget events share AA but have separate keys.",
        "Scripts",
        "__setitem__ __delitem__",
    ),
    (
        "annotation_relations",
        "Keep the popup pair, reply target and page Annots membership consistent.",
        "Annotation",
        "in_reply_to= del:in_reply_to popup_rect= del:popup_rect is_open=",
    ),
    (
        "file_specification",
        "Edit file specification strings or embedded-file Params without replacing the data stream.",
        "AnnotationFile",
        "created= del:created description= del:description modified= del:modified filename=",
    ),
    (
        "file_specification",
        "File specifications in the name tree use the same EF and Params fields.",
        "Attachment",
        "created= del:created description= del:description modified= del:modified filename=",
    ),
    (
        "file_specification",
        "Replace the annotation file stream while retaining its file specification.",
        "AnnotationFile",
        "write",
    ),
    (
        "related_files",
        "Replace or remove ordered filename/stream pairs in the file specification shared with attachments.",
        "AnnotationFile",
        "related_files= del:related_files",
    ),
    (
        "related_files",
        "Replace or remove ordered filename/stream pairs while retaining streams referenced by other files.",
        "Attachment",
        "related_files= del:related_files",
    ),
    (
        "collection_item",
        "Set and retire the file specification's CI entries while preserving its embedded bytes.",
        "Attachment",
        "collection_item= del:collection_item",
    ),
    (
        "article_membership",
        "Maintain page B memberships, the bead's P and its display-space rectangle.",
        "Bead",
        "page=",
    ),
    (
        "article_membership",
        "Maintain the ring, the affected page arrays, and actions referencing retired beads.",
        "Beads",
        "add __delitem__",
    ),
    (
        "article_membership",
        "Maintain catalog membership, bead rings, page arrays and article actions.",
        "Threads",
        "add __delitem__ move",
    ),
    (
        "article_membership",
        "Deleting all threads uses the same membership and reference cleanup.",
        "Document",
        "del:threads",
    ),
    (
        "canvas_path",
        "Graphics-state scopes and clipping wrap the buffered path in balanced operators.",
        "Canvas",
        "clip transform",
    ),
    (
        "canvas_resources",
        "Opacity and blend modes register ExtGState entries for the drawing.",
        "Canvas",
        "opacity blend",
    ),
    ("canvas_resources", "Use the layer's shared indirect OCG as the marked content property.", "Canvas", "layer"),
    (
        "canvas_resources",
        "A snapshot of the placed page's content, geometry and resources becomes a Form XObject.",
        "Canvas",
        "page",
    ),
    (
        "tagged_drawing",
        "Create marked content with an MCID and keep structure K and ParentTree connected.",
        "Canvas",
        "tag",
    ),
    (
        "catalog_entry",
        "Page layout, mode and language are scalar catalog keys; deletion removes that key.",
        "Document",
        "page_layout= del:page_layout page_mode= del:page_mode language= del:language",
    ),
    (
        "catalog_records",
        "Store capability requirements and retire their owned handlers.",
        "Document",
        "requirements= del:requirements",
    ),
    (
        "catalog_records",
        "Preserve indirect MarkInfo and Legal dictionaries when replacing their entries.",
        "Document",
        "mark_info= del:mark_info legal_attestation= del:legal_attestation",
    ),
    (
        "security_store",
        "Maintain and deduplicate shared status/certificate streams in DSS and VRI.",
        "Document",
        "security_store= del:security_store",
    ),
    (
        "signature_field",
        "Update the unsigned field's Lock and SV dictionaries while retaining its widget.",
        "Document",
        "prepare_signature",
    ),
    (
        "signature_field",
        "Field lock settings share the unsigned signature field's Lock entry.",
        "Field",
        "lock= del:lock",
    ),
    (
        "document_metadata",
        "Synchronize Info values with corresponding entries in the XMP metadata stream.",
        "Metadata",
        "__setitem__ __delitem__ update clear",
    ),
    (
        "document_metadata",
        "Replacing metadata uses the same Info/XMP synchronization; XMP itself owns the XML packet.",
        "Document",
        "metadata= xmp= del:xmp",
    ),
    (
        "document_metadata",
        "Replace or remove the metadata stream association for one component, isolating a shared packet.",
        "Dictionary",
        "xmp= del:xmp",
    ),
    (
        "named_pages",
        "Rebuild the named-page tree; keep page identity, sibling name trees and Limits.",
        "NamedPages",
        "__setitem__ __delitem__ clear update",
    ),
    (
        "named_pages",
        "Replacing/deleting the map uses the same tree writer and cleanup.",
        "Document",
        "named_pages= del:named_pages",
    ),
    (
        "outline",
        "Write linked siblings, parents, destinations and descendant Counts.",
        "Document",
        "outline= del:outline",
    ),
    (
        "outline",
        "Set item entries; opening or closing an item propagates its Count along Parents.",
        "OutlineItem",
        "title= destination= color= bold= italic= is_open=",
    ),
    (
        "page_labels",
        "Maintain the page-label number tree and free only unused nodes.",
        "Document",
        "page_labels= del:page_labels",
    ),
    (
        "document_scripts",
        "Named JavaScript actions and the JavaScript name tree are pruned together.",
        "DocumentScripts",
        "__setitem__ __delitem__ clear update",
    ),
    (
        "document_scripts",
        "Remove catalog AA events and associated indirect scripts.",
        "DocumentTriggers",
        "__setitem__ __delitem__ clear update",
    ),
    (
        "document_scripts",
        "Bulk setters use the catalog script/name-tree and AA writers.",
        "Document",
        "scripts= del:scripts triggers= del:triggers",
    ),
    (
        "structure_membership",
        "Prune descendants and repair ParentTree/IDTree entries for removed members.",
        "StructureTree",
        "__delitem__",
    ),
    ("structure_membership", "Delete the same subtree below a nested parent.", "StructureChildren", "__delitem__"),
    (
        "structure_membership",
        "Removing the root clears tagged relationships and MarkInfo.",
        "Document",
        "del:structure",
    ),
    (
        "structure_membership",
        "Reparent a subtree; maintain inherited page identity on content references.",
        "StructureElement",
        "page= del:page move",
    ),
    (
        "recolor",
        "Convert resources for selected pages; keep originals used on unselected pages.",
        "Document",
        "recolor",
    ),
    ("recolor", "The page call selects one page in the same recoloring implementation.", "Page", "recolor"),
    (
        "image_stream",
        "Replace the selected shared image bytes, metadata and soft mask for all placements.",
        "Document",
        "replace_image remove_image",
    ),
    (
        "rewrite_images",
        "Use placement resolution, preserve shared masks and rewrite selected samples.",
        "Document",
        "rewrite_images",
    ),
    (
        "subset_fonts",
        "Subset shared font programs and rename their descriptors while retaining glyph IDs.",
        "Document",
        "subset_fonts",
    ),
    (
        "radio_groups",
        "Reparent flat radio widget siblings under one field; keep widget state and actions.",
        "Form",
        "repair_radio_groups",
    ),
    ("fill_field", "Appearance regeneration clears NeedAppearances on the form.", "Form", "need_appearances="),
    (
        "history_restore",
        "Restore versions only to their matching fresh base, including edits, steps and redo.",
        "History",
        "restore",
    ),
    (
        "optional_content",
        "Maintain the OCG list, default order and the visibility/locked configuration.",
        "Layers",
        "add switch use",
    ),
    (
        "links",
        "Add or remove the link from page Annots; maintain its target and rectangle.",
        "Links",
        "add __delitem__",
    ),
    ("links", "Edit the existing link dictionary and keep other page annotations untouched.", "Link", "rect= target="),
    (
        "page_copies",
        "Import page content, widgets and structure into new page identities; extend delegates to insert.",
        "Pages",
        "insert extend",
    ),
    (
        "page_copies",
        "Select preserves requested pages, duplicates shared content, and prunes omitted pages.",
        "Pages",
        "select",
    ),
    (
        "new_page_image",
        "Create a page for each image frame, register its image and draw the placement.",
        "Pages",
        "new_from_image",
    ),
    (
        "page_boxes",
        "Apply the display-to-PDF transform to the page boundary without moving other pages.",
        "Page",
        "media_box= crop_box= bleed_box= trim_box= art_box=",
    ),
    (
        "remove_rotation",
        "Keep displays stable by transforming content, annotation geometry and page boxes.",
        "Page",
        "remove_rotation",
    ),
    (
        "page_records",
        "Replace transitions, viewport/measurement objects, thumbnail images and linked navigation nodes.",
        "Page",
        (
            "duration= del:duration transition= del:transition viewports= del:viewports thumbnail= "
            "del:thumbnail navigation_steps= del:navigation_steps"
        ),
    ),
    (
        "clean_content",
        "Normalize page content and prune resources; preserve shared/inherited resources.",
        "Page",
        "clean_contents",
    ),
    (
        "replace_text",
        "Re-encode a matching text operand with its source font and preserve adjacent operators.",
        "Page",
        "replace_text",
    ),
    ("ocr_layer", "Place the invisible text layer with its embedded font and glyph mappings.", "Page", "apply_ocr"),
    (
        "sanitize",
        "Sanitization requires a full output file; the current document also retains the transaction.",
        "Document",
        "sanitize",
    ),
)

# Pure readers, value factories/transformations and external or session-only
# effects are listed independently of edits to the versioned PDF.
READ_ONLY = {
    "Annotation": "pixmap text sound",
    "CancellationToken": "cancelled",
    "AnnotationFile": "read",
    "Attachment": "read",
    "Chars": "column",
    "Chunk": "to_dict",
    "Content": "path_clusters",
    "Document": "verify_integrity changes check to_bytes render text markdown to_json to_chunks font_program",
    "DocumentLimits": "web",
    "Element": "to_dict",
    "Font": "from_file from_bytes standard chars char_widths char_bbox program text_width text_height has_glyph",
    "Form": "to_fdf to_xfdf",
    "History": "save",
    "Layers": "visible",
    "Layout": "to_dict to_html to_xml",
    "Matrix": "planish",
    "Operations": "parse to_bytes",
    "Outline": "walk entries",
    "OutlineItem": "to_entry",
    "Page": "images fonts pixmap ocr svg content paths text words layout chars search selection markdown tables",
    "PageImage": "pixmap stored",
    "PageLabelRange": "label",
    "Pages": "labeled index",
    "Paths": "column",
    "Pixmap": (
        "invert gamma tint to_gray to_rgb with_alpha without_alpha crop scale rotate flip warp "
        "pixel with_pixel fill color_count colors top_color from_bytes open to_pil to_bytes "
        "height channels stride resolution width alpha colorspace"
    ),
    "Point": "distance_to",
    "Quad": "morph",
    "Real": "to_decimal",
    "Rect": "round intersects area norm normalized morph torect",
    "IRect": "intersects area norm normalized morph torect",
    "pyropdf": "open probe parse_fdf parse_xfdf parse_pdf_date pdf_date vri_key wrap_encrypted_payload",
    "Revision": "open",
    "Signature": "verify",
    "Signer": "from_pem from_pkcs12 callback",
    "Dictionary": "geospatial_point",
    "Stream": "read",
    "StructureTree": "resolve_role walk",
    "Table": "markdown",
}

EXTERNAL = {
    "CancellationToken": "cancel",
    "Document": "unlock save close __enter__ __exit__ fallback_fonts= del:fallback_fonts",
    "FallbackFonts": "__setitem__ __delitem__",
    "History": "max_size= clear",
    "OcrEngine": "recognize",
    "Pixmap": "save",
}


def public_members(cls: type) -> Iterator[str]:
    """Yield public call sites and writable property accessors defined on a public type."""
    # operator publishes every binary in-place protocol, including set and
    # numeric operations that a future mutable PDF collection might implement.
    protocols = {name for name in vars(operator) if name.startswith("__i") and "__" + name[3:] in vars(operator)}
    protocols.update(("__setitem__", "__delitem__", "__enter__", "__exit__", "__aenter__", "__aexit__"))
    immutable = getattr(getattr(cls, "__dataclass_params__", None), "frozen", False)
    if not immutable:
        protocols.update(("__setattr__", "__delattr__"))
    for name, member in vars(cls).items():
        if name.startswith("_") and name not in protocols:
            continue
        if isinstance(member, property):
            if member.fset is not None:
                yield f"{name}="
            if member.fdel is not None:
                yield f"del:{name}"
        elif not immutable and (inspect.isgetsetdescriptor(member) or inspect.ismemberdescriptor(member)):
            # Native descriptors do not expose whether a setter exists.
            # Classify their spelling explicitly, including read-only fields.
            yield name
        elif isinstance(member, (staticmethod, classmethod)) or inspect.isroutine(member):
            yield name


def public_operations() -> set[tuple[str, str]]:
    """Find arbitrary new method names as well as new setters and deleters."""
    types = {name: getattr(pyropdf, name) for name in pyropdf.__all__}
    typed = pyropdf.typed
    typed_exports = set(vars(typed)["__all__"])
    # The module exposes the generated classes through dir() and __getattr__.
    # Each inherits View's four mutations until it introduces its own writer.
    for name in dir(typed):
        if not name.startswith("_"):
            entry = getattr(typed, name)
            if name in typed_exports or (isinstance(entry, type) and issubclass(entry, typed.View)):
                types[f"typed.{name}"] = entry
    exported = {cls for cls in types.values() if isinstance(cls, type)}
    methods = {
        (owner, op)
        for owner, cls in types.items()
        if isinstance(cls, type) and not issubclass(cls, (Enum, BaseException))
        for base in cls.__mro__
        if base is cls or (base not in exported and base.__module__.startswith("pyropdf"))
        for op in public_members(base)
    }
    for name, entry in types.items():
        if inspect.isroutine(entry):
            module, _, member = name.rpartition(".")
            methods.add((module or "pyropdf", member))
    return methods


def test_a_typed_view_override_needs_its_own_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pyropdf.typed.Catalog, "__setitem__", pyropdf.typed.View.__setitem__)
    operations = public_operations()
    assert ("typed.Catalog", "__setitem__") in operations
    assert ("typed.PageObject", "__setitem__") not in operations


def test_native_read_only_descriptors_reject_assignment() -> None:
    with pyropdf.Document() as doc:
        pixmap = doc.pages.new((10, 10)).pixmap(dpi=72)
        for value in (pixmap, pyropdf.CancellationToken()):
            owner = type(value)
            for name in READ_ONLY[owner.__name__].split():
                if inspect.isgetsetdescriptor(vars(owner).get(name)):
                    with pytest.raises(AttributeError):
                        setattr(value, name, getattr(value, name))


def test_every_public_operation_has_a_classification() -> None:
    # An operation can have several cases for different input dependencies.
    assignments = [(owner, op, case) for case, _, owner, names in MUTATIONS for op in names.split()]
    assert len(assignments) == len(set(assignments))
    declared = {(owner, op) for owner, op, _ in assignments}
    for coverage in (READ_ONLY, EXTERNAL):
        for owner, names in coverage.items():
            for op in names.split():
                assert (owner, op) not in declared
                declared.add((owner, op))
    assert declared == public_operations()


def test_each_mutation_names_an_executable_save_case() -> None:
    assert {case for case, _, _, _ in MUTATIONS} == EDITS.keys() - {"none"}
    for case, dependencies, owner, names in MUTATIONS:
        assert dependencies.strip(), owner
        assert names.split(), owner
        assert callable(EDITS[case].apply), (owner, names, case)
        assert EDITS[case].check is not nothing, (owner, names, case)


def test_collection_mixins_still_delegate_to_classified_primitives() -> None:
    """A new override becomes an independently classified public member."""
    for owner in pyropdf.__all__:
        cls = getattr(pyropdf, owner)
        if not isinstance(cls, type):
            continue
        if issubclass(cls, MutableMapping):
            for name in ("clear", "pop", "popitem", "setdefault", "update"):
                assert getattr(cls, name) is getattr(MutableMapping, name) or (owner, name) in public_operations()
        if issubclass(cls, MutableSequence):
            for name in ("append", "clear", "extend", "pop", "remove", "reverse", "__iadd__"):
                assert getattr(cls, name) is getattr(MutableSequence, name) or (owner, name) in public_operations()


def test_arlington_checks_the_effective_version_of_saved_editor_output(tmp_path: Path) -> None:
    from test_verapdf import VERAPDF  # noqa: PLC0415 - load the independent validator driver for this test only.

    path = tmp_path / "collection.pdf"
    data = (Path(__file__).parent / "assets/pdfa/pdfa-1b.pdf").read_bytes()
    with pyropdf.open(data) as doc:
        doc.collection = pyropdf.Collection()
        path.write_bytes(doc.to_bytes(incremental=True))
    assert path.read_bytes().startswith(b"%PDF-1.4")
    assert VERAPDF.arlington(path, "1b") == "arlington1.7"
