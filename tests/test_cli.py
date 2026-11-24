"""CLI helpers: keytest config overrides and the `learn` output block."""

from __future__ import annotations

from pathlib import Path

import yaml

from radio_keyer.__main__ import keypad_block, keytest_config
from radio_keyer.config import parse_config_text
from radio_keyer.keypad import DEFAULT_DEVICE_PATH, DeviceInfo


def _parse_block(block: str) -> dict[str, object]:
    cfg = parse_config_text(block + "\nprofiles: {a: {}}")
    raw = yaml.safe_load(block)
    assert isinstance(raw, dict)
    assert cfg.keypad.keys == tuple(raw["keypad"]["keys"])
    return raw["keypad"]


def test_keypad_block_prefers_by_id_path() -> None:
    info = DeviceInfo(
        "/dev/input/event8",
        'Pad "Pro" Keyboard',
        ("/dev/input/by-id/usb-Pad-event-mouse", "/dev/input/by-id/usb-Pad-event-kbd"),
        True,
    )
    block = keypad_block(["KEY_F", "KEY_E"], "/dev/input/event8", info, None)
    kp = _parse_block(block)
    assert kp["device"] == "/dev/input/by-id/usb-Pad-event-kbd"
    assert kp["columns"] == 2
    assert '# device: {name: "Pad \\"Pro\\" Keyboard"}' in block


def test_keypad_block_by_name() -> None:
    info = DeviceInfo("/dev/input/event20", "BT Pad", (), True)
    kp = _parse_block(keypad_block(["KEY_1", "KEY_2", "KEY_3", "KEY_4"], info.path, info, None))
    assert kp["device"] == {"name": "BT Pad"} and kp["columns"] == 3
    kp = _parse_block(keypad_block(["KEY_1"], "/dev/input/event8", None, "Asked For"))
    assert kp["device"] == {"name": "Asked For"}


def test_keytest_config(tmp_path: Path) -> None:
    assert keytest_config(tmp_path / "none.yaml", None, None).device == DEFAULT_DEVICE_PATH
    path = tmp_path / "config.yaml"
    path.write_text("keypad: {device: {name: Pad}, keys: [a, b]}\nprofiles: {a: {}}\n")
    kc = keytest_config(path, None, None)
    assert kc.device_name == "Pad" and kc.num_keys == 2
    kc = keytest_config(path, "/dev/input/event3", None)
    assert kc.device == "/dev/input/event3" and kc.device_name is None and kc.num_keys == 2
    assert keytest_config(path, None, "Other").device_name == "Other"
