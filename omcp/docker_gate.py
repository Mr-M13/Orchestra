"""Fail-closed Docker argv gate for the Delnavazan disposable acceptance operation."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ALLOWED_IMAGES = {
    "mariadb:11.4", "wordpress:php8.3-apache", "wordpress:cli-php8.3",
}
COMPOSE_COMMANDS = {
    ("up", "--pull", "never", "-d", "--wait", "db"),
    ("up", "--pull", "never", "-d", "wordpress"),
    ("down", "--volumes", "--remove-orphans"),
}


def _required_environment() -> dict[str, str]:
    names = (
        "OMCP_DOCKER_REAL", "OMCP_DOCKER_PROJECT_DIR", "OMCP_DOCKER_PROJECT",
        "OMCP_DOCKER_COMPOSE_FILE", "OMCP_DOCKER_OVERRIDE",
        "OMCP_DOCKER_RUNTIME_STATE", "OMCP_DOCKER_NETWORK",
    )
    values = {name: os.environ.get(name, "") for name in names}
    if not all(values.values()):
        raise PermissionError("Docker gate configuration is incomplete")
    return values


def _inside(path: str, root: Path) -> bool:
    candidate = Path(path).resolve(strict=False)
    return candidate != root and candidate.is_relative_to(root)


def gated_command(arguments: list[str], environment: dict[str, str]) -> list[str]:
    """Return the only real Docker command permitted by the built-in operation."""
    if not arguments:
        raise PermissionError("Docker subcommand is required")
    real = str(Path(environment["OMCP_DOCKER_REAL"]).resolve(strict=True))
    runtime_state = Path(environment["OMCP_DOCKER_RUNTIME_STATE"]).resolve(strict=True)
    if arguments[:2] == ["image", "inspect"]:
        if len(arguments) != 3 or arguments[2] not in ALLOWED_IMAGES:
            raise PermissionError("only required cached image inspection is permitted")
        return [real, *arguments]
    if arguments[0] == "compose":
        prefix = [
            "compose", "--project-directory", environment["OMCP_DOCKER_PROJECT_DIR"],
            "--project-name", environment["OMCP_DOCKER_PROJECT"], "-f",
            environment["OMCP_DOCKER_COMPOSE_FILE"],
        ]
        if arguments[:len(prefix)] != prefix or tuple(arguments[len(prefix):]) not in COMPOSE_COMMANDS:
            raise PermissionError("Docker Compose command is outside the acceptance allowlist")
        return [
            real, *prefix, "-f", environment["OMCP_DOCKER_OVERRIDE"],
            *arguments[len(prefix):],
        ]
    if arguments[0] != "run":
        raise PermissionError("Docker command is outside the acceptance allowlist")
    options_with_value = {"-v", "--volume", "--network", "-u", "--user", "-e", "--env", "--entrypoint"}
    allowed_flags = {"--rm", "--pull=never"}
    parsed = ["run"]
    index = 1
    network_seen = False
    image = None
    while index < len(arguments):
        value = arguments[index]
        if value in ALLOWED_IMAGES:
            image = value
            parsed.extend(arguments[index:])
            break
        if value in allowed_flags:
            parsed.append(value)
            index += 1
            continue
        if value not in options_with_value or index + 1 >= len(arguments):
            raise PermissionError("Docker run option is outside the acceptance allowlist")
        option_value = arguments[index + 1]
        if value in {"-v", "--volume"}:
            source = option_value.split(":", 1)[0]
            if not _inside(source, runtime_state):
                raise PermissionError("Docker mounts must remain inside operation runtime state")
        elif value == "--network":
            if option_value != environment["OMCP_DOCKER_NETWORK"]:
                raise PermissionError("Docker run network is outside the operation network")
            network_seen = True
        elif value == "--entrypoint" and option_value != "php":
            raise PermissionError("only the PHP entrypoint is permitted")
        parsed.extend([value, option_value])
        index += 2
    if image is None:
        raise PermissionError("Docker run must use an approved cached image")
    if "--rm" not in parsed or "--pull=never" not in parsed:
        raise PermissionError("Docker run must be disposable and pull-disabled")
    if not network_seen:
        image_index = parsed.index(image)
        parsed[image_index:image_index] = ["--network", "none"]
    return [real, *parsed]


def main(argv: list[str] | None = None) -> int:
    try:
        environment = _required_environment()
        command = gated_command(list(sys.argv[1:] if argv is None else argv), environment)
    except (OSError, PermissionError, ValueError) as exc:
        print(f"docker gate denied request: {exc}", file=sys.stderr)
        return 126
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
