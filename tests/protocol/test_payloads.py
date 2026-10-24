"""Tests for Vehicle Command Protocol payload builders."""

from __future__ import annotations

import struct

import pytest

from tescmd.protocol.payloads import build_command_payload


def test_parental_controls_activate_payload() -> None:
    assert build_command_payload("parental_controls_activate", {"pin": "1234"}) == (
        b"\x12\x0b\xfa\x06\x08\x08\x01\x12\x041234"
    )


def test_parental_controls_deactivate_payload() -> None:
    assert build_command_payload("parental_controls_deactivate", {"pin": "1234"}) == (
        b"\x12\x0b\xfa\x06\x08\x08\x00\x12\x041234"
    )


def test_parental_controls_enable_setting_payload() -> None:
    assert (
        build_command_payload(
            "parental_controls_enable_setting",
            {"setting": "Acceleration", "enable": True},
        )
        == b"\x12\x07\x82\x07\x04\x08\x01\x10\x01"
    )


def test_parental_controls_set_speed_limit_payload() -> None:
    expected = b"\x12\x0c\x8a\x07\x09\x09" + struct.pack("<d", 65.5)
    assert (
        build_command_payload("parental_controls_set_speed_limit", {"limit_mph": 65.5}) == expected
    )


def test_parental_controls_clear_pin_admin_payload() -> None:
    assert build_command_payload("parental_controls_clear_pin_admin", None) == (
        b"\x12\x03\xf2\x06\x00"
    )


@pytest.mark.parametrize("pin", ["123", "12345", "12a4", None])
def test_parental_controls_require_four_digit_pin(pin: object) -> None:
    with pytest.raises(ValueError, match="four-digit PIN"):
        build_command_payload("parental_controls_activate", {"pin": pin})


def test_parental_controls_reject_unknown_setting() -> None:
    with pytest.raises(ValueError, match="Unsupported Parental Controls setting"):
        build_command_payload(
            "parental_controls_enable_setting",
            {"setting": "speed_limit", "enable": True},
        )
