# Type stubs for the extension module that crates/pyropdf-python builds.

from os import PathLike
from typing import Any, Literal, TypeAlias, final

from pyropdf._pixmap import Colorspace

__version__: str

class PdfError(Exception): ...
class MalformedPdfError(PdfError): ...
class PasswordError(PdfError): ...
class EncryptedDocumentError(PdfError): ...

class LimitError(PdfError):
    @property
    def code(self) -> str: ...

class StaleObjectError(PdfError): ...
class DocumentClosedError(PdfError): ...
class OcrError(PdfError): ...

@final
class CancellationToken:
    def __init__(self) -> None: ...
    def cancel(self) -> None: ...
    @property
    def cancelled(self) -> bool: ...

@final
class _Document:
    @staticmethod
    def empty(limits: object, /) -> _Document: ...
    @staticmethod
    def open_path(path: str, limits: object, password: bytes | None = None, /) -> _Document: ...
    @staticmethod
    def open_buffer(
        data: bytes | bytearray | memoryview, limits: object, password: bytes | None = None, /
    ) -> _Document: ...
    def is_repaired(self) -> bool: ...
    def save_path(
        self,
        path: str,
        incremental: bool,
        encryption: object = None,
        prune: bool = False,
        compress: int | None = None,
        decompress: bool = False,
        renew_id: bool = False,
        cross_reference_stream: bool | None = None,
        object_streams: bool = False,
        renumber: bool = False,
        dedupe: bool = False,
        /,
    ) -> None: ...
    def to_bytes(
        self,
        incremental: bool,
        max_size: int | None,
        encryption: object = None,
        prune: bool = False,
        compress: int | None = None,
        decompress: bool = False,
        renew_id: bool = False,
        cross_reference_stream: bool | None = None,
        object_streams: bool = False,
        renumber: bool = False,
        dedupe: bool = False,
        /,
    ) -> bytes: ...
    def _objects(self) -> list[tuple[int, int, bytes]]: ...
    def close(self) -> None: ...

@final
class CharColumns:
    def __len__(self) -> int: ...
    def rows(
        self, start: int, stop: int, /
    ) -> list[tuple[str, tuple[float, float, float, float], tuple[float, float], bool, tuple[int, int] | None]]: ...
    def column(self, name: str, start: int, stop: int, /) -> Column: ...
    def arrow_schema(self) -> object: ...
    def arrow_array(self, start: int, stop: int, /) -> tuple[object, object]: ...

@final
class Column:
    def __buffer__(self, flags: int, /) -> memoryview: ...

@final
class Pixmap:
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def channels(self) -> int: ...
    @property
    def alpha(self) -> bool: ...
    @property
    def colorspace(self) -> Colorspace: ...
    @property
    def stride(self) -> int: ...
    @property
    def resolution(self) -> tuple[int, int]: ...
    def invert(self, rect: tuple[int, int, int, int] | None = None) -> Pixmap: ...
    def gamma(self, value: float) -> Pixmap: ...
    def tint(
        self, black: tuple[int, int, int] = (0, 0, 0), white: tuple[int, int, int] = (255, 255, 255)
    ) -> Pixmap: ...
    def to_gray(self) -> Pixmap: ...
    def to_rgb(self) -> Pixmap: ...
    def with_alpha(self) -> Pixmap: ...
    def without_alpha(self, background: tuple[int, int, int] = (255, 255, 255)) -> Pixmap: ...
    def crop(self, rect: tuple[int, int, int, int]) -> Pixmap: ...
    def scale(self, width: int, height: int) -> Pixmap: ...
    @staticmethod
    def from_bytes(data: bytes | bytearray | memoryview | Pixmap) -> Pixmap: ...
    @staticmethod
    def open(path: str | PathLike[str]) -> Pixmap: ...
    def to_pil(self) -> Any: ...  # noqa: ANN401 - a PIL.Image.Image; Pillow is optional
    def to_bytes(self, format: str = "png", *, quality: int = 95, max_size: int | None = ...) -> bytes: ...  # noqa: A002
    def save(self, path: str | PathLike[str], /, *, format: str | None = None, quality: int = 95) -> None: ...  # noqa: A002
    def __buffer__(self, flags: int, /) -> memoryview: ...

