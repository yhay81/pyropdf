"""PDF declarations and embedded resource relationships for STEP and glTF RichMedia."""

from __future__ import annotations

import json
import re
import struct
from typing import TYPE_CHECKING

from pyropdf import typed
from pyropdf._errors import LimitError
from pyropdf._interchange import ISO_EXTENSIONS
from pyropdf._objects import Array, Dictionary, Name, Stream, String

if TYPE_CHECKING:
    from pyropdf._document import Document
    from pyropdf._media import RichMediaModel
    from pyropdf._rich_media import _Work


_STEP = frozenset({"model/step", "model/step+xml", "model/step+zip", "model/step-xml+zip"})
_GLTF = frozenset({"model/gltf+json", "model/gltf-binary"})
_EXTENSION_LEVEL = {"STEP": 24064, "glTF": 32007}
_METADATA_BYTES = 4 << 20
_MAX_DEPTH = 64
_JSON_TOKENS = re.compile(rb'\\.|["{}\[\],:]', re.DOTALL)
_HEADER = struct.Struct("<4sIIII")
_CHUNK = struct.Struct("<I4s")
_JSON = int.from_bytes(b"JSON", "little")


def format_for(mime: object) -> str | None:
    if not isinstance(mime, str) or len(mime) > len("model/step-xml+zip"):
        return None
    if mime.lower() in _STEP:
        return "STEP"
    return "glTF" if mime.lower() in _GLTF else None


def _extension_entries(raw: object, work: _Work) -> Array | tuple[Dictionary, ...]:
    if isinstance(raw, Array):
        if raw.is_indirect:
            msg = "the ISO_ extensions array must be direct"
            raise ValueError(msg)
        work.add(len(raw))
        return raw
    if raw is None or (isinstance(raw, Dictionary) and not isinstance(raw, Stream)):
        return () if raw is None else (raw,)
    msg = "ISO_ must contain developer extension dictionaries"
    raise TypeError(msg)


def _text_matches(raw: object, text: str) -> bool:
    return isinstance(raw, String) and len(raw) <= len(text) * 4 + 4 and raw.text == text


def _declaration(doc: Document, model: str, work: _Work, *, create: bool) -> None:
    level = _EXTENSION_LEVEL[model]
    url, revision = ISO_EXTENSIONS[level]
    catalog = doc.catalog
    if create and "Extensions" not in catalog:
        catalog["Extensions"] = {}
    extensions = catalog.get("Extensions")
    if not isinstance(extensions, Dictionary) or isinstance(extensions, Stream) or extensions.is_indirect:
        msg = "RichMedia requires a direct catalog Extensions dictionary"
        raise ValueError(msg)
    raw = extensions.get("ISO_")
    entries = _extension_entries(raw, work)
    for entry in entries:
        if not isinstance(entry, Dictionary) or isinstance(entry, Stream):
            msg = "ISO_ must contain developer extension dictionaries"
            raise TypeError(msg)
        if entry.get("ExtensionLevel") == level:
            if (
                entry.is_indirect
                or type(entry.get("ExtensionLevel")) is not int
                or entry.get("Type") != Name("DeveloperExtensions")
                or entry.get("BaseVersion") != Name("2.0")
                or not _text_matches(entry.get("ExtensionRevision"), revision)
                or not _text_matches(entry.get("URL"), url)
            ):
                msg = "inconsistent ISO RichMedia developer extension"
                raise ValueError(msg)
            return
    if not create:
        msg = "missing ISO RichMedia developer extension"
        raise ValueError(msg)
    entry = {
        "Type": Name("DeveloperExtensions"),
        "BaseVersion": Name("2.0"),
        "ExtensionLevel": level,
        "ExtensionRevision": revision,
        "URL": url,
    }
    if isinstance(raw, Array):
        raw.append(entry)
    else:
        extensions["ISO_"] = [entry] if raw is None else [raw, entry]


def declare_step_requirement(doc: Document) -> None:
    from pyropdf import _core  # noqa: PLC0415
    from pyropdf._rich_media import _Work  # noqa: PLC0415

    _core.read_operation(doc._core(), lambda budget: _declaration(doc, "STEP", _Work(budget), create=True))  # noqa: SLF001


