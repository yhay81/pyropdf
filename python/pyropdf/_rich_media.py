"""Relationships between RichMedia settings, shared content and named assets."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from pyropdf import _core, _rich_models, typed
from pyropdf._errors import LimitError
from pyropdf._media import (
    _MAX_ITEMS,
    _MAX_TEXT,
    MediaAsset,
    MediaKind,
    _base_or_extension,
    _filespec,
    _require_media_version,
    _text,
)
from pyropdf._objects import Array, Dictionary, Name, Stream, String, _encode_text_string

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from pyropdf._annotations import Annotation
    from pyropdf._document import Document
    from pyropdf._media import RichMediaModel


def _dict(value: object, description: str) -> Dictionary:
    if not isinstance(value, Dictionary) or isinstance(value, Stream):
        msg = f"{description} must be a dictionary"
        raise TypeError(msg)
    return value


def _indirect(value: object, description: str) -> Dictionary:
    result = _dict(value, description)
    if not result.is_indirect:
        msg = f"{description} must be indirect"
        raise ValueError(msg)
    return result


class _Work:
    def __init__(self, budget: _core._OperationBudget) -> None:
        self.budget = budget
        self.count = 0

    def add(self, size: int, memory: int = 0) -> None:
        self.count += size
        if self.count > _MAX_ITEMS:
            msg = "RichMedia relationships exceed 4096 entries"
            code = "annotation_count"
            raise LimitError(code, msg)
        self.budget.charge("memory_size", memory)
        self.budget.poll()


def _array(value: object, work: _Work, description: str) -> Array:
    if not isinstance(value, Array):
        msg = f"{description} must be an array"
        raise TypeError(msg)
    work.add(len(value))
    return value


def _content(annot: Dictionary) -> Dictionary:
    if annot.get("Subtype") != "RichMedia":
        msg = "the annotation is not RichMedia"
        raise ValueError(msg)
    return _dict(annot.get("RichMediaContent"), "RichMediaContent")


@dataclass(slots=True)
class _Entry:
    key: String
    file: Dictionary
    leaf: Dictionary
    index: int


@dataclass(slots=True)
class _Tree:
    entries: dict[str, _Entry] = field(default_factory=dict)
    parents: dict[Dictionary, tuple[Dictionary, int]] = field(default_factory=dict)


def _decoded_name(key: String, work: _Work) -> str:
    if len(key) > _MAX_TEXT:
        msg = "a RichMedia name exceeds the annotation text limit"
        code = "annotation_input_size"
        raise LimitError(code, msg)
    work.add(0, 4 * len(key))
    return key.text


def _read_names(
    node: Dictionary, names: object, entries: dict[str, _Entry], previous: String | None, work: _Work
) -> String | None:
    if not isinstance(names, Array) or len(names) % 2:
        msg = "asset Names must contain name and file specification pairs"
        raise ValueError(msg)
    work.add(len(names) // 2, 512 * (len(names) // 2))
    for i in range(0, len(names), 2):
        key = names[i]
        if not isinstance(key, String):
            msg = "an asset name must be a text string"
            raise TypeError(msg)
        text = _decoded_name(key, work)
        if text in entries or (previous is not None and key <= previous):
            msg = "asset names must be unique and sorted by their stored bytes"
            raise ValueError(msg)
        entries[text] = _Entry(key, _indirect(names[i + 1], "named asset"), node, i)
        previous = key
    return previous


def _assets(content: Dictionary, work: _Work) -> _Tree:
    root = content.get("Assets")
    result = _Tree()
    if root is None:
        return result
    stack = [_dict(root, "asset name tree")]
    previous = None
    while stack:
        node = stack.pop()
        work.add(1, 256)
        kids = node.get("Kids")
        names = node.get("Names")
        if kids is not None:
            if names is not None:
                msg = "an asset name tree node cannot have both Kids and Names"
                raise ValueError(msg)
            children = _array(kids, work, "asset tree children")
            work.add(0, 256 * len(children))
            for i in reversed(range(len(children))):
                child = _indirect(children[i], "asset tree child")
                if child in result.parents or child == root:
                    msg = "an asset name tree contains a repeated node"
                    raise ValueError(msg)
                result.parents[child] = (node, i)
                stack.append(child)
        elif names is not None:
            previous = _read_names(node, names, result.entries, previous, work)
        if node != root and not (kids or names):
            msg = "an asset name tree child must be nonempty"
            raise ValueError(msg)
    return result


def assets(annotation: Annotation) -> Mapping[str, typed.FileSpecification]:
    annot = annotation.obj

    def read(budget: _core._OperationBudget) -> Mapping[str, typed.FileSpecification]:
        tree = _assets(_content(annot), _Work(budget))
        return MappingProxyType({key: typed.FileSpecification(entry.file) for key, entry in tree.entries.items()})

    return _core.read_operation(annot._core(), read)  # noqa: SLF001


def _update_limits(node: Dictionary, low: bytes, high: bytes) -> bool:
    current = node.get("Limits")
    if isinstance(current, Array) and len(current) == 2 and current[0] == low and current[1] == high:  # noqa: PLR2004
        return False
    node["Limits"] = [low, high]
    return True


def _write_node(node: Dictionary, key: str, items: list, *, root: bool) -> None:
    node[key] = items
    other = "Kids" if key == "Names" else "Names"
    if other in node:
        del node[other]
    if root:
        if "Limits" in node:
            del node["Limits"]
    elif key == "Names":
        _update_limits(node, items[0], items[-2])
    else:
        _update_limits(node, items[0]["Limits"][0], items[-1]["Limits"][1])


def _refresh_limits(tree: _Tree, node: Dictionary, work: _Work) -> None:
    while (parent := tree.parents.get(node)) is not None:
        node, _ = parent
        if node not in tree.parents:
            break
        work.budget.poll()
        kids = node["Kids"]
        if not _update_limits(node, kids[0]["Limits"][0], kids[-1]["Limits"][1]):
            break


def _write_path(tree: _Tree, node: Dictionary, names: list, work: _Work) -> None:
    # Split only the edited path, preserving the existing nodes' private keys.
    key, items, fanout = "Names", names, 128
    doc = node._document  # noqa: SLF001
    while True:
        work.add(0, 96 * len(items))
        parent = tree.parents.get(node)
        if not items and parent is None:
            _write_node(node, "Names", [], root=True)
            return
        siblings = []
        if items and len(items) <= fanout:
            _write_node(node, key, items, root=parent is None)
            # The references in every ancestor still point to the same children.
            _refresh_limits(tree, node, work)
            return
        if items:
            # Keep the old root as root. Below it, reuse the original left node.
            groups = (len(items) + fanout - 1) // fanout
            step = (len(items) + groups - 1) // groups
            if key == "Names":
                step += step % 2  # Keep each name with its file specification.
            for start in range(0, len(items), step):
                child = node if start == 0 and parent is not None else doc.objects[doc.objects.add({})]
                _write_node(child, key, items[start : start + step], root=False)
                siblings.append(child)
        if parent is None:
            key, items, fanout = "Kids", siblings, 64
            continue
        node, position = parent
        kids = node["Kids"]
        work.add(0, 192 * len(kids))
        items = list(kids)
        items[position : position + 1] = siblings
        key, fanout = "Kids", 64


def _asset_file(doc: Document, asset: MediaAsset | typed.FileSpecification) -> Dictionary:
    if isinstance(asset, MediaAsset):
        return _filespec(doc, asset)
    if not isinstance(asset, typed.FileSpecification):
        msg = "a named asset must be a MediaAsset or an indirect file specification"
        raise TypeError(msg)
    file = _indirect(asset.obj, "named asset")
    if file._document is not doc:  # noqa: SLF001
        msg = "a named asset must belong to the same document"
        raise ValueError(msg)
    if file.get("Type", "Filespec") != "Filespec":
        msg = "a named asset must be a file specification dictionary"
        raise ValueError(msg)
    return file


def add_configuration(
    annotation: Annotation, name: str, kind: MediaKind, assets: Sequence[typed.FileSpecification]
) -> typed.RichMediaConfiguration:
    _text(name)
    kind = MediaKind(kind)
    with _operation(annotation) as (annot, work):
        doc = annot._document  # noqa: SLF001
        instances = []
        for asset in assets:
            work.add(1, 256)
            if not isinstance(asset, typed.FileSpecification):
                msg = "configuration assets must be existing indirect file specifications"
                raise TypeError(msg)
            file = _asset_file(doc, asset)
            instance = doc.objects.add({"Type": Name("RichMediaInstance"), "Subtype": Name(kind), "Asset": file})
            instances.append(doc.objects[instance])
        config = doc.objects[
            doc.objects.add(
                {"Type": Name("RichMediaConfiguration"), "Subtype": Name(kind), "Name": name, "Instances": instances}
            )
        ]
        _content(annot)["Configurations"].append(config)
        return typed.RichMediaConfiguration(config)


def set_asset(annotation: Annotation, name: str, asset: MediaAsset | typed.FileSpecification | None) -> None:
    _text(name)
    with _operation(annotation, protect_models=True) as (annot, work):
        content = _content(annot)
        tree = _assets(content, work)
        entry = tree.entries.get(name)
        if entry is None and asset is None:
            raise KeyError(name)
        doc = annot._document  # noqa: SLF001
        file = None if asset is None else _asset_file(doc, asset)
        if entry is not None:
            if file is not None:
                entry.leaf["Names"][entry.index + 1] = file
            else:
                source = entry.leaf["Names"]
                work.add(0, len(source) * 192)
                names = list(source)
                del names[entry.index : entry.index + 2]
                _write_path(tree, entry.leaf, names, work)
        else:
            raw = _encode_text_string(name)
            if content.get("Assets") is None:
                content["Assets"] = {"Names": []}
            leaf = content["Assets"]
            position = 0
            for candidate in tree.entries.values():
                leaf, position = candidate.leaf, candidate.index
                if candidate.key > raw:
                    break
                position += 2
            source = leaf.get("Names", ())
            work.add(0, len(source) * 192)
            names = list(source)
            names[position:position] = [raw, file]
            _write_path(tree, leaf, names, work)


def _references(raw: object, work: _Work, label: str) -> set[Dictionary]:
    array = _array(raw, work, label)
    work.add(0, len(array) * 192)
    return {_indirect(element, label) for element in array}


def _settings(
    owner: Dictionary, configs: set[Dictionary], views: set[Dictionary], files: set[Dictionary], work: _Work
) -> None:
    settings = owner.get("RichMediaSettings")
    if settings is None:
        return
    activation = _dict(settings, "RichMediaSettings").get("Activation")
    if activation is None:
        return
    activation = _dict(activation, "RichMediaActivation")
    for key, targets in (("Configuration", configs), ("View", views)):
        target = activation.get(key)
        if target is not None and _indirect(target, key) not in targets:
            msg = f"activation {key} does not belong to the shared RichMedia content"
            raise ValueError(msg)
    scripts = activation.get("Scripts")
    if scripts is not None and not _references(scripts, work, "activation script").issubset(files):
        msg = "an activation script must remain in the RichMedia Assets name tree"
        raise ValueError(msg)


def initial_target(annotation: Annotation, target: str, collection: str) -> Dictionary | None:
    annot = annotation.obj

    def read(budget: _core._OperationBudget) -> Dictionary | None:
        content = _content(annot)
        raw = content.get(collection)
        choices = None if raw is None else _array(raw, _Work(budget), collection)
        settings = annot.get("RichMediaSettings")
        activation = None if settings is None else _dict(settings, "RichMediaSettings").get("Activation")
        chosen = None if activation is None else _dict(activation, "RichMediaActivation").get(target)
        if chosen is not None:
            selected = _indirect(chosen, target)
            if choices is None or selected not in choices:
                msg = f"activation {target} does not belong to the shared RichMedia content"
                raise ValueError(msg)
            return selected
        if choices:
            return _indirect(choices[0], target)
        return None

    return _core.read_operation(annot._core(), read)  # noqa: SLF001


def _annotations_sharing(annot: Dictionary, content: Dictionary, work: _Work) -> Generator[Dictionary, None, None]:
    settings = annot.get("RichMediaSettings")
    unique = isinstance(settings, Dictionary) and settings.is_indirect
    if content.is_indirect or unique:
        for page in annot._document.pages:  # noqa: SLF001
            work.add(1)
            group = page.obj.get("Annots")
            if group is None:
                continue
            for other in _array(group, work, "page annotations"):
                if not isinstance(other, Dictionary) or other == annot or other.get("Subtype") != "RichMedia":
                    continue
                if unique and other.get("RichMediaSettings") == settings:
                    msg = "RichMediaSettings must be unique to each annotation"
                    raise ValueError(msg)
                if other.get("RichMediaContent") == content:
                    yield other


def _instance_set(configs: set[Dictionary], work: _Work) -> set[Dictionary]:
    instances: set[Dictionary] = set()
    for config in configs:
        raw = config.get("Instances")
        if raw is not None:
            group = _references(raw, work, "RichMedia instance")
            work.add(0, 128 * len(group))
            instances.update(group)
    return instances


@dataclass(slots=True)
class _Views:
    ordered: list[Dictionary] = field(default_factory=list)
    named: dict[str, Dictionary] = field(default_factory=dict)

    def resolve(self, selector: object) -> Dictionary | None:
        if type(selector) is int and 0 <= selector < len(self.ordered):
            return self.ordered[selector]
        return self.named.get(selector) if type(selector) is str else None


def _view_choices(content: Dictionary, work: _Work) -> _Views:
    choices = _Views()
    raw = content.get("Views")
    if raw is not None:
        for entry in _array(raw, work, "RichMedia views"):
            view = _indirect(entry, "RichMedia view")
            work.add(0, 256)
            choices.ordered.append(view)
            name = view.get("IN", view.get("XN"))
            if isinstance(name, String):
                choices.named.setdefault(_decoded_name(name, work), view)
    return choices


def _check_action_targets(
    annot: Dictionary, removed: set[Dictionary], views: tuple[_Views, _Views], work: _Work
) -> None:
    """Inspect reachable actions when an edit changes which view or instance a command would address."""
    content = _content(annot)
    stack: list[Dictionary | Array] = [annot._document.catalog]  # noqa: SLF001
    seen: set[Dictionary | Array] = set()
    while stack:
        obj = stack.pop()
        if obj in seen:
            continue
        work.add(1, 256)
        seen.add(obj)
        if isinstance(obj, Dictionary) and obj.get("S") in ("RichMediaExecute", "GoTo3DView"):
            target = obj.get("TA")
            if isinstance(target, Dictionary) and target.get("RichMediaContent") == content:
                _check_action(obj, removed, views, work)
        work.add(len(obj), 128 * len(obj))
        children = obj.values() if isinstance(obj, Dictionary) else obj
        stack.extend(item for item in children if isinstance(item, (Dictionary, Array)))


def _check_action(action: Dictionary, removed: set[Dictionary], views: tuple[_Views, _Views], work: _Work) -> None:
    if action.get("S") == "RichMediaExecute":
        if action.get("TI") in removed:
            msg = "a RichMediaExecute action still targets the removed instance"
            raise ValueError(msg)
    else:
        selector = action.get("V")
        if isinstance(selector, String):
            selector = _decoded_name(selector, work)
        previous, current = views
        if previous.resolve(selector) is not None and current.resolve(selector) is None:
            msg = "a GoTo3DView action still targets the removed view"
            raise ValueError(msg)


def _validate_instances(annot: Dictionary, instances: set[Dictionary], files: set[Dictionary]) -> None:
    from pyropdf._annotations import _version  # noqa: PLC0415

    doc = annot._document  # noqa: SLF001
    legacy = _version(doc) == (1, 7) and _base_or_extension(doc, 3) == (1, 7)
    for instance in instances:
        file = _indirect(instance.get("Asset"), "instance asset")
        if legacy and file not in files:
            msg = "a PDF 1.7 RichMedia instance asset must remain named"
            raise ValueError(msg)
        if not legacy and instance.get("Subtype") == "Flash":
            msg = "a Flash instance requires PDF 1.7 with ADBE extension level 3"
            raise ValueError(msg)
        if "Params" in instance:
            if not legacy or instance.get("Subtype") != "Flash":
                msg = "instance Params require a Flash instance in PDF 1.7 with ADBE extension level 3"
                raise ValueError(msg)
            params = _dict(instance["Params"], "RichMediaParams")
            if params.get("Binding") == "Material" and not isinstance(params.get("BindingMaterial"), String):
                msg = "a Material binding requires BindingMaterial"
                raise ValueError(msg)


def _models(doc: Document, configs: Array, files: set[Dictionary], work: _Work) -> tuple[RichMediaModel, ...]:
    result = []
    for raw in configs:
        config = _indirect(raw, "RichMedia configuration")
        if "Instances" not in config:
            continue
        for instance in _array(config["Instances"], work, "RichMedia instances"):
            model = _rich_models.inspect_model(doc, config, _indirect(instance, "RichMedia instance"), files, work)
            if model is not None:
                if config.get("Subtype", Name("3D")) != "3D":
                    msg = "a RichMedia model requires a 3D configuration"
                    raise ValueError(msg)
                result.append(model)
    return tuple(result)


def models_for(annot: Dictionary, doc: Document, work: _Work) -> tuple[RichMediaModel, ...]:
    content = _content(annot)
    files = {entry.file for entry in _assets(content, work).entries.values()}
    configs = _array(content.get("Configurations"), work, "RichMedia configurations")
    return _models(doc, configs, files, work)


def models(annotation: Annotation) -> tuple[RichMediaModel, ...]:
    annot = annotation.obj

    def read(budget: _core._OperationBudget) -> tuple[RichMediaModel, ...]:
        return models_for(annot, annot._document, _Work(budget))  # noqa: SLF001

    return _core.read_operation(annot._core(), read)  # noqa: SLF001


def initialize_model(doc: Document, annot: Dictionary, model: str, version: str) -> None:
    def initialize(budget: _core._OperationBudget) -> None:
        work = _Work(budget)
        _rich_models.declarations(doc, model, work, add_version=version)
        root = _content(annot)["Assets"]
        names = root["Names"]
        if len(names) > 128:  # noqa: PLR2004 - 64 name/file pairs per leaf.
            work.add(0, 192 * len(names))
            _write_path(_Tree(), root, list(names), work)
        _validate(annot, work)

    _core.read_operation(annot._core(), initialize)  # noqa: SLF001


def _model_files(instances: set[Dictionary]) -> dict[Dictionary, Dictionary]:
    result = {}
    for instance in instances:
        file = instance.get("Asset")
        if isinstance(file, Dictionary):
            stream = _rich_models.file_stream(file)
            if stream is not None and _rich_models.format_for(stream.get("Subtype")):
                result[instance] = file
    return result


def _check_retained_model_types(original: dict[Dictionary, Dictionary], retained: set[Dictionary]) -> None:
    current = _model_files(retained)
    for instance, file in original.items():
        if instance in retained and instance.get("Asset") == file and instance not in current:
            msg = "the shared model must retain its registered embedded MIME type"
            raise ValueError(msg)


def _file_objects(files: set[Dictionary], work: _Work) -> set[Dictionary]:
    work.add(0, 256 * len(files))
    return files | {stream for file in files if (stream := _rich_models.file_stream(file)) is not None}


def _other_model_contents(annot: Dictionary, work: _Work) -> Generator[Dictionary, None, None]:
    current = _content(annot)
    for page in annot._document.pages:  # noqa: SLF001
        work.add(1)
        group = page.obj.get("Annots")
        if not isinstance(group, Array):
            continue
        for other in _array(group, work, "page annotations"):
            if isinstance(other, Dictionary) and other.get("Subtype") == "RichMedia" and other != annot:
                content = other.get("RichMediaContent")
                if isinstance(content, Dictionary) and content != current:
                    yield content


def _check_shared_model_files(annot: Dictionary, affected: set[Dictionary], work: _Work) -> None:
    # Distinct RichMediaContent dictionaries can share a file specification or just its stream.
    for content in _other_model_contents(annot, work):
        raw = content.get("Configurations")
        if not isinstance(raw, Array):
            continue
        candidates = set()
        work.add(len(raw), 128 * len(raw))
        for config in raw:
            group = config.get("Instances") if isinstance(config, Dictionary) else None
            if isinstance(group, Array):
                work.add(len(group), 128 * len(group))
                candidates.update(inst for inst in group if isinstance(inst, Dictionary))
        primary = set(_model_files(candidates).values())
        if not primary:
            continue
        primary_shared = bool(affected & _file_objects(primary, work))
        try:
            files = {entry.file for entry in _assets(content, work).entries.values()}
        except (TypeError, ValueError):
            if primary_shared:
                raise
            # An independently malformed name tree cannot resolve any ancillary resource.
            continue
        if primary_shared or affected & _file_objects(files, work):
            _models(annot._document, raw, files, work)  # noqa: SLF001


def _validate(annot: Dictionary, work: _Work) -> set[Dictionary]:
    content = _content(annot)
    named = _assets(content, work).entries
    work.add(0, 128 * len(named))
    files = {entry.file for entry in named.values()}
    configs = _references(content.get("Configurations"), work, "RichMedia configuration")
    if not configs:
        msg = "RichMedia requires a nonempty Configurations array"
        raise ValueError(msg)
    raw_views = content.get("Views")
    views = set() if raw_views is None else _references(raw_views, work, "RichMedia view")
    instances = _instance_set(configs, work)
    _validate_instances(annot, instances, files)
    _models(annot._document, content["Configurations"], files, work)  # noqa: SLF001
    _settings(annot, configs, views, files, work)
    for other in _annotations_sharing(annot, content, work):
        _settings(other, configs, views, files, work)
    return instances


@contextmanager
def _operation(
    annotation: Annotation, *, protect_actions: bool = False, protect_models: bool = False
) -> Generator[tuple[Dictionary, _Work], None, None]:
    core = annotation.page._document._core()  # noqa: SLF001
    budget = _core.begin_operation(core, "edit RichMedia")
    try:
        annot = annotation.obj
        content = _content(annot)
        doc = annot._document  # noqa: SLF001
        _require_media_version(doc, _base_or_extension(doc, 3))
        work = _Work(budget)
        before = set()
        original_views = _Views()
        original_models: dict[Dictionary, Dictionary] = {}
        affected: set[Dictionary] = set()
        if protect_actions or protect_models:
            configs = _references(content.get("Configurations"), work, "RichMedia configuration")
            before = _instance_set(configs, work)
            if protect_actions:
                original_views = _view_choices(content, work)
            original_models = _model_files(before)
            if original_models:
                linked = set(original_models.values()) | {e.file for e in _assets(content, work).entries.values()}
                affected = _file_objects(linked, work)
        yield annot, work
        retained = _validate(annot, _Work(budget))
        if original_models:
            _check_retained_model_types(original_models, retained)
            _check_shared_model_files(annot, affected, work)
        if protect_actions:
            work.add(0, 128 * len(before))
            removed = before - retained
            current_views = _view_choices(_content(annot), _Work(budget))
            if removed or current_views != original_views:
                _check_action_targets(annot, removed, (original_views, current_views), _Work(budget))
    except BaseException:
        _core.abort_canvas(core, budget)
        raise
    _core.end_transaction(core, True)  # noqa: FBT003


@contextmanager
def edit(annotation: Annotation) -> Generator[typed.AnnotRichMedia, None, None]:
    with _operation(annotation, protect_actions=True) as (annot, _):
        yield typed.AnnotRichMedia(annot)