def insert_ocr_layer(
    document: _Document,
    number: int,
    generation: int,
    words: list[tuple[float, float, float, float, str]],
    rotation: int,
    /,
) -> bool: ...

class _OcrEngine:
    def __init__(
        self,
        detector_path: str,
        recognizer_path: str,
        dictionary_path: str,
        threads: int,
        max_model_size: int | None,
        /,
    ) -> None: ...
    def recognize_pixmap(
        self,
        pixmap: Pixmap,
        *,
        tile_size: int = 1408,
        overlap: int = 192,
        min_confidence: float = 0.5,
        rotation: int = 0,
        document: _Document | None = None,
    ) -> list[tuple[float, float, float, float, str, float]]: ...

def render_page(
    document: _Document,
    number: int,
    generation: int,
    matrix: tuple[float, float, float, float],
    alpha: bool,
    gray: bool,
    background: tuple[int, int, int, int],
    clip: tuple[float, float, float, float] | None,
    annotations: bool,
    /,
) -> Pixmap: ...
def render_page_svg(
    document: _Document, number: int, generation: int, annotations: bool, max_size: int | None, /
) -> str: ...
def render_pages(
    document: _Document,
    pages: list[int] | None,
    matrix: tuple[float, float, float, float],
    alpha: bool,
    gray: bool,
    background: tuple[int, int, int, int],
    annotations: bool,
    format: str,  # noqa: A002
    quality: int,
    workers: int,
    max_size: int | None,
    /,
) -> list[bytes]: ...
def web_limits() -> dict[str, Any]: ...
def probe(
    path: str | None,
    data: bytes | bytearray | memoryview | None,
    max_file_size: int | None,
    password: bytes | None = None,
    /,
) -> tuple[int, str, bool, bool, dict[str, str]]: ...
def header_version(document: _Document, /) -> str | None: ...
def page_count(document: _Document, /) -> int: ...
def page_object(document: _Document, at: int, /) -> tuple[int, int]: ...
def page_index(document: _Document, number: int, generation: int, /) -> int | None: ...
def page_objects(document: _Document, /) -> list[tuple[int, int]]: ...
def page_geometry(document: _Document, number: int, generation: int, /) -> tuple[int, float, float] | None: ...
def page_image(
    document: _Document, number: int, generation: int, ordinal: int, annotations: bool, /
) -> Pixmap | None: ...
def image_stored(document: _Document, root: tuple[int, int], /) -> tuple[str, bytes] | None: ...
def decode_png(document: _Document, data: bytes, /) -> tuple[int, int, bool, bytes, bytes | None]: ...
def used_glyphs(document: _Document, fonts: list[tuple[int, int]], /) -> list[list[int]]: ...
def subset_font_program(data: bytes, format: str, keep: list[int], /) -> tuple[bytes, int, int] | None: ...  # noqa: A002
def page_boxes(
    document: _Document, number: int, generation: int, /
) -> list[tuple[float, float, float, float]] | None: ...
def set_page_box(
    document: _Document, number: int, generation: int, kind: int, rect: tuple[float, float, float, float], /
) -> bool: ...
def set_page_rotation(document: _Document, number: int, generation: int, rotation: int, /) -> bool: ...
def set_pages(document: _Document, pages: list[tuple[int, int]], copies: list[bool] = ..., /) -> None: ...

_Id: TypeAlias = tuple[int, int]
_Annotation: TypeAlias = tuple[
    int,
    _Id,
    str | None,
    tuple[float, float, float, float],
    int,
    list[str | None],
    list[float],
    list[float],
    float | None,
    tuple[float, str, list[float]] | None,
    str | None,
    tuple[_Id | None, tuple[float, float, float, float] | None, bool],
]