def declarations(doc: Document, model: str, work: _Work, *, add_version: str | None = None) -> tuple[str, ...]:
    from pyropdf._annotations import _version  # noqa: PLC0415
    from pyropdf._measurement import DocumentRequirement  # noqa: PLC0415

    if _version(doc) < (2, 0):
        msg = "STEP and glTF RichMedia require PDF 2.0"
        raise ValueError(msg)
    _declaration(doc, model, work, create=add_version is not None)
    cat = doc.catalog
    if add_version is not None and "Requirements" not in cat:
        cat["Requirements"] = []
    raw = cat.get("Requirements")
    if not isinstance(raw, Array):
        msg = "missing RichMedia Requirements array"
        raise TypeError(msg)
    work.add(len(raw), 128 * len(raw))
    versions = []
    for item in raw:
        if isinstance(item, Dictionary) and item.get("S") == model:
            version = item.get("V")
            if not isinstance(version, Name):
                msg = "the model requirement must specify a version name"
                raise ValueError(msg)
            DocumentRequirement(model, version=version)
            versions.append(str(version))
    if add_version is not None and add_version not in versions:
        raw.append({"Type": Name("Requirement"), "S": Name(model), "V": Name(add_version)})
        versions.append(add_version)
    if not versions:
        msg = "missing matching RichMedia model requirement"
        raise ValueError(msg)
    return tuple(versions)


def _json_chunk(data: bytes, mime: str, work: _Work) -> tuple[memoryview, int | None]:
    if mime.lower() != "model/gltf-binary":
        return memoryview(data), None
    if len(data) < _HEADER.size:
        msg = "truncated binary glTF header"
        raise ValueError(msg)
    magic, version, length, first, kind = _HEADER.unpack_from(data)
    if (magic, version, length, kind) != (b"glTF", 2, len(data), _JSON) or first % 4 or first > length - _HEADER.size:
        msg = "invalid binary glTF header or JSON chunk"
        raise ValueError(msg)
    offset = _HEADER.size + first
    binary = None
    while offset < length:
        work.add(1)
        if length - offset < _CHUNK.size:
            msg = "incomplete binary glTF chunk header"
            raise ValueError(msg)
        size, kind = _CHUNK.unpack_from(data, offset)
        if size % 4 or size > length - offset - _CHUNK.size or kind == b"JSON":
            msg = "invalid binary glTF chunk"
            raise ValueError(msg)
        if kind == b"BIN\0":
            if offset != _HEADER.size + first:
                msg = "binary glTF BIN must be the second chunk"
                raise ValueError(msg)
            binary = size
        offset += _CHUNK.size + size
    return memoryview(data)[_HEADER.size : _HEADER.size + first], binary


def _non_json_number(value: str) -> None:
    msg = f"invalid glTF JSON number {value}"
    raise ValueError(msg)


def _manifest(data: bytes, mime: str, work: _Work) -> tuple[dict, int | None]:
    raw, binary = _json_chunk(data, mime, work)
    if len(raw) > _METADATA_BYTES:
        msg = "glTF JSON exceeds 4 MiB"
        code = "annotation_input_size"
        raise LimitError(code, msg)
    # Bound nesting before calling the JSON decoder, and charge for each container and member.
    work.add(0, len(raw) * 8)
    quoted = False
    depth = 0
    for match in _JSON_TOKENS.finditer(raw):
        token = match[0]
        if token == b'"':
            quoted = not quoted
        elif not quoted:
            work.add(1, 192)
            if token in (b"{", b"["):
                depth += 1
                if depth > _MAX_DEPTH:
                    msg = "glTF JSON nesting exceeds 64"
                    code = "object_value_depth"
                    raise LimitError(code, msg)
            elif token in (b"}", b"]"):
                depth -= 1
    root = json.loads(bytes(raw).decode("utf-8"), parse_constant=_non_json_number)
    if not isinstance(root, dict) or not isinstance(root.get("asset"), dict) or root["asset"].get("version") != "2.0":
        msg = "glTF requires an asset declaring version 2.0"
        raise ValueError(msg)
    return root, binary


def file_stream(file: Dictionary) -> Stream | None:
    ef = file.get("EF")
    stream = ef.get("F") if isinstance(ef, Dictionary) else None
    return stream if isinstance(stream, Stream) else None


def _check_inline_resource(item: dict, key: str, index: int, binary: int | None) -> None:
    if key == "buffers" and (
        index
        or binary is None
        or type(item.get("byteLength")) is not int
        or not 0 < item["byteLength"] <= binary < item["byteLength"] + 4
    ):
        msg = "glTF buffer must reference an embedded URI or the GLB BIN chunk"
        raise ValueError(msg)
    if key == "images" and (type(item.get("bufferView")) is not int or not isinstance(item.get("mimeType"), str)):
        msg = "an inline glTF image requires a bufferView and MIME type"
        raise ValueError(msg)


