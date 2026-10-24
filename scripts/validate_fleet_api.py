#!/usr/bin/env python3
"""Validate tescmd against an offline snapshot or Tesla's current documentation.

Usage:
    python scripts/validate_fleet_api.py                   # validate the offline snapshot
    python scripts/validate_fleet_api.py --check-docs      # fetch and compare live Tesla docs
    python scripts/validate_fleet_api.py --check-docs --json

The JSON file is a supplemental offline reference for request details. Live
endpoint and command inventory checks use Tesla's published endpoint pages.

Sources:
    - Tesla Fleet API docs: https://developer.tesla.com/docs/fleet-api/endpoints
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = ROOT / "spec" / "fleet_api_spec.json"
COMMAND_API = ROOT / "src" / "tescmd" / "api" / "command.py"
VEHICLE_API = ROOT / "src" / "tescmd" / "api" / "vehicle.py"
ENERGY_API = ROOT / "src" / "tescmd" / "api" / "energy.py"
USER_API = ROOT / "src" / "tescmd" / "api" / "user.py"
SHARING_API = ROOT / "src" / "tescmd" / "api" / "sharing.py"
CHARGING_API = ROOT / "src" / "tescmd" / "api" / "charging.py"
PARTNER_API = ROOT / "src" / "tescmd" / "api" / "partner.py"
PROTOCOL_COMMANDS = ROOT / "src" / "tescmd" / "protocol" / "commands.py"
LIVE_DOC_PAGES = {
    "vehicle commands": "https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-commands",
    "vehicle": "https://developer.tesla.com/docs/fleet-api/endpoints/vehicle-endpoints",
    "energy": "https://developer.tesla.com/docs/fleet-api/endpoints/energy",
    "charging": "https://developer.tesla.com/docs/fleet-api/endpoints/charging-endpoints",
    "user": "https://developer.tesla.com/docs/fleet-api/endpoints/user-endpoints",
    "partner": "https://developer.tesla.com/docs/fleet-api/endpoints/partner-endpoints",
}
LEGACY_REST_ONLY_COMMANDS = {"navigation_request"}

# ---------------------------------------------------------------------------
# Severity levels
# ---------------------------------------------------------------------------

ERROR = "ERROR"
WARNING = "WARNING"
INFO = "INFO"


@dataclass
class Issue:
    severity: str
    category: str
    name: str
    message: str


@dataclass
class MethodSig:
    """Extracted method signature from AST."""

    name: str
    params: list[str]  # parameter names (excluding self, *, **)
    annotations: dict[str, str]  # param_name -> annotation string
    defaults: dict[str, str]  # param_name -> default value string


@dataclass(frozen=True, order=True)
class HttpRoute:
    method: str
    path: str


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)
    command_total: int = 0
    command_found: int = 0
    vehicle_endpoint_total: int = 0
    vehicle_endpoint_found: int = 0
    energy_endpoint_total: int = 0
    energy_endpoint_found: int = 0
    user_endpoint_total: int = 0
    user_endpoint_found: int = 0
    charging_endpoint_total: int = 0
    charging_endpoint_found: int = 0
    partner_endpoint_total: int = 0
    partner_endpoint_found: int = 0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == WARNING)

    @property
    def info_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == INFO)


# ---------------------------------------------------------------------------
# AST introspection
# ---------------------------------------------------------------------------


def _annotation_to_str(node: ast.expr | None) -> str:
    """Convert an AST annotation node to a readable string."""
    if node is None:
        return ""
    return ast.unparse(node)


def extract_methods(source_path: Path) -> dict[str, MethodSig]:
    """Parse a Python file and extract async method signatures from classes."""
    tree = ast.parse(source_path.read_text())
    methods: dict[str, MethodSig] = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name.startswith("_"):
            continue

        params: list[str] = []
        annotations: dict[str, str] = {}
        defaults_map: dict[str, str] = {}

        args = node.args
        # Positional args (skip 'self')
        all_args = args.args[1:] if args.args and args.args[0].arg == "self" else args.args
        # Keyword-only args
        all_kw = args.kwonlyargs

        # Defaults for positional args (right-aligned)
        pos_defaults = args.defaults
        offset = len(all_args) - len(pos_defaults)
        for i, a in enumerate(all_args):
            params.append(a.arg)
            annotations[a.arg] = _annotation_to_str(a.annotation)
            if i >= offset:
                defaults_map[a.arg] = ast.unparse(pos_defaults[i - offset])

        # Keyword-only args with defaults
        for i, a in enumerate(all_kw):
            params.append(a.arg)
            annotations[a.arg] = _annotation_to_str(a.annotation)
            if i < len(args.kw_defaults):
                default = args.kw_defaults[i]
                if default is not None:
                    defaults_map[a.arg] = ast.unparse(default)

        methods[node.name] = MethodSig(
            name=node.name,
            params=params,
            annotations=annotations,
            defaults=defaults_map,
        )

    return methods


class _CodeTextParser(HTMLParser):
    """Collect text rendered in code elements on Tesla's endpoint pages."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.code_depth = 0
        self.current: list[str] = []
        self.code_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "code":
            if self.code_depth == 0:
                self.current = []
            self.code_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self.code_depth:
            self.code_depth -= 1
            if self.code_depth == 0:
                text = "".join(self.current).strip()
                if text:
                    self.code_text.append(text)

    def handle_data(self, data: str) -> None:
        if self.code_depth:
            self.current.append(data)


