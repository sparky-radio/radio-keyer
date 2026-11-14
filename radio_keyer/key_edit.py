"""Read and rewrite one key's bindings in config.yaml text, keeping comments.

Used by the web UI's per-key form. ruamel.yaml's round-trip mode changes only
the edited key; the rest of the file (comments, order, quoting) is kept.
The result is plain text: the caller validates it with `parse_config_text`
before saving, so nothing here needs to know the action rules.
"""

from __future__ import annotations

import difflib
import io
from collections.abc import Mapping, Sequence
from typing import Final

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from radio_keyer.config import GLOBAL_PROFILE, NUM_KEYS, ConfigError
from radio_keyer.keypad import PressKind

type Scalar = str | int | float | bool | None
type ActionDict = dict[str, Scalar]
type PressActions = list[ActionDict]
type KeyActions = dict[str, PressActions | None]
"""{"short": [...] | None, "long": [...] | None}"""

_QUOTED: Final[frozenset[str]] = frozenset({"cw", "cat", "cat_hex"})
"""Action values written as "double quoted" text."""
_KINDS: Final[tuple[str, ...]] = tuple(k.value for k in PressKind)


class KeyEditError(ConfigError):
    pass


class UnknownProfileError(KeyEditError):
    pass


def _yaml() -> YAML:
    y = YAML()  # round-trip
    y.preserve_quotes = True
    y.width = 4096  # never wrap long flow lines
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _load(text: str) -> CommentedMap:
    try:
        data: object = _yaml().load(text)
    except YAMLError as exc:
        raise KeyEditError(f"invalid YAML: {exc}") from None
    if not isinstance(data, CommentedMap):
        raise KeyEditError("config is not a mapping")
    return data