def _resource_types(root: dict, binary: int | None) -> dict[str, str]:
    needed: dict[str, str] = {}
    for key in ("buffers", "images"):
        values = root.get(key, [])
        if not isinstance(values, list):
            msg = "glTF buffers and images must be arrays"
            raise TypeError(msg)
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                msg = "invalid glTF resource"
                raise TypeError(msg)
            uri = item.get("uri")
            if uri is None:
                _check_inline_resource(item, key, index, binary)
                continue
            if not isinstance(uri, str) or not uri:
                msg = "glTF resource URI must be a nonempty string"
                raise ValueError(msg)
            if uri[:5].lower() != "data:":
                expected = "application/octet-stream" if key == "buffers" else item.get("mimeType", "image/")
                if not isinstance(expected, str) or (uri in needed and needed[uri] != expected):
                    msg = "inconsistent glTF resource MIME types"
                    raise ValueError(msg)
                needed[uri] = expected
    return needed


def _gltf_resources(needed: dict[str, str], files: set[Dictionary], work: _Work) -> tuple[typed.FileSpecification, ...]:
    if not needed:
        return ()
    found: dict[str, typed.FileSpecification] = {}
    for file in files:
        raw = file.get("F")
        if isinstance(raw, String):
            work.add(0, 4 * len(raw))
            filename = raw.text
            if filename in needed:
                stream = file_stream(file)
                mime = stream.get("Subtype") if stream is not None else None
                expected = needed[filename]
                if (
                    filename in found
                    or not isinstance(mime, Name)
                    or not (
                        mime.lower() == expected
                        or (expected == "image/" and mime.lower().startswith(expected))
                        or (expected == "application/octet-stream" and mime.lower() == "application/gltf-buffer")
                    )
                ):
                    msg = "ambiguous or incorrectly typed embedded glTF resource"
                    raise ValueError(msg)
                found[filename] = typed.FileSpecification(file)
    if needed.keys() != found.keys():
        msg = "a glTF resource URI has no matching embedded file specification"
        raise ValueError(msg)
    return tuple(found[uri] for uri in needed)


def inspect_model(
    doc: Document, config: Dictionary, instance: Dictionary, files: set[Dictionary], work: _Work
) -> RichMediaModel | None:
    from pyropdf._media import RichMediaModel  # noqa: PLC0415

    file = instance.get("Asset")
    if not isinstance(file, Dictionary):
        return None
    stream = file_stream(file)
    mime = stream.get("Subtype") if stream is not None else None
    model = format_for(mime)
    if isinstance(mime, String) and len(mime) <= len("model/step-xml+zip") and format_for(mime.text):
        msg = "the model stream Subtype must be a PDF name"
        raise TypeError(msg)
    if model is None or not isinstance(mime, Name) or stream is None:
        return None
    if instance.get("Subtype") != "3D" or (model == "glTF" and file not in files):
        msg = "a model requires a 3D instance and glTF requires its embedded file in Assets"
        raise ValueError(msg)
    versions = declarations(doc, model, work)
    resources = ()
    scene = None
    if model == "glTF":
        from pyropdf._media import _MAX_ASSET  # noqa: PLC0415

        limit = _METADATA_BYTES if mime.lower() == "model/gltf+json" else _MAX_ASSET
        root, binary = _manifest(stream.read(max_size=limit), mime, work)
        scenes = root.get("scenes")
        if not isinstance(scenes, list) or not scenes or not all(isinstance(value, dict) for value in scenes):
            msg = "embedded glTF requires at least one scene"
            raise ValueError(msg)
        default = root.get("scene", 0)
        scene = instance.get("Scene", default)
        for index in (default, scene):
            if type(index) is not int or not 0 <= index < len(scenes):
                msg = "glTF scene index is outside the scenes array"
                raise ValueError(msg)
        resources = _gltf_resources(_resource_types(root, binary), files, work)
    return RichMediaModel(
        format=model,
        versions=versions,
        configuration=typed.RichMediaConfiguration(config),
        instance=typed.RichMediaInstance(instance),
        file=typed.FileSpecification(file),
        resources=resources,
        scene=scene,
    )