_DOC_ROUTE_PATTERN = re.compile(
    r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/api/[A-Za-z0-9_{}./:-]+)",
    re.IGNORECASE,
)


def parse_documented_routes(html: str) -> set[HttpRoute]:
    """Extract HTTP method/path pairs from route snippets in official docs HTML."""
    parser = _CodeTextParser()
    parser.feed(html)
    routes: set[HttpRoute] = set()
    for code in parser.code_text:
        for method, path in _DOC_ROUTE_PATTERN.findall(code):
            routes.add(HttpRoute(method.upper(), path.rstrip("/")))
    return routes


def _render_path(node: ast.expr, assignments: dict[str, ast.expr], depth: int = 0) -> str | None:
    """Resolve route strings and f-strings from an API method AST."""
    if depth > 3:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in assignments:
        return _render_path(assignments[node.id], assignments, depth + 1)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                parts.append("{" + ast.unparse(value.value) + "}")
            else:
                return None
        return "".join(parts)
    return None


def extract_http_routes(source_paths: list[Path]) -> set[HttpRoute]:
    """Extract literal API routes from HTTP calls in Python source."""
    methods = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE"}
    routes: set[HttpRoute] = set()
    for source_path in source_paths:
        if not source_path.exists():
            continue
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            assignments: dict[str, ast.expr] = {}
            for statement in ast.walk(function):
                if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                    target = statement.targets[0]
                    if isinstance(target, ast.Name):
                        assignments[target.id] = statement.value
            for call in ast.walk(function):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                    continue
                method = methods.get(call.func.attr)
                if method is None or not call.args:
                    continue
                path = _render_path(call.args[0], assignments)
                if path and "/api/" in path:
                    path = path[path.index("/api/") :]
                if path and path.startswith("/api/"):
                    routes.add(HttpRoute(method, path.rstrip("/")))
    return routes


def _route_matches(pattern: str, path: str) -> bool:
    """Match documented placeholders against source f-string placeholders."""
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(pattern_parts) != len(path_parts):
        return False
    return all(
        left == right or left.startswith("{") or right.startswith("{")
        for left, right in zip(pattern_parts, path_parts, strict=True)
    )


def _live_source_paths() -> list[Path]:
    return [
        COMMAND_API,
        VEHICLE_API,
        ENERGY_API,
        USER_API,
        SHARING_API,
        CHARGING_API,
        ROOT / "src" / "tescmd" / "api" / "partner.py",
        ROOT / "src" / "tescmd" / "api" / "signed_command.py",
        ROOT / "src" / "tescmd" / "auth" / "oauth.py",
    ]


