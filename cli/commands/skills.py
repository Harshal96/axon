"""Install bundled skills without overwriting user changes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from importlib.metadata import PackageNotFoundError, version
from importlib.resources import files
from pathlib import Path

import typer

app = typer.Typer(help="List and install Axon IDE-agent skills")
_PROJECT = {
    "codex": ".agents/skills",
    "cursor": ".agents/skills",
    "claude": ".claude/skills",
    "devin": ".devin/skills",
}
_GLOBAL = {
    "codex": ".codex/skills",
    "cursor": ".cursor/skills",
    "claude": ".claude/skills",
    "devin": ".config/devin/skills",
}


def _version() -> str:
    for distribution in ("axon-cli", "axon"):
        try:
            return version(distribution)
        except PackageNotFoundError:
            pass
    return "0.2.0"


def bundled() -> dict[str, str]:
    result = {}
    for entry in sorted(files("cli").joinpath("skills").iterdir(), key=lambda p: p.name):
        skill = entry.joinpath("SKILL.md")
        if skill.is_file():
            text = skill.read_text(encoding="utf-8")
            result[entry.name] = text.replace("---\n", f"---\naxon-version: {_version()}\n", 1)
    return result


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".axon-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@app.command("list")
def list_cmd(json_output: bool = typer.Option(False, "--json")) -> None:
    data = {"version": _version(), "skills": sorted(bundled())}
    typer.echo(json.dumps(data) if json_output else "\n".join(data["skills"]))


def _manage(
    action: str, repo: str, agent: str, global_: bool, project: bool, force: bool, json_output: bool
) -> None:
    def fail(message: str, code: int) -> None:
        typer.echo(json.dumps({"error": message}) if json_output else message)
        raise typer.Exit(code)

    if agent not in _PROJECT or (global_ and project):
        fail("choose one scope and an agent: codex, cursor, claude, devin", 2)
    base = Path.home() / _GLOBAL[agent] if global_ else Path(repo).resolve() / _PROJECT[agent]
    planned = []
    for name, content in bundled().items():
        directory = base / name
        target = directory / "SKILL.md"
        manifest = directory / ".axon-install.json"
        if directory.is_symlink() or target.is_symlink() or manifest.is_symlink():
            fail(f"refusing symlink: {directory}", 4)
        previous = {}
        if manifest.exists():
            try:
                previous = json.loads(manifest.read_text())
            except (ValueError, OSError):
                pass
        if target.exists() and not force:
            if _digest(target.read_text()) != previous.get("sha256"):
                fail(f"locally modified or unmanaged skill: {target}; use --force", 4)
        planned.append((target, manifest, content))
    for target, manifest, content in planned:
        if action == "uninstall":
            target.unlink(missing_ok=True)
            manifest.unlink(missing_ok=True)
        else:
            _write(target, content)
            _write(manifest, json.dumps({"version": _version(), "sha256": _digest(content)}))
    data = {
        "action": action,
        "version": _version(),
        "files": [str(target) for target, _, _ in planned],
    }
    typer.echo(json.dumps(data) if json_output else "\n".join(data["files"]))


def _command(action: str):  # type: ignore[no-untyped-def]
    def command(
        repo: str = typer.Option(".", "--repo"),
        agent: str = typer.Option("codex", "--agent"),
        global_: bool = typer.Option(False, "--global"),
        project: bool = typer.Option(False, "--project"),
        force: bool = typer.Option(False, "--force"),
        json_output: bool = typer.Option(False, "--json"),
    ) -> None:
        _manage(action, repo, agent, global_, project, force, json_output)

    return command


for _action in ("install", "update", "uninstall"):
    app.command(_action)(_command(_action))


def installed_skill_issues(root: str | Path) -> list[dict[str, str]]:
    """Report installed Axon workflows older than this CLI, without writing.

    Both project and global agent locations are checked, with shared paths
    deduplicated. Unstamped source skills remain unmanaged and are ignored.
    """

    def release(value: str) -> tuple[int, ...] | None:
        match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(.*)", value)
        if not match:
            return None
        parts = tuple(int(part) for part in match.group(1).split("."))
        suffix = match.group(2).lower()
        phase, sequence = 4, 0
        if suffix and not suffix.startswith("+"):
            pre = re.fullmatch(r"[.-]?(dev|a|alpha|b|beta|rc|post)(\d*)", suffix)
            if pre is None:
                return None
            phase = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "rc": 3, "post": 5}[
                pre.group(1)
            ]
            sequence = int(pre.group(2) or "0")
        return parts + (0,) * max(0, 4 - len(parts)) + (phase, sequence)

    current_text = _version()
    current = release(current_text)
    if current is None:
        return []
    bases = {Path(root).resolve() / path for path in _PROJECT.values()}
    bases.update(Path.home() / path for path in _GLOBAL.values())
    issues: list[dict[str, str]] = []
    seen: set[Path] = set()
    for base in sorted(bases):
        for name in sorted(bundled()):
            path = base / name / "SKILL.md"
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if not text.startswith("---\n"):
                continue
            frontmatter = text.split("---", 2)
            if len(frontmatter) != 3:
                continue
            stamp = re.search(r"(?m)^axon-version:\s*['\"]?([^'\"\s]+)", frontmatter[1])
            installed = release(stamp.group(1)) if stamp else None
            if installed is not None and installed < current:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "skill-outdated",
                        "message": (
                            f"{path}: installed Axon skill {stamp.group(1)} "
                            f"is older than CLI {current_text}; "
                            "run axon skills update for this scope and agent"
                        ),
                    }
                )
    return issues
