"""Redacted comparison of rendered deployment controls and runtime readback."""

from collections.abc import Mapping

from .delivery_safety import DISABLED_EMAIL_CONTROLS, LIVE_EMAIL_CONTROLS, email_activation_status

EMAIL_COMPONENTS = ("gateway", "worker", "smtp-relay", "postal-web", "postal-worker", "postal-smtp")
RUNTIME_NAMES = {"klyrow-" + name + "-1": name for name in EMAIL_COMPONENTS}


def _environment(value):
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, list):
        result = {}
        for entry in value:
            if not isinstance(entry, str) or "=" not in entry:
                raise ValueError("invalid_environment")
            name, raw = entry.split("=", 1)
            if name in result:
                raise ValueError("duplicate_environment_control")
            result[name] = raw
        return result
    raise ValueError("invalid_environment")


def validate_email_activation(document, *, source="compose") -> dict:
    """Require one complete disabled or live profile across the delivery chain.

    This is configuration evidence only. Postal flags are not evidence that
    upstream Postal or a host-mounted entrypoint enforces them. No credentials,
    message data, arbitrary environment values, or health logs are returned.
    """

    errors = []
    services = {}
    if source == "compose" and isinstance(document, dict):
        configured = document.get("services", {})
        if isinstance(configured, dict):
            for name in EMAIL_COMPONENTS:
                service = configured.get(name)
                if isinstance(service, dict):
                    services[name] = service.get("environment", {})
    elif source == "inspect" and isinstance(document, list):
        for container in document:
            if not isinstance(container, dict):
                errors.append("invalid_container_record")
                continue
            name = RUNTIME_NAMES.get(str(container.get("Name", "")).lstrip("/"))
            if name is None:
                continue
            if name in services:
                errors.append(name + ":duplicate_container")
                continue
            config = container.get("Config") or {}
            state = container.get("State") or {}
            if not isinstance(config, dict) or not isinstance(state, dict):
                errors.append(name + ":invalid_container_record")
                continue
            services[name] = config.get("Env", [])
            if state.get("Status") != "running":
                errors.append(name + ":not_running")
    else:
        raise ValueError("invalid_activation_document")

    components = {}
    for name in EMAIL_COMPONENTS:
        if name not in services:
            errors.append(name + ":missing_component")
            continue
        try:
            status = email_activation_status(_environment(services[name]))
        except ValueError:
            errors.append(name + ":invalid_environment")
            continue
        components[name] = status
        if status["missing_controls"]:
            errors.append(name + ":missing_controls")
        if status["invalid_controls"]:
            errors.append(name + ":invalid_controls")
        if status["controls"] not in (DISABLED_EMAIL_CONTROLS, LIVE_EMAIL_CONTROLS):
            errors.append(name + ":partial_activation")

    reference = components.get("gateway", {}).get("controls")
    for name, status in components.items():
        if reference is not None and status["controls"] != reference:
            errors.append(name + ":gateway_mismatch")
    consistent = not errors and len(components) == len(EMAIL_COMPONENTS)
    return {
        "scope": "stack_configuration",
        "source": source,
        "configuration_consistent": consistent,
        "mode": components["gateway"]["mode"] if consistent else "blocked",
        "components": components,
        "errors": sorted(set(errors)),
        "postal_enforcement": "unverified",
        "end_to_end_delivery": "unverified",
    }