def annotation_list(document: _Document, number: int, generation: int, /) -> list[_Annotation] | None: ...
def annotation_read(
    document: _Document, number: int, generation: int, index: int, annotation: _Id, /
) -> _Annotation | None: ...
def annotation_add(document: _Document, number: int, generation: int, spec: dict[str, object], /) -> _Id | None: ...
def annotation_geometry(
    document: _Document, number: int, generation: int, index: int, annotation: _Id, /
) -> tuple[list[list[tuple[float, float]]] | None, tuple[str, str] | None] | None: ...
def annotation_edit(
    document: _Document, number: int, generation: int, index: int, annotation: _Id, edit: dict[str, object], /
) -> _Id | None: ...
def annotation_popup(
    document: _Document,
    number: int,
    generation: int,
    index: int,
    annotation: _Id,
    area: tuple[float, float, float, float] | None,
    /,
) -> _Id | None: ...
def annotation_open(
    document: _Document, number: int, generation: int, index: int, annotation: _Id, is_open: bool, /
) -> _Id | None: ...
def annotation_reply(
    document: _Document, number: int, generation: int, index: int, annotation: _Id, target: _Id | None, /
) -> _Id | None: ...
def annotation_remove(document: _Document, number: int, generation: int, handles: list[tuple[int, _Id]], /) -> bool: ...

_Target: TypeAlias = tuple[str, int | None, tuple[float, float] | None, float | None, str | None, str | None]
_Link: TypeAlias = tuple[int, _Id, tuple[float, float, float, float], _Target]
_Place: TypeAlias = tuple[int, tuple[float, float] | None, float | None]

def link_list(document: _Document, number: int, generation: int, /) -> list[_Link] | None: ...
def link_read(document: _Document, number: int, generation: int, index: int, annotation: _Id, /) -> _Link | None: ...
def link_add(
    document: _Document, number: int, generation: int, area: tuple[float, float, float, float], target: _Target, /
) -> _Id | None: ...
def link_edit(
    document: _Document,
    number: int,
    generation: int,
    index: int,
    annotation: _Id,
    area: tuple[float, float, float, float] | None,
    target: _Target | None,
    /,
) -> _Id | None: ...
def named_destinations(document: _Document, /) -> list[tuple[str, _Place | None]]: ...

_Rgb: TypeAlias = tuple[float, float, float]
_OutlineItem: TypeAlias = tuple[int, str, _Target, _Rgb | None, int, int, bool]
_OutlineEntry: TypeAlias = tuple[_Id, int, str, _Target, _Rgb | None, int, int]

_Widget: TypeAlias = tuple[int, _Id, _Id | None, tuple[float, float, float, float], int, list[str], str | None]
_WidgetStyle: TypeAlias = tuple[
    tuple[list[float], list[float], str | None, int],
    tuple[float, str, list[float]] | None,
    tuple[str, float, tuple[float, float, float]] | None,
]

