"""Optional content: ``doc.layers`` lists the groups and configurations and sets their visibility (ISO 32000-2 8.11).

hayro renders and extracts text under the default configuration, the
catalog's ``/OCProperties /D``: its ``/BaseState``, ``/ON``, and ``/OFF``
groups, and the ``/P`` policy of membership dictionaries. Switching a group
or choosing a configuration therefore rewrites ``/D``, and the next
rendering and text extraction follow it. :meth:`Layers.visible` evaluates
a group or membership dictionary, ``/VE`` visibility expressions included.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, overload

from pyropdf._errors import LimitError
from pyropdf._objects import Array, Dictionary, Name, String

if TYPE_CHECKING:
    from pyropdf._document import Document

#: Levels of a ``/VE`` visibility expression that an evaluation follows.
_MAX_EXPRESSION_DEPTH = 32
#: Operands of one visibility expression, over all its levels.
_MAX_EXPRESSION_OPERANDS = 4096
#: Groups and configurations that one listing reads.
_MAX_LAYERS = 4096
_COUNT = "optional_content_count"
_EXPRESSION = "optional_content_expression"


@dataclass(frozen=True, slots=True)
class Layer:
    """An optional content group (``/Type /OCG``) and its state in the default configuration."""

    xref: int
    name: str
    intent: tuple[str, ...]
    """The group's ``/Intent`` names, ``("View",)`` by default."""
    on: bool
    """Whether the default configuration shows the group."""
    locked: bool
    """Whether the default configuration's ``/Locked`` asks viewers not to change it."""


@dataclass(frozen=True, slots=True)
class LayerConfig:
    """An optional content configuration: the default ``/D`` at index 0, then each of ``/Configs``."""

    index: int
    name: str
    creator: str
    base_state: str
    """``ON``, ``OFF``, or ``Unchanged``, the state of the groups that ``on`` and ``off`` leave out."""
    on: tuple[int, ...]
    off: tuple[int, ...]
    locked: tuple[int, ...]


def _text(value: object) -> str:
    return value.text if isinstance(value, String) else ""


def _xrefs(value: object) -> tuple[int, ...]:
    """Return the object numbers of the indirect dictionaries of array ``value``."""
    if not isinstance(value, Array):
        return ()
    if len(value) > _MAX_LAYERS:
        msg = f"an optional content array has more than {_MAX_LAYERS} entries"
        raise LimitError(_COUNT, msg)
    return tuple(item.xref for item in value if isinstance(item, Dictionary) and item.xref is not None)


def _intents(value: object) -> tuple[str, ...]:
    if isinstance(value, Name):
        return (str(value),)
    if isinstance(value, Array):
        return tuple(str(name) for name in value if isinstance(name, Name))
    return ("View",)


def _config(index: int, config: Dictionary) -> LayerConfig:
    base = config.get("BaseState")
    return LayerConfig(
        index,
        _text(config.get("Name")),
        _text(config.get("Creator")),
        str(base) if isinstance(base, Name) and base in ("ON", "OFF", "Unchanged") else "ON",
        _xrefs(config.get("ON")),
        _xrefs(config.get("OFF")),
        _xrefs(config.get("Locked")),
    )