def _dump(data: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def _splice(original: str, data: CommentedMap, edited: CommentedMap) -> str:
    """Apply only the lines the edit changed to `original`.

    ruamel normalizes some untouched formatting (e.g. `cw:   {...}` alignment).
    Diffing its dump of the unedited file against the edited dump finds the
    edit's lines; when the unedited dump lines up with the original, those
    lines are patched into the original so everything else stays byte-exact.
    """
    new = _dump(edited)
    base = _dump(data).splitlines(keepends=True)
    orig = original.splitlines(keepends=True)
    if len(base) != len(orig):
        return new
    out = list(orig)
    new_lines = new.splitlines(keepends=True)
    ops = difflib.SequenceMatcher(None, base, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in reversed(ops.get_opcodes()):
        if tag != "equal":
            out[i1:i2] = new_lines[j1:j2]
    return "".join(out)


def _check_position(position: int) -> None:
    if not 1 <= position <= NUM_KEYS:
        raise KeyEditError(f"key position must be 1-{NUM_KEYS}")


def _profile_node(data: CommentedMap, profile: str, *, create: bool) -> CommentedMap | None:
    """The profile's mapping (`global` or `profiles.<name>`); None if absent and not created."""
    if profile == GLOBAL_PROFILE:
        parent, name = data, GLOBAL_PROFILE
        if parent.get(name) is None and not create:
            return None
    else:
        profiles = data.get("profiles")
        if not isinstance(profiles, CommentedMap) or profile not in profiles:
            raise UnknownProfileError(f"unknown profile '{profile}'")
        parent, name = profiles, profile

    node = parent.get(name)
    if node is None:
        node = CommentedMap()
        if name in parent:
            parent[name] = node
        else:  # new `global:` goes just before `profiles:`
            idx = list(parent).index("profiles") if "profiles" in parent else len(parent)
            parent.insert(idx, name, node)
    if not isinstance(node, CommentedMap):
        raise KeyEditError(f"profile '{profile}' is not a mapping")
    return node


def _key_lookup(keys: Mapping[object, object], position: int) -> object | None:
    """Keys may be written `1:` (int) or `'1':` (str)."""
    for k in (position, str(position)):
        if k in keys:
            return k
    return None


# --- read -----------------------------------------------------------------


def _plain(value: object) -> Scalar:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    raise KeyEditError(f"unsupported value {value!r} in action")


def _actions(raw: object) -> PressActions:
    items: Sequence[object] = raw if isinstance(raw, list) else [raw]
    out: PressActions = []
    for item in items:
        if not isinstance(item, Mapping):
            raise KeyEditError(f"action {item!r} is not a mapping")
        out.append({str(k): _plain(v) for k, v in item.items()})
    return out


def read_key(text: str, profile: str, position: int) -> KeyActions:
    """The key's own short/long actions in `profile` (None = not set there)."""
    _check_position(position)
    data = _load(text)
    result: KeyActions = {kind: None for kind in _KINDS}
    node = _profile_node(data, profile, create=False)
    keys = node.get("keys") if node is not None else None
    if not isinstance(keys, Mapping):
        return result
    k = _key_lookup(keys, position)
    entry = keys.get(k) if k is not None else None
    if entry is None:
        return result
    if isinstance(entry, Mapping) and entry and all(str(x) in _KINDS for x in entry):
        for kind in _KINDS:
            if entry.get(kind) is not None:
                result[kind] = _actions(entry[kind])
    else:
        result["short"] = _actions(entry)
    return result


# --- write ----------------------------------------------------------------


def _action_node(action: Mapping[str, Scalar]) -> CommentedMap:
    if not action:
        raise KeyEditError("empty action")
    node = CommentedMap()
    for k, v in action.items():
        node[k] = DoubleQuotedScalarString(v) if k in _QUOTED and isinstance(v, str) else v
    node.fa.set_flow_style()
    return node


def _press_node(actions: Sequence[Mapping[str, Scalar]]) -> CommentedMap | CommentedSeq:
    if len(actions) == 1:
        return _action_node(actions[0])
    seq = CommentedSeq(_action_node(a) for a in actions)
    seq.fa.set_block_style()
    return seq


def _entry_node(short: PressActions | None, long: PressActions | None) -> object | None:
    if not short and not long:
        return None
    if short and not long and len(short) == 1:
        return _action_node(short[0])  # `1: {cw: "..."}`
    entry = CommentedMap()
    for kind, actions in (("short", short), ("long", long)):
        if actions:
            entry[kind] = _press_node(actions)
    entry.fa.set_block_style()
    return entry


def _move_trailing_comment(old: object, new: object) -> None:
    """Keep a comment/blank line that ruamel attached after the old value.

    ruamel stores comments that follow a node (e.g. the comment above the next
    profile) on the deepest last item of that node. Without this, replacing a
    key's value would delete them.
    """
    token = _pop_trailing(old)
    if token is not None:
        _attach_trailing(new, token)


def _last_container(node: object) -> tuple[CommentedMap | CommentedSeq, object] | None:
    """Deepest (container, last key/index) along the last-item path."""
    found: tuple[CommentedMap | CommentedSeq, object] | None = None
    while True:
        if isinstance(node, CommentedMap) and node:
            last: object = list(node)[-1]
            found = (node, last)
            node = node[last]
        elif isinstance(node, CommentedSeq) and node:
            found = (node, len(node) - 1)
            node = node[-1]
        else:
            return found


def _pop_trailing(node: object) -> object | None:
    spot = _last_container(node)
    if spot is None:
        return None
    container, key = spot
    slot = 2 if isinstance(container, CommentedMap) else 0
    items = container.ca.items.get(key)
    if not items or len(items) <= slot or items[slot] is None:
        return None
    token = items[slot]
    items[slot] = None
    return token


def _attach_trailing(node: object, token: object) -> None:
    spot = _last_container(node)
    if spot is None:
        return
    container, key = spot
    slot, size = (2, 4) if isinstance(container, CommentedMap) else (0, 2)
    items = container.ca.items.setdefault(key, [None] * size)
    items[slot] = token


def write_key(
    text: str,
    profile: str,
    position: int,
    short: PressActions | None,
    long: PressActions | None,
) -> str:
    """Return `text` with the key's bindings in `profile` replaced (both empty = remove)."""
    _check_position(position)
    if read_key(text, profile, position) == {"short": short or None, "long": long or None}:
        return text  # unchanged: keep the key's own formatting too
    data = _load(text)
    edited = _load(text)
    _edit(edited, profile, position, _entry_node(short, long))
    return _splice(text, data, edited)


def _edit(data: CommentedMap, profile: str, position: int, new: object | None) -> None:
    """Replace, insert or (new None) remove the key entry in `data`, in place."""
    node = _profile_node(data, profile, create=new is not None)
    if node is None:  # removing from a global profile that doesn't exist
        return

    keys = node.get("keys")
    if keys is None:
        if new is None:
            return
        keys = CommentedMap()
        node["keys"] = keys
    if not isinstance(keys, CommentedMap):
        raise KeyEditError(f"profile '{profile}' keys is not a mapping")

    existing = _key_lookup(keys, position)
    if new is None:
        if existing is not None:
            old = keys[existing]
            idx = list(keys).index(existing)
            del keys[existing]
            if idx > 0:  # hand the trailing comment to the previous key
                _move_trailing_comment(old, keys[list(keys)[idx - 1]])
        return

    if existing is not None:
        _move_trailing_comment(keys[existing], new)
        keys[existing] = new
    else:
        later = [i for i, k in enumerate(keys) if str(k).isdigit() and int(str(k)) > position]
        idx = later[0] if later else len(keys)
        if idx == len(keys) and keys:  # new last key takes the trailing comment
            _move_trailing_comment(keys[list(keys)[-1]], new)
        keys.insert(idx, position, new)