def form_fields(document: _Document, /) -> list[tuple[str, _Id]]: ...
def form_field(document: _Document, name: str, /) -> _Id | None: ...
def form_field_read(document: _Document, id: _Id, /) -> tuple[str, str, int, str | None]: ...  # noqa: A002
def form_field_fill(document: _Document, id: _Id, value: str | bool, /) -> None: ...  # noqa: A002
def form_field_choices(document: _Document, id: _Id, /) -> list[tuple[str, str]]: ...  # noqa: A002
def form_field_details(document: _Document, id: _Id, /) -> tuple[str | None, int | None, str | None]: ...  # noqa: A002
def form_field_edit(document: _Document, id: _Id, edit: dict[str, object], /) -> None: ...  # noqa: A002
def flatten(document: _Document, page: _Id | None, annotations: bool, widgets: bool, /) -> bool: ...
def form_field_remove(document: _Document, field: _Id, /) -> None: ...
def form_field_widgets(document: _Document, id: _Id, /) -> list[tuple[_Id, _Id, int]]: ...  # noqa: A002
def form_repair_radio_groups(document: _Document, /) -> int: ...
def form_set_need_appearances(document: _Document, value: bool, /) -> None: ...
def form_walk_counts(document: _Document, /) -> tuple[int, int]: ...
def widget_add(
    document: _Document, number: int, generation: int, spec: dict[str, object], /
) -> tuple[_Id, int] | None: ...
def widget_edit(
    document: _Document, number: int, generation: int, index: int, widget: _Id, edit: dict[str, object], /
) -> _Id | None: ...
def widget_list(document: _Document, number: int, generation: int, /) -> list[_Widget] | None: ...
def widget_read(document: _Document, number: int, generation: int, index: int, widget: _Id, /) -> _Widget | None: ...
def widget_remove(document: _Document, number: int, generation: int, handles: list[tuple[int, _Id]], /) -> bool: ...
def widget_style(
    document: _Document, number: int, generation: int, index: int, widget: _Id, /
) -> _WidgetStyle | None: ...
def outline_children(document: _Document, parent: _Id | None = None, /) -> list[_Id]: ...
def outline_item(document: _Document, id: _Id, /) -> _OutlineItem: ...  # noqa: A002
def outline_entries(document: _Document, /) -> list[_OutlineEntry]: ...
def outline_walk_counts(document: _Document, /) -> tuple[int, int]: ...
def outline_write(document: _Document, specs: list[tuple[int, str, _Target, _Rgb | None, int, bool]], /) -> None: ...
def outline_set_title(document: _Document, id: _Id, title: str, /) -> None: ...  # noqa: A002
def outline_set_target(document: _Document, id: _Id, target: _Target, /) -> None: ...  # noqa: A002
def outline_set_style(
    document: _Document,
    id: _Id,  # noqa: A002
    color: _Rgb | None,
    flags: int | None = None,
    flag: tuple[int, bool] | None = None,
    /,
) -> None: ...
def outline_set_open(document: _Document, id: _Id, open: bool, /) -> None: ...  # noqa: A002
def import_pages(document: _Document, source: _Document, pages: list[tuple[int, int]], at: int, /) -> None: ...
def new_page(document: _Document, at: int, width: float, height: float, /) -> tuple[int, int]: ...

_Root: TypeAlias = tuple[int, int] | None
_Step: TypeAlias = int | bytes

def object_lookup(document: _Document, number: int, /) -> tuple[int, object] | None: ...
def object_numbers(document: _Document, /) -> list[int]: ...
def object_get(document: _Document, root: _Root, path: list[_Step], step: _Step, /) -> object: ...
def object_keys(document: _Document, root: _Root, path: list[_Step], /) -> list[bytes]: ...
def object_len(document: _Document, root: _Root, path: list[_Step], /) -> int: ...
def object_set(document: _Document, root: _Root, path: list[_Step], step: _Step, value: object, /) -> None: ...
def object_delete(document: _Document, root: _Root, path: list[_Step], step: _Step, /) -> None: ...
def object_free_unreferenced(document: _Document, candidates: list[int], /) -> list[int]: ...
def object_insert(document: _Document, root: _Root, path: list[_Step], index: int, value: object, /) -> None: ...
def object_replace(document: _Document, number: int, value: object, /) -> None: ...
def object_add(document: _Document, value: object, /) -> tuple[int, int]: ...
def object_add_stream(document: _Document, dictionary: object, data: bytes, compress: bool, /) -> tuple[int, int]: ...
def stream_read(document: _Document, root: tuple[int, int], decode: bool, max_size: int | None, /) -> bytes: ...
def stream_write(document: _Document, root: tuple[int, int], data: bytes, compress: bool, /) -> None: ...
def decode_text_string(data: bytes, /) -> str: ...
def parse_operations(data: bytes, max_operations: int | None, max_size: int | None, /) -> tuple[list[Any], bytes]: ...
def page_operations(document: _Document, number: int, generation: int, /) -> tuple[list[Any], bytes] | None: ...
def page_content(
    document: _Document,
    number: int,
    generation: int,
    kinds: tuple[bool, bool, bool],
    annotations: bool,
    clip: tuple[float, float, float, float] | None,
    max_elements: int | None,
    /,
) -> tuple[list[Any], list[Any], list[Any], list[Any], list[Any], list[Any]]: ...