class Layers(Sequence[Layer]):
    """The optional content groups of a document, in ``/OCGs`` order; empty without optional content."""

    __slots__ = ("_document",)

    def __init__(self, document: Document) -> None:
        self._document = document

    def _properties(self) -> Dictionary | None:
        properties = self._document.catalog.get("OCProperties")
        return properties if isinstance(properties, Dictionary) else None

    def _groups(self) -> list[Dictionary]:
        properties = self._properties()
        groups = properties.get("OCGs") if properties is not None else None
        if not isinstance(groups, Array):
            return []
        if len(groups) > _MAX_LAYERS:
            msg = f"the document has more than {_MAX_LAYERS} optional content groups"
            raise LimitError(_COUNT, msg)
        return [group for group in groups if isinstance(group, Dictionary) and group.xref is not None]

    def _default(self) -> Dictionary | None:
        properties = self._properties()
        config = properties.get("D") if properties is not None else None
        return config if isinstance(config, Dictionary) else None

    def _off(self) -> set[int]:
        """Return the groups that the default configuration hides."""
        config = self._default()
        if config is None:
            return set()
        state = _config(0, config)
        off = {group.xref for group in self._groups()} if state.base_state == "OFF" else set()
        off -= set(state.on)
        off |= set(state.off)
        return {xref for xref in off if xref is not None}

    def _layers(self) -> list[Layer]:
        off = self._off()
        config = self._default()
        locked = set(_config(0, config).locked) if config is not None else set()
        layers = []
        for group in self._groups():
            xref = group.xref
            assert xref is not None  # noqa: S101 - _groups keeps indirect groups only
            layers.append(
                Layer(xref, _text(group.get("Name")), _intents(group.get("Intent")), xref not in off, xref in locked)
            )
        return layers

    def __len__(self) -> int:
        """Return the number of optional content groups."""
        return len(self._groups())

    @overload
    def __getitem__(self, index: int) -> Layer: ...
    @overload
    def __getitem__(self, index: slice) -> list[Layer]: ...
    def __getitem__(self, index: int | slice) -> Layer | list[Layer]:
        """Return the group at ``index``, or a list for a slice."""
        return self._layers()[index]

    def __iter__(self) -> Iterator[Layer]:
        """Iterate over the groups, read once."""
        return iter(self._layers())

    @property
    def configs(self) -> list[LayerConfig]:
        """Return the default configuration, then the alternate ones of ``/Configs``."""
        properties = self._properties()
        if properties is None:
            return []
        configs = []
        default = properties.get("D")
        if isinstance(default, Dictionary):
            configs.append(_config(0, default))
        alternates = properties.get("Configs")
        if isinstance(alternates, Array):
            if len(alternates) > _MAX_LAYERS:
                msg = f"the document has more than {_MAX_LAYERS} optional content configurations"
                raise LimitError(_COUNT, msg)
            configs += [_config(at + 1, item) for at, item in enumerate(alternates) if isinstance(item, Dictionary)]
        return configs

    def _require(self) -> tuple[Dictionary, Dictionary]:
        properties = self._properties()
        if properties is None:
            msg = "the document has no optional content"
            raise ValueError(msg)
        config = properties.get("D")
        if not isinstance(config, Dictionary):
            properties["D"] = {}
            config = properties["D"]
        return properties, config

    def switch(self, xref: int, *, on: bool) -> None:
        """Show or hide group ``xref`` in the default configuration, in one transaction."""
        if isinstance(xref, bool) or not isinstance(xref, int):
            msg = f"xref must be an int: {xref!r}"
            raise TypeError(msg)
        if not isinstance(on, bool):
            msg = f"on must be a bool: {on!r}"
            raise TypeError(msg)
        if xref not in {group.xref for group in self._groups()}:
            msg = f"object {xref} is not an optional content group of the document"
            raise ValueError(msg)
        document = self._document
        with document.transaction("set layer"):
            _, config = self._require()
            state = _config(0, config)
            keep_on = [number for number in state.on if number != xref]
            keep_off = [number for number in state.off if number != xref]
            hidden_by_base = state.base_state == "OFF"
            if on and hidden_by_base:
                keep_on.append(xref)
            elif not on and not hidden_by_base:
                keep_off.append(xref)
            config["ON"] = [document.objects[number] for number in keep_on]
            config["OFF"] = [document.objects[number] for number in keep_off]

    def use(self, index: int) -> None:
        """Make configuration ``index`` of :attr:`configs` the default, in one transaction.

        The configuration's ``/BaseState``, ``/ON``, ``/OFF``, and
        ``/Locked`` replace those of ``/D``; index 0 is ``/D`` itself.
        """
        configs = self.configs
        if isinstance(index, bool) or not isinstance(index, int):
            msg = f"index must be an int: {index!r}"
            raise TypeError(msg)
        if not 0 <= index < len(configs):
            msg = f"no optional content configuration {index}: the document has {len(configs)}"
            raise IndexError(msg)
        if index == 0:
            return
        chosen = configs[index]
        document = self._document
        with document.transaction("use layer configuration"):
            _, config = self._require()
            config["BaseState"] = Name("OFF" if chosen.base_state == "OFF" else "ON")
            for key, numbers in (("ON", chosen.on), ("OFF", chosen.off), ("Locked", chosen.locked)):
                config[key] = [document.objects[number] for number in numbers]

    def visible(self, oc: Dictionary | None) -> bool:
        """Return whether content marked with ``oc``, a group or membership dictionary, shows by default.

        A membership dictionary's ``/VE`` visibility expression takes
        precedence over its ``/OCGs`` and ``/P``. An expression deeper than
        32 levels or with more than 4,096 operands raises
        :class:`LimitError` (``optional_content_expression``). ``None``, or
        a dictionary of another type, is visible.
        """
        if oc is None:
            return True
        if not isinstance(oc, Dictionary):
            msg = f"oc must be a Dictionary or None: {type(oc).__name__}"
            raise TypeError(msg)
        off = self._off()
        if oc.get("Type") != "OCMD":
            return oc.get("Type") != "OCG" or oc.xref not in off
        expression = oc.get("VE")
        if isinstance(expression, Array):
            return _evaluate(expression, off)
        groups = oc.get("OCGs")
        members = [groups] if isinstance(groups, Dictionary) else list(groups) if isinstance(groups, Array) else []
        states = [member.xref not in off for member in members if isinstance(member, Dictionary)]
        return _policy(oc.get("P"), states)


