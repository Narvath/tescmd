"""Tests for parsing live Tesla Fleet API documentation."""

from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING

import pytest
from scripts import validate_fleet_api as validator
from scripts.validate_fleet_api import HttpRoute, _route_matches, parse_documented_routes

if TYPE_CHECKING:
    from pathlib import Path


def test_parse_documented_routes_from_nested_code_markup() -> None:
    html = (
        "<div><code>GET <span>/api/1/vehicles/{vin}/vehicle_data_cached</span></code>"
        "<code>POST /api/1/vehicles/{vin}/command/door_lock</code></div>"
    )

    assert parse_documented_routes(html) == {
        HttpRoute("GET", "/api/1/vehicles/{vin}/vehicle_data_cached"),
        HttpRoute("POST", "/api/1/vehicles/{vin}/command/door_lock"),
    }


def test_live_doc_routes_are_present_in_local_apis() -> None:
    from scripts.validate_fleet_api import _live_source_paths, extract_http_routes

    routes = extract_http_routes(_live_source_paths())

    assert HttpRoute("GET", "/api/1/vehicles/{vin}/vehicle_data_cached") in routes
    assert HttpRoute("POST", "/api/1/vehicles/{vin}/command/{command}") in routes


def test_route_matching_accepts_variable_names_but_requires_same_shape() -> None:
    assert _route_matches(
        "/api/1/vehicles/{vin}/invitations/{id}/revoke",
        "/api/1/vehicles/{vehicle_id}/invitations/{invite_id}/revoke",
    )
    assert not _route_matches(
        "/api/1/vehicles/{vin}/invitations/{id}/revoke",
        "/api/1/vehicles/{vehicle_id}/invitations/revoke",
    )


@pytest.mark.parametrize(
    ("include_local_only", "expected_count"),
    [(True, "2 local-only item(s)"), (False, None)],
)
def test_live_doc_summary_excludes_documented_legacy_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    include_local_only: bool,
    expected_count: str | None,
) -> None:
    html = (
        b"<code>GET /api/1/vehicles/{vin}/vehicle_data</code>"
        b"<code>POST /api/1/vehicles/{vin}/command/navigation_request</code>"
    )
    monkeypatch.setattr(validator, "LIVE_DOC_PAGES", {"vehicle": "https://example.test/docs"})
    monkeypatch.setattr(validator, "urlopen", lambda *_args, **_kwargs: BytesIO(html))
    local_routes = {HttpRoute("GET", "/api/1/vehicles/{vin}/vehicle_data")}
    if include_local_only:
        local_routes.add(HttpRoute("GET", "/api/1/vehicles/{vin}/local_only"))
    monkeypatch.setattr(validator, "extract_http_routes", lambda _paths: local_routes)
    monkeypatch.setattr(
        validator,
        "extract_methods",
        lambda _path: (
            {"navigation_request", "local_only_command"}
            if include_local_only
            else {"navigation_request"}
        ),
    )
    protocol_commands = tmp_path / "commands.py"
    protocol_commands.write_text("", encoding="utf-8")
    monkeypatch.setattr(validator, "PROTOCOL_COMMANDS", protocol_commands)

    assert validator.check_live_docs(verbose=True) == 0

    output = capsys.readouterr().out
    assert "navigation_request is legacy REST-only" in output
    if expected_count is None:
        assert "local-only item(s)" not in output
    else:
        assert expected_count in output