_Box: TypeAlias = tuple[float, float, float, float]
_CoreTable: TypeAlias = tuple[
    _Box, int, int, list[tuple[_Box, str] | None], list[int], tuple[float, float | None, float | None, float | None]
]
_TextOptions: TypeAlias = tuple[tuple[float, float, float, float] | None, bool, int, bool, bool, bool, bool, bool]

def page_text(
    document: _Document,
    number: int,
    generation: int,
    options: _TextOptions,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[str, int]: ...
def document_text(
    document: _Document,
    pages: list[int] | None,
    options: _TextOptions,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[str, int]: ...
def page_result_counts(document: _Document, /) -> tuple[int, int]: ...
def page_layout(
    document: _Document,
    number: int,
    generation: int,
    options: _TextOptions,
    chars: bool,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[float, float, list[Any], int, CharColumns | None]: ...
def page_chars(
    document: _Document,
    number: int,
    generation: int,
    options: _TextOptions,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[CharColumns, int]: ...
def page_search(
    document: _Document,
    number: int,
    generation: int,
    needle: str,
    quads: bool,
    options: _TextOptions,
    max_hits: int | None,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[list[Any], int]: ...
def page_selection(
    document: _Document,
    number: int,
    generation: int,
    start: tuple[float, float],
    end: tuple[float, float],
    options: _TextOptions,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[str, int]: ...
def page_tables(
    document: _Document,
    number: int,
    generation: int,
    text_strategy: bool,
    clip: tuple[float, float, float, float] | None,
    max_glyphs: int | None,
    max_size: int | None,
    /,
) -> tuple[list[_CoreTable], int]: ...
def begin_transaction(document: _Document, name: str, /) -> None: ...
def end_transaction(document: _Document, commit: bool, /) -> None: ...
def changes(
    document: _Document, incremental: bool, /
) -> tuple[list[tuple[str, int, str, list[int]]], list[tuple[str, int, str, list[int]]]]: ...
def structural_check(
    document: _Document, /
) -> tuple[
    list[
        tuple[int | None, str, str, str, Literal["type", "required", "version", "indirect", "value", "invariant"], str]
    ],
    bool,
    int,
    int,
    str,
]: ...
def history_state(document: _Document, /) -> tuple[list[str], int, int | None, int]: ...
def history_undo(document: _Document, /) -> None: ...
def history_redo(document: _Document, /) -> None: ...
def set_history_limit(document: _Document, limit: int | None, /) -> None: ...
def history_clear(document: _Document, /) -> None: ...

# One type of a key: kind, linked object types, default, possible values.
_SchemaType: TypeAlias = tuple[str, list[str], str | None, list[str]]
# One key: name, inheritable, required (TRUE, FALSE, or a predicate), types.
_SchemaKey: TypeAlias = tuple[str, bool, str, list[_SchemaType]]

def schema_names() -> list[str]: ...
def schema_object(name: str, /) -> list[_SchemaKey] | None: ...
def security_state(document: _Document, /) -> tuple[bool, bool, str | None, int | None, str | None]: ...
def unlock(document: _Document, password: bytes, /) -> str: ...
def _copy_windows_dacl(source: PathLike[str] | str, destination: PathLike[str] | str, /) -> None: ...
def _stale_appearances(document: _Document, /) -> list[int]: ...