def _policy(policy: object, states: list[bool]) -> bool:
    """Apply a membership dictionary's ``/P`` policy, ``AnyOn`` by default, to its groups' states."""
    if not states:
        return True
    if policy == "AllOn":
        return all(states)
    if policy == "AnyOff":
        return not all(states)
    if policy == "AllOff":
        return not any(states)
    return any(states)


def _combine(operator: str, values: list[bool]) -> bool:
    """Apply a visibility expression's operator to its operands' values; an empty expression is visible."""
    if operator == "Not":
        return not values[0] if values else True
    if operator == "Or":
        return any(values) if values else True
    return all(values)


def _evaluate(expression: Array, off: set[int]) -> bool:
    """Evaluate visibility expression ``expression`` with an explicit stack (ISO 32000-2 8.11.2.2)."""
    operands = 0
    # Each frame is an expression, its operator, its operand values so far, and the next operand.
    stack: list[tuple[Array, str, list[bool], int]] = []

    def frame(array: Array) -> tuple[Array, str, list[bool], int]:
        if len(stack) == _MAX_EXPRESSION_DEPTH:
            msg = f"a visibility expression is deeper than {_MAX_EXPRESSION_DEPTH} levels"
            raise LimitError(_EXPRESSION, msg)
        operator = array[0] if len(array) else None
        return (array, str(operator) if isinstance(operator, Name) else "", [], 1)

    stack.append(frame(expression))
    while True:
        array, operator, values, at = stack[-1]
        if at < len(array):
            operands += 1
            if operands > _MAX_EXPRESSION_OPERANDS:
                msg = f"a visibility expression has more than {_MAX_EXPRESSION_OPERANDS} operands"
                raise LimitError(_EXPRESSION, msg)
            stack[-1] = (array, operator, values, at + 1)
            operand = array[at]
            if isinstance(operand, Array):
                stack.append(frame(operand))
            elif isinstance(operand, Dictionary):
                values.append(operand.xref not in off)
            continue
        stack.pop()
        result = _combine(operator, values)
        if not stack:
            return result
        stack[-1][2].append(result)


# The package re-exports these names; reprs, pickles, and documentation name them.
for _public in (Layer, LayerConfig, Layers):
    _public.__module__ = "pyropdf"