def check_live_docs(*, verbose: bool = False, as_json: bool = False) -> int:
    """Check implementation routes and commands against live Tesla docs."""
    documented_routes: set[HttpRoute] = set()
    fetch_errors: list[str] = []
    for label, url in LIVE_DOC_PAGES.items():
        try:
            request = Request(url, headers={"User-Agent": "tescmd-api-doc-check/1.0"})
            with urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8")
        except (OSError, TimeoutError, UnicodeDecodeError, URLError) as exc:
            fetch_errors.append(f"{label}: {exc}")
            continue
        routes = parse_documented_routes(html)
        if not routes:
            fetch_errors.append(f"{label}: no documented routes found in {url}")
            continue
        documented_routes.update(routes)

    if fetch_errors:
        if as_json:
            print(json.dumps({"errors": fetch_errors}, indent=2))
        else:
            print("Could not validate against live Tesla documentation:")
            for error in fetch_errors:
                print(f"  ERROR: {error}")
        return 2

    command_routes = {route for route in documented_routes if "/command/" in route.path}
    documented_commands = {route.path.rsplit("/", 1)[-1] for route in command_routes}
    documented_endpoints = documented_routes - command_routes
    local_routes = extract_http_routes(_live_source_paths())
    local_commands = extract_methods(COMMAND_API)
    protocol_source = PROTOCOL_COMMANDS.read_text(encoding="utf-8")
    registered_commands = set(
        re.findall(r'^\s*"([^"]+)":\s*CommandSpec\(', protocol_source, re.MULTILINE)
    )

    errors: list[str] = []
    warnings: list[str] = []
    local_only_items: list[str] = []
    for route in sorted(documented_endpoints):
        if not any(
            local.method == route.method and _route_matches(local.path, route.path)
            for local in local_routes
        ):
            errors.append(f"Missing implementation: {route.method} {route.path}")

    for command in sorted(documented_commands):
        if command not in local_commands:
            errors.append(f"Missing CommandAPI method: {command}")
        if command not in registered_commands and command not in LEGACY_REST_ONLY_COMMANDS:
            errors.append(f"Missing signed-command registry entry: {command}")
        elif command in LEGACY_REST_ONLY_COMMANDS and verbose:
            warnings.append(
                f"Documented command {command} is legacy REST-only and excluded from VCP routing"
            )

    for route in sorted(local_routes):
        if "/command/" not in route.path and not any(
            doc.method == route.method and _route_matches(route.path, doc.path)
            for doc in documented_endpoints
        ):
            message = (
                f"Locally implemented but not listed in current public docs: "
                f"{route.method} {route.path}"
            )
            warnings.append(message)
            local_only_items.append(message)
    for command in sorted(set(local_commands) - documented_commands):
        message = f"CommandAPI method not listed in current public docs: {command}"
        warnings.append(message)
        local_only_items.append(message)

    if as_json:
        print(
            json.dumps(
                {
                    "pages_checked": len(LIVE_DOC_PAGES),
                    "documented_endpoints": len(documented_endpoints),
                    "documented_commands": len(documented_commands),
                    "errors": errors,
                    "warnings": warnings if verbose else [],
                    "local_only_count": len(local_only_items),
                },
                indent=2,
            )
        )
    else:
        print("Live Tesla Fleet API documentation check")
        print(
            f"  Pages: {len(LIVE_DOC_PAGES)}  "
            f"Endpoints: {len(documented_endpoints)}  "
            f"Commands: {len(documented_commands)}"
        )
        for error in errors:
            print(f"  ERROR: {error}")
        if verbose:
            for warning in warnings:
                print(f"  NOTE: {warning}")
        if not errors:
            print("  All documented endpoints and commands are represented.")
            if local_only_items:
                print(
                    f"  {len(local_only_items)} local-only item(s) are not in the public "
                    "docs; review them, but absence does not prove deprecation."
                )
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Type checking helpers
# ---------------------------------------------------------------------------

