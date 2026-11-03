"""Source-backed discovery for the Klyrow SMTP integration contract.

The conformance assertions are added in S0-T3.  These helpers deliberately
inspect Python syntax instead of duplicating application behavior in a test
fixture or importing the gateway and relying on runtime registration side
effects.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_ROOT = REPOSITORY_ROOT / "apps" / "gateway" / "app"
MANIFEST_PATH = REPOSITORY_ROOT / "codestra" / "integration" / "klyrow-smtp.integration.v1.json"
COMMAND_PATTERN = re.compile(r"^email\.[a-z0-9_.-]+\.v[0-9]+$")
HTTP_METHODS = {"delete", "get", "patch", "post", "put"}
PROMETHEUS_CONSTRUCTORS = {"Counter", "Gauge", "Histogram", "Info", "Summary"}


@dataclass(frozen=True)
class SourceModule:
    path: Path
    tree: ast.Module
    module_bindings: dict[str, tuple[ast.AST, ...]]
    router_prefixes: dict[str, str]


@dataclass(frozen=True)
class DynamicSite:
    path: str
    line: int


@dataclass(frozen=True)
class EventDiscovery:
    events: frozenset[str]
    dynamic_sites: tuple[DynamicSite, ...]


@dataclass(frozen=True)
class CommandDiscovery:
    command_endpoint_registered: bool
    readback_endpoint_registered: bool
    commands: frozenset[str]


@dataclass(frozen=True)
class StatusDiscovery:
    statuses: frozenset[str]
    dynamic_sites: tuple[DynamicSite, ...]


@dataclass(frozen=True)
class CollectorDiscovery:
    path: str
    line: int
    name: str
    labels: frozenset[str]


def load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _assignment_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return None


def _bindings(nodes: Iterable[ast.stmt]) -> dict[str, tuple[ast.AST, ...]]:
    collected: dict[str, list[ast.AST]] = {}
    for statement in nodes:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                name = _assignment_name(target)
                if name and value is not None:
                    collected.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in collected.items()}


def _function_bindings(function: ast.AST) -> dict[str, tuple[ast.AST, ...]]:
    collected: dict[str, list[ast.AST]] = {}
    for statement in ast.walk(function):
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                name = _assignment_name(target)
                if name and value is not None:
                    collected.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in collected.items()}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal_strings(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        item.value
        for item in ast.walk(node)
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _string_values(
    node: ast.AST | None,
    local_bindings: dict[str, tuple[ast.AST, ...]],
    module_bindings: dict[str, tuple[ast.AST, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[set[str], bool]:
    """Resolve finite string values and report whether an expression is open-ended."""

    if node is None:
        return set(), False
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return {node.value}, False
        return set(), False
    if isinstance(node, ast.Name):
        if node.id in seen:
            return set(), True
        expressions = local_bindings.get(node.id) or module_bindings.get(node.id)
        if not expressions:
            return set(), True
        values: set[str] = set()
        dynamic = False
        for expression in expressions:
            found, is_dynamic = _string_values(
                expression,
                local_bindings,
                module_bindings,
                seen | {node.id},
            )
            values.update(found)
            dynamic = dynamic or is_dynamic
        return values, dynamic
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        values: set[str] = set()
        dynamic = False
        for element in node.elts:
            found, is_dynamic = _string_values(element, local_bindings, module_bindings, seen)
            values.update(found)
            dynamic = dynamic or is_dynamic
        return values, dynamic
    if isinstance(node, ast.Dict):
        values: set[str] = set()
        dynamic = False
        for value in node.values:
            found, is_dynamic = _string_values(value, local_bindings, module_bindings, seen)
            values.update(found)
            dynamic = dynamic or is_dynamic
        return values, dynamic
    if isinstance(node, ast.IfExp):
        left, left_dynamic = _string_values(node.body, local_bindings, module_bindings, seen)
        right, right_dynamic = _string_values(node.orelse, local_bindings, module_bindings, seen)
        return left | right, left_dynamic or right_dynamic
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, left_dynamic = _string_values(node.left, local_bindings, module_bindings, seen)
        right, right_dynamic = _string_values(node.right, local_bindings, module_bindings, seen)
        combined = {prefix + suffix for prefix in left for suffix in right}
        return combined, left_dynamic or right_dynamic
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            mapping_name = None
            if isinstance(node.func.value, ast.Name):
                mapping_name = node.func.value.id
            elif isinstance(node.func.value, ast.Attribute):
                mapping_name = node.func.value.attr
            mapping_expressions = module_bindings.get(mapping_name or "") or local_bindings.get(
                mapping_name or ""
            )
            dictionary_values: set[str] = set()
            dictionary_found = False
            if mapping_expressions:
                for expression in mapping_expressions:
                    if isinstance(expression, ast.Dict):
                        dictionary_found = True
                        found, _dynamic = _string_values(
                            expression, local_bindings, module_bindings, seen
                        )
                        dictionary_values.update(found)
            default_values: set[str] = set()
            default_dynamic = False
            if len(node.args) >= 2:
                default_values, default_dynamic = _string_values(
                    node.args[1], local_bindings, module_bindings, seen
                )
            if dictionary_found:
                return dictionary_values | default_values, default_dynamic
            return default_values, True
        if _call_name(node.func) == "str" and node.args:
            return _string_values(node.args[0], local_bindings, module_bindings, seen)
        return set(), True
    return set(), True


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    prefixes: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or not isinstance(statement.value, ast.Call):
            continue
        if _call_name(statement.value.func) != "APIRouter":
            continue
        prefix = ""
        for keyword in statement.value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = str(keyword.value.value)
        for target in statement.targets:
            name = _assignment_name(target)
            if name:
                prefixes[name] = prefix
    return prefixes


def source_modules() -> tuple[SourceModule, ...]:
    modules = []
    for path in sorted(APPLICATION_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules.append(
            SourceModule(
                path=path,
                tree=tree,
                module_bindings=_bindings(tree.body),
                router_prefixes=_router_prefixes(tree),
            )
        )
    return tuple(modules)


def _relative(path: Path) -> str:
    return str(path.relative_to(REPOSITORY_ROOT))


def discover_emitted_events() -> EventDiscovery:
    events: set[str] = set()
    dynamic_sites: set[tuple[str, int]] = set()
    modules = source_modules()
    shared_bindings: dict[str, tuple[ast.AST, ...]] = {}
    for module in modules:
        for name, expressions in module.module_bindings.items():
            if name.isupper():
                shared_bindings[name] = shared_bindings.get(name, ()) + expressions
    for module in modules:
        available_bindings = {**shared_bindings, **module.module_bindings}
        functions = (
            node
            for node in ast.walk(module.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            local_bindings = _function_bindings(function)
            for call in (
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call) and _call_name(node.func) == "emit_middleware"
            ):
                expression = call.args[0] if call.args else None
                values, dynamic = _string_values(expression, local_bindings, available_bindings)
                events.update(value for value in values if value.startswith("klyrow."))
                if dynamic:
                    dynamic_sites.add((_relative(module.path), call.lineno))
    return EventDiscovery(
        events=frozenset(events),
        dynamic_sites=tuple(DynamicSite(path, line) for path, line in sorted(dynamic_sites)),
    )


def _routes(module: SourceModule) -> tuple[tuple[str, str, ast.AST], ...]:
    routes = []
    for function in ast.walk(module.tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.lower()
            if method not in HTTP_METHODS or not decorator.args:
                continue
            path_node = decorator.args[0]
            if not isinstance(path_node, ast.Constant) or not isinstance(path_node.value, str):
                continue
            owner = decorator.func.value
            owner_name = owner.id if isinstance(owner, ast.Name) else ""
            prefix = module.router_prefixes.get(owner_name, "")
            path = (prefix.rstrip("/") + "/" + path_node.value.lstrip("/")).rstrip("/")
            routes.append((method.upper(), path or "/", function))
    return tuple(routes)


def discover_commands() -> CommandDiscovery:
    command_route = False
    readback_route = False
    commands: set[str] = set()
    for module in source_modules():
        routes = _routes(module)
        command_handlers = [
            function
            for method, path, function in routes
            if method == "POST" and path == "/v1/commands"
        ]
        command_route = command_route or bool(command_handlers)
        readback_route = readback_route or any(
            method == "GET" and path == "/v1/operations/{command_id}"
            for method, path, _function in routes
        )
        if command_handlers:
            referenced_names = {
                node.id
                for function in command_handlers
                for node in ast.walk(function)
                if isinstance(node, ast.Name)
            }
            candidates = set()
            for function in command_handlers:
                candidates.update(_literal_strings(function))
            for name in referenced_names:
                for expression in module.module_bindings.get(name, ()):
                    candidates.update(_literal_strings(expression))
            commands.update(value for value in candidates if COMMAND_PATTERN.match(value))
    return CommandDiscovery(command_route, readback_route, frozenset(commands))


def _contains_symbol(node: ast.AST, name: str) -> bool:
    return any(
        (isinstance(item, ast.Name) and item.id == name)
        or (isinstance(item, ast.Attribute) and item.attr == name)
        for item in ast.walk(node)
    )


def discover_message_statuses() -> StatusDiscovery:
    statuses: set[str] = set()
    dynamic_sites: set[tuple[str, int]] = set()
    for module in source_modules():
        functions = (
            node
            for node in ast.walk(module.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        )
        for function in functions:
            local_bindings = _function_bindings(function)
            core_message_variables: set[str] = set()
            for assignment in (
                node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign))
            ):
                value = assignment.value
                if value is None or not _contains_symbol(value, "Message"):
                    continue
                if _contains_symbol(value, "ProviderMessage"):
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                core_message_variables.update(
                    name for target in targets if (name := _assignment_name(target))
                )

            for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
                if _call_name(call.func) != "Message":
                    continue
                for keyword in call.keywords:
                    if keyword.arg != "status":
                        continue
                    values, dynamic = _string_values(
                        keyword.value, local_bindings, module.module_bindings
                    )
                    statuses.update(values)
                    if dynamic:
                        dynamic_sites.add((_relative(module.path), call.lineno))

            for assignment in (
                node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign))
            ):
                value = assignment.value
                if value is None:
                    continue
                targets = (
                    assignment.targets
                    if isinstance(assignment, ast.Assign)
                    else [assignment.target]
                )
                status_target = any(
                    isinstance(target, ast.Attribute)
                    and target.attr == "status"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in core_message_variables
                    for target in targets
                )
                if not status_target:
                    continue
                values, dynamic = _string_values(value, local_bindings, module.module_bindings)
                statuses.update(values)
                if dynamic:
                    dynamic_sites.add((_relative(module.path), assignment.lineno))
    return StatusDiscovery(
        statuses=frozenset(statuses),
        dynamic_sites=tuple(DynamicSite(path, line) for path, line in sorted(dynamic_sites)),
    )


def discover_canonical_status_vocabulary() -> frozenset[str]:
    values: set[str] = set()
    for module in source_modules():
        for expression in module.module_bindings.get("CANONICAL_SMTP_STATUSES", ()):
            found, dynamic = _string_values(expression, {}, module.module_bindings)
            assert dynamic is False
            values.update(found)
    return frozenset(values)


def discover_middleware_headers() -> frozenset[str]:
    headers: set[str] = set()
    for module in source_modules():
        for function in ast.walk(module.tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if function.name != "emit_middleware":
                continue
            for node in ast.walk(function):
                if isinstance(node, ast.Assign):
                    if any(
                        isinstance(target, ast.Name) and target.id == "headers"
                        for target in node.targets
                    ) and isinstance(node.value, ast.Dict):
                        headers.update(
                            key.value
                            for key in node.value.keys
                            if isinstance(key, ast.Constant) and isinstance(key.value, str)
                        )
                    for target in node.targets:
                        if (
                            isinstance(target, ast.Subscript)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "headers"
                        ):
                            headers.update(_literal_strings(target.slice))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "headers"
                    and node.func.attr == "update"
                ):
                    for argument in node.args:
                        if isinstance(argument, ast.Dict):
                            headers.update(
                                key.value
                                for key in argument.keys
                                if isinstance(key, ast.Constant) and isinstance(key.value, str)
                            )
    return frozenset(headers)


def discover_prometheus_collectors() -> tuple[CollectorDiscovery, ...]:
    collectors = []
    for module in source_modules():
        for call in (
            node
            for node in ast.walk(module.tree)
            if isinstance(node, ast.Call) and _call_name(node.func) in PROMETHEUS_CONSTRUCTORS
        ):
            if not call.args or not isinstance(call.args[0], ast.Constant):
                continue
            name = call.args[0].value
            if not isinstance(name, str):
                continue
            label_node = call.args[2] if len(call.args) >= 3 else None
            for keyword in call.keywords:
                if keyword.arg in {"labelnames", "labels"}:
                    label_node = keyword.value
            labels, dynamic = _string_values(label_node, {}, {})
            if dynamic:
                labels.update(_literal_strings(label_node))
            collectors.append(
                CollectorDiscovery(
                    path=_relative(module.path),
                    line=call.lineno,
                    name=name,
                    labels=frozenset(labels),
                )
            )
    return tuple(sorted(collectors, key=lambda item: (item.path, item.line)))


def test_source_discovery_finds_direct_and_finite_mapped_event_emitters() -> None:
    discovery = discover_emitted_events()
    assert "klyrow.email.queued" in discovery.events
    assert "klyrow.email.delivered" in discovery.events


def test_source_discovery_combines_router_prefixes_with_route_paths() -> None:
    routes = {
        (method, path) for module in source_modules() for method, path, _function in _routes(module)
    }
    assert ("POST", "/v1/email/send") in routes
    assert ("POST", "/v1/internal/email/send") in routes


def test_source_discovery_finds_real_message_statuses() -> None:
    discovery = discover_message_statuses()
    assert {"accepted", "queued"} <= discovery.statuses


def test_source_discovery_reads_middleware_headers_from_sender_code() -> None:
    headers = discover_middleware_headers()
    assert {"Authorization", "X-Klyrow-Event-Id", "X-Klyrow-Signature"} <= headers


def test_source_discovery_reads_prometheus_constructor_labels() -> None:
    collectors = {item.name: item for item in discover_prometheus_collectors()}
    assert collectors["klyrow_http_requests_total"].labels == {
        "codestra_business",
        "application",
        "service",
        "environment",
        "server",
        "region",
        "deployment",
        "path",
        "status",
    }


def test_string_discovery_preserves_a_default_for_an_open_ended_value() -> None:
    expression = ast.parse('payload.get("event", "klyrow.email.unknown")', mode="eval").body
    values, dynamic = _string_values(expression, {}, {})
    assert values == {"klyrow.email.unknown"}
    assert dynamic is True


def test_string_discovery_rejects_an_unbound_name_as_open_ended() -> None:
    expression = ast.parse("runtime_event", mode="eval").body
    values, dynamic = _string_values(expression, {}, {})
    assert values == set()
    assert dynamic is True


def test_declared_events_match_emitted_events() -> None:
    manifest = load_manifest()
    declared = set(manifest["events"]["publishedEvents"])
    discovery = discover_emitted_events()

    assert discovery.dynamic_sites == (), (
        f"unbounded emit_middleware event expressions: {discovery.dynamic_sites}"
    )
    assert declared == set(discovery.events), (
        f"claimed-not-emitted: {declared - discovery.events}; "
        f"emitted-not-declared: {discovery.events - declared}"
    )


def test_declared_commands_are_routed_and_readable() -> None:
    manifest = load_manifest()
    declared = set(manifest["middleware"]["allowedCommands"])
    discovery = discover_commands()

    assert discovery.command_endpoint_registered is True
    assert discovery.readback_endpoint_registered is True
    assert declared == set(discovery.commands), (
        f"unrouted commands: {declared - discovery.commands}; "
        f"undeclared handlers: {discovery.commands - declared}"
    )


def test_declared_status_model_matches_message_code() -> None:
    manifest = load_manifest()
    declared = set(manifest["events"]["statusModel"])
    discovery = discover_message_statuses()

    assert discovery.dynamic_sites == (), (
        f"open-ended Message.status expressions: {discovery.dynamic_sites}"
    )
    assert declared == set(discover_canonical_status_vocabulary())
    assert set(discovery.statuses) <= declared, (
        f"used-not-declared: {discovery.statuses - declared}"
    )


def test_required_headers_are_sent_to_middleware() -> None:
    manifest = load_manifest()
    required = set(manifest["middleware"]["requiredHeaders"])
    emitted = set(discover_middleware_headers())

    assert required <= emitted, f"missing Middleware headers: {required - emitted}"


def test_no_collector_uses_a_forbidden_label() -> None:
    manifest = load_manifest()
    forbidden = set(manifest["observability"]["forbiddenLabels"])
    violations = {
        f"{collector.path}:{collector.line}:{collector.name}": sorted(collector.labels & forbidden)
        for collector in discover_prometheus_collectors()
        if collector.labels & forbidden
    }

    assert violations == {}


def test_collectors_carry_required_platform_labels() -> None:
    manifest = load_manifest()
    required = set(manifest["observability"]["requiredLabels"])
    missing = {
        f"{collector.path}:{collector.line}:{collector.name}": sorted(required - collector.labels)
        for collector in discover_prometheus_collectors()
        if required - collector.labels
    }

    assert missing == {}
