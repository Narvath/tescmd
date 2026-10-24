"""Tests for parsing live Tesla Fleet API documentation."""

from __future__ import annotations

from scripts.validate_fleet_api import HttpRoute, _route_matches, parse_documented_routes


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