PYTHON_TYPE_MAP = {
    "int": {"int"},
    "float": {"float", "int"},  # float accepts int too
    "bool": {"bool"},
    "string": {"str"},
    "object": {"dict", "dict[str, Any]", "str"},  # flexible for JSON objects
}


def types_compatible(spec_type: str, python_annotation: str, *, required: bool = True) -> bool:
    """Check if a Python annotation is compatible with a spec type."""
    if not python_annotation:
        return True  # no annotation = can't check
    # Strip Optional / | None for optional params
    ann = python_annotation.replace(" ", "")
    if ann.endswith("|None") or ann.startswith("None|"):
        ann = ann.replace("|None", "").replace("None|", "")
    if ann.startswith("Optional[") and ann.endswith("]"):
        ann = ann[9:-1]
    acceptable = PYTHON_TYPE_MAP.get(spec_type, set())
    # Normalize: "dict[str, Any]" -> check if "dict" is in acceptable
    ann_lower = ann.lower().split("[")[0].strip()
    return ann_lower in acceptable or ann in acceptable


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_vehicle_commands(
    spec: dict[str, Any], methods: dict[str, MethodSig], result: ValidationResult
) -> None:
    """Validate vehicle commands (CommandAPI)."""
    commands = spec["vehicle_commands"]
    impl_map = spec.get("implementation_map", {}).get("vehicle_commands", {})

    result.command_total = len(commands)

    for cmd in commands:
        name = cmd["name"]
        py_name = impl_map.get(name, name)

        if py_name not in methods:
            source = cmd.get("source", "docs")
            if source == "go_sdk_only":
                result.issues.append(
                    Issue(
                        WARNING,
                        "MISSING_COMMAND",
                        name,
                        f"Command '{name}' (Go SDK only) not implemented in CommandAPI",
                    )
                )
            else:
                result.issues.append(
                    Issue(
                        ERROR,
                        "MISSING_COMMAND",
                        name,
                        f"Command '{name}' not implemented in CommandAPI",
                    )
                )
            continue

        result.command_found += 1
        method = methods[py_name]
        spec_params = cmd.get("params", [])

        # Check each spec param exists in method signature
        for sp in spec_params:
            sp_name = sp["name"]
            # Skip complex body params that are passed as **kwargs or dict
            if sp["type"] == "object" and sp_name in ("body_json", "value", "location"):
                continue

            if sp_name not in method.params:
                # Check for aliased names (e.g., "vin" is always first positional)
                if sp_name == "vin":
                    continue
                result.issues.append(
                    Issue(
                        ERROR,
                        "MISSING_PARAM",
                        name,
                        f"Command '{name}' missing param '{sp_name}' "
                        f"(expected: {sp['type']}). Method has: {method.params}",
                    )
                )
            else:
                # Type check
                ann = method.annotations.get(sp_name, "")
                if ann and not types_compatible(
                    sp["type"], ann, required=sp.get("required", True)
                ):
                    result.issues.append(
                        Issue(
                            ERROR,
                            "WRONG_TYPE",
                            name,
                            f"Command '{name}' param '{sp_name}' has type '{ann}' "
                            f"but spec says '{sp['type']}'",
                        )
                    )

    # Check for extra methods not in spec (commands we have but spec doesn't)
    spec_py_names = {impl_map.get(c["name"], c["name"]) for c in commands}
    # Also include convenience, key management, and wake commands
    for section in (
        "vehicle_commands_convenience",
        "vehicle_commands_key_management",
        "vehicle_commands_wake",
    ):
        for item in spec.get(section, []):
            py_name = impl_map.get(item["name"], item["name"])
            spec_py_names.add(py_name)

    for method_name in methods:
        if method_name not in spec_py_names:
            result.issues.append(
                Issue(
                    INFO,
                    "EXTRA_COMMAND",
                    method_name,
                    f"Method '{method_name}' in CommandAPI not in spec — "
                    f"may be a new addition or internal method",
                )
            )


def validate_endpoints(
    spec_key: str,
    api_path: Path | list[Path],
    spec: dict[str, Any],
    result: ValidationResult,
    total_attr: str,
    found_attr: str,
    label: str,
    *,
    skip_names: set[str] | None = None,
    name_map: dict[str, str] | None = None,
) -> None:
    """Generic endpoint validator.

    Args:
        api_path: Single file or list of files to search for methods.
        name_map: Maps spec endpoint name -> Python method name when they differ.
    """
    endpoints = spec.get(spec_key, [])
    setattr(result, total_attr, len(endpoints))
    skip = skip_names or set()
    nmap = name_map or {}

    # Collect methods from one or more source files
    paths = api_path if isinstance(api_path, list) else [api_path]
    methods: dict[str, MethodSig] = {}
    for p in paths:
        if p.exists():
            methods.update(extract_methods(p))

    if not methods:
        for ep in endpoints:
            if ep["name"] not in skip:
                result.issues.append(
                    Issue(
                        ERROR,
                        "MISSING_FILE",
                        ep["name"],
                        f"API files not found for {label} endpoints",
                    )
                )
        return

    for ep in endpoints:
        name = ep["name"]
        if name in skip:
            setattr(result, found_attr, getattr(result, found_attr) + 1)
            continue

        note = ep.get("note", "")
        py_name = nmap.get(name, name)

        if py_name not in methods:
            # Check if it's expected to be elsewhere
            if "Implemented in" in note:
                setattr(result, found_attr, getattr(result, found_attr) + 1)
                continue
            if "Used internally" in note:
                setattr(result, found_attr, getattr(result, found_attr) + 1)
                continue
            if "Enterprise only" in note or "Not recommended" in note:
                result.issues.append(
                    Issue(
                        INFO,
                        "MISSING_ENDPOINT",
                        name,
                        f"{label} endpoint '{name}' not implemented. {note}",
                    )
                )
                continue

            result.issues.append(
                Issue(
                    WARNING,
                    "MISSING_ENDPOINT",
                    name,
                    f"{label} endpoint '{name}' ({ep.get('method', '?')} "
                    f"{ep.get('path', '?')}) not implemented",
                )
            )
            continue

        setattr(result, found_attr, getattr(result, found_attr) + 1)


def validate_protocol_registry(spec: dict[str, Any], result: ValidationResult) -> None:
    """Check that all spec commands have protocol registry entries."""
    if not PROTOCOL_COMMANDS.exists():
        result.issues.append(
            Issue(ERROR, "MISSING_FILE", "commands.py", "Protocol commands file missing")
        )
        return

    source = PROTOCOL_COMMANDS.read_text()

    # Commands that should NOT be in registry (REST-only, no signing)
    rest_only = {
        "navigation_request",
        "set_managed_charge_current_request",
        "set_managed_charger_location",
        "set_managed_scheduled_charging_time",
        "wake_up",
        "add_key_request",
    }

    for cmd in spec["vehicle_commands"]:
        name = cmd["name"]
        if name in rest_only:
            continue
        if f'"{name}"' not in source:
            result.issues.append(
                Issue(
                    INFO,
                    "MISSING_REGISTRY",
                    name,
                    f"Command '{name}' not found in protocol registry (commands.py)",
                )
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_spec() -> dict[str, Any]:
    """Load the Fleet API spec file."""
    if not SPEC_PATH.exists():
        print(f"ERROR: Spec file not found: {SPEC_PATH}", file=sys.stderr)
        sys.exit(2)
    spec: object = json.loads(SPEC_PATH.read_text())
    if not isinstance(spec, dict):
        raise ValueError(f"Fleet API spec must contain a JSON object: {SPEC_PATH}")
    return spec


def validate(*, verbose: bool = False) -> ValidationResult:
    """Run full validation."""
    spec = load_spec()
    result = ValidationResult()

    # 1. Vehicle commands (CommandAPI)
    cmd_methods = extract_methods(COMMAND_API)
    validate_vehicle_commands(spec, cmd_methods, result)

    # 2. Vehicle endpoints (VehicleAPI)
    vehicle_name_map = {
        "fleet_telemetry_config_get": "fleet_telemetry_config",
        "fleet_telemetry_config_jws": "fleet_telemetry_config_create_jws",
    }
    validate_endpoints(
        "vehicle_endpoints",
        VEHICLE_API,
        spec,
        result,
        "vehicle_endpoint_total",
        "vehicle_endpoint_found",
        "Vehicle",
        # These are implemented in SharingAPI or are internal
        skip_names={
            "remove_driver",
            "invitations_list",
            "invitations_create",
            "invitations_redeem",
            "invitations_revoke",
            "signed_command",
        },
        name_map=vehicle_name_map,
    )

    # 3. Energy endpoints (EnergyAPI)
    energy_name_map = {
        "products": "list_products",
        "backup": "set_backup_reserve",
        "operation": "set_operation_mode",
        "storm_mode": "set_storm_mode",
    }
    validate_endpoints(
        "energy_endpoints",
        ENERGY_API,
        spec,
        result,
        "energy_endpoint_total",
        "energy_endpoint_found",
        "Energy",
        name_map=energy_name_map,
    )

    # 4. User endpoints (UserAPI)
    validate_endpoints(
        "user_endpoints",
        USER_API,
        spec,
        result,
        "user_endpoint_total",
        "user_endpoint_found",
        "User",
    )

    # 5. Charging endpoints (ChargingAPI)
    charging_name_map = {
        "charging_sessions": "charging_sessions",
    }
    validate_endpoints(
        "charging_endpoints",
        CHARGING_API,
        spec,
        result,
        "charging_endpoint_total",
        "charging_endpoint_found",
        "Charging",
        name_map=charging_name_map,
    )

    # 6. Partner endpoints — split across auth/oauth.py and PartnerAPI
    partner_endpoints = spec.get("partner_endpoints", [])
    result.partner_endpoint_total = len(partner_endpoints)
    auth_oauth = ROOT / "src" / "tescmd" / "auth" / "oauth.py"
    partner_sources = [auth_oauth, PARTNER_API]
    partner_name_map = {
        "register": "register_partner_account",
        "public_key": "public_key",
        "fleet_telemetry_error_vins": "fleet_telemetry_error_vins",
        "fleet_telemetry_errors": "fleet_telemetry_errors",
    }
    # Partner endpoints are handled specially — check by presence in source
    for ep in partner_endpoints:
        name = ep["name"]
        py_name = partner_name_map.get(name, name)
        found = False
        for src in partner_sources:
            if src.exists() and py_name in src.read_text():
                found = True
                break
        if found:
            result.partner_endpoint_found += 1
        else:
            result.issues.append(
                Issue(
                    INFO,
                    "PARTNER_ENDPOINT",
                    name,
                    f"Partner endpoint '{name}' is not implemented by PartnerAPI; "
                    "generic raw access is not counted as endpoint coverage",
                )
            )

    # 7. Protocol registry
    validate_protocol_registry(spec, result)

    return result


def print_report(
    result: ValidationResult, *, verbose: bool = False, as_json: bool = False
) -> None:
    """Print the validation report."""
    if as_json:
        data = {
            "errors": result.error_count,
            "warnings": result.warning_count,
            "info": result.info_count,
            "coverage": {
                "vehicle_commands": {"found": result.command_found, "total": result.command_total},
                "vehicle_endpoints": {
                    "found": result.vehicle_endpoint_found,
                    "total": result.vehicle_endpoint_total,
                },
                "energy_endpoints": {
                    "found": result.energy_endpoint_found,
                    "total": result.energy_endpoint_total,
                },
                "user_endpoints": {
                    "found": result.user_endpoint_found,
                    "total": result.user_endpoint_total,
                },
                "charging_endpoints": {
                    "found": result.charging_endpoint_found,
                    "total": result.charging_endpoint_total,
                },
                "partner_endpoints": {
                    "found": result.partner_endpoint_found,
                    "total": result.partner_endpoint_total,
                },
            },
            "issues": [
                {
                    "severity": i.severity,
                    "category": i.category,
                    "name": i.name,
                    "message": i.message,
                }
                for i in result.issues
            ],
        }
        print(json.dumps(data, indent=2))
        return

    width = 72
    print("=" * width)
    print("Tesla Fleet API Coverage Validation")
    print(f"Spec: {SPEC_PATH.relative_to(ROOT)}")
    print("=" * width)

    # Group issues by severity
    errors = [i for i in result.issues if i.severity == ERROR]
    warnings = [i for i in result.issues if i.severity == WARNING]
    infos = [i for i in result.issues if i.severity == INFO]

    if errors:
        print(f"\n{'-' * width}")
        print(f"ERRORS ({len(errors)})")
        print(f"{'-' * width}")
        for i in errors:
            print(f"  [E] [{i.category}] {i.name}: {i.message}")

    if warnings:
        print(f"\n{'-' * width}")
        print(f"WARNINGS ({len(warnings)})")
        print(f"{'-' * width}")
        for i in warnings:
            print(f"  [W] [{i.category}] {i.name}: {i.message}")

    if verbose and infos:
        print(f"\n{'-' * width}")
        print(f"INFO ({len(infos)})")
        print(f"{'-' * width}")
        for i in infos:
            print(f"  [i] [{i.category}] {i.name}: {i.message}")

    if not errors and not warnings:
        print("\n  All checks passed.")

    # Coverage summary
    print(f"\n{'=' * width}")
    print("COVERAGE SUMMARY")
    print(f"{'=' * width}")

    def pct(found: int, total: int) -> str:
        return f"{found}/{total} ({100 * found // total}%)" if total else "N/A"

    r = result
    print(f"  Vehicle commands:   {pct(r.command_found, r.command_total)}")
    print(f"  Vehicle endpoints:  {pct(r.vehicle_endpoint_found, r.vehicle_endpoint_total)}")
    print(f"  Energy endpoints:   {pct(r.energy_endpoint_found, r.energy_endpoint_total)}")
    print(f"  User endpoints:     {pct(r.user_endpoint_found, r.user_endpoint_total)}")
    print(f"  Charging endpoints: {pct(r.charging_endpoint_found, r.charging_endpoint_total)}")
    print(f"  Partner endpoints:  {pct(r.partner_endpoint_found, r.partner_endpoint_total)}")

    total_found = (
        result.command_found
        + result.vehicle_endpoint_found
        + result.energy_endpoint_found
        + result.user_endpoint_found
        + result.charging_endpoint_found
        + result.partner_endpoint_found
    )
    total_all = (
        result.command_total
        + result.vehicle_endpoint_total
        + result.energy_endpoint_total
        + result.user_endpoint_total
        + result.charging_endpoint_total
        + result.partner_endpoint_total
    )
    print(f"\n  Overall: {pct(total_found, total_all)}")
    errs, warns, info = r.error_count, r.warning_count, r.info_count
    print(f"  Errors: {errs}  Warnings: {warns}  Info: {info}")
    print()


def main() -> None:
    if "--check-docs" in sys.argv:
        sys.exit(
            check_live_docs(
                verbose="--verbose" in sys.argv or "-v" in sys.argv,
                as_json="--json" in sys.argv,
            )
        )

    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    as_json = "--json" in sys.argv

    result = validate(verbose=verbose)
    print_report(result, verbose=verbose, as_json=as_json)

    if result.error_count > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
