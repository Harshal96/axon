"""Adaptable AST-only registry generator. Never imports target registry code."""

import argparse
import ast
import hashlib
import shlex
from pathlib import Path

from axon.v1.servicemap import ServiceMap, dump_service_map

VERSION = "1"


def generate(root: Path, registry: Path, revision: str, date: str) -> str:
    """Example schema: Service(id, path, ...), Library(path), Client(owner, ...).

    Adapt constructor/field names to the inspected registry, commit that adapter
    in the target repo, and document each mapping. Unknown expressions fail.
    """
    raw = registry.read_bytes()
    tree = ast.parse(raw, filename=str(registry))
    services = []
    shared = []
    clients = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        kind = node.func.id
        if kind not in {"Service", "Library", "Client"}:
            continue
        if node.args or any(kw.arg is None for kw in node.keywords):
            raise ValueError(f"{registry}:{node.lineno}: use literal named fields")
        try:
            fields = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{registry}:{node.lineno}: non-literal registry field") from exc
        if kind == "Client":
            owner = fields.pop("owner")
            clients.append((owner, fields))
            continue
        path = fields.pop("path")
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root.resolve()) or not candidate.is_dir():
            raise ValueError(f"{registry}:{node.lineno}: missing or unsafe root: {path}")
        if kind == "Library":
            shared.append(path)
        else:
            for spec in fields.get("specs", []):
                if not (root / spec).is_file():
                    raise ValueError(f"{registry}:{node.lineno}: missing spec: {spec}")
            services.append({**fields, "roots": [path]})
    if not services:
        raise ValueError("registry contains no supported Service declarations")
    by_id = {service["id"]: service for service in services}
    for owner, client in clients:
        if owner not in by_id:
            raise ValueError(f"unknown client owner: {owner}")
        by_id[owner].setdefault("clients", []).append(client)
    sm = ServiceMap.model_validate(
        {
            "version": 1,
            "repo": root.resolve().name,
            "services": sorted(services, key=lambda service: service["id"]),
            "shared": sorted(set(shared)),
        }
    )
    registry_path = str(registry.resolve().relative_to(root.resolve()))
    command = shlex.join(
        [
            "python",
            "tools/axon/gen_axon_yaml.py",
            "--root",
            ".",
            "--registry",
            registry_path,
            "--revision",
            revision,
            "--date",
            date,
        ]
    )
    header = (
        f"# Generator version: {VERSION}; registry revision: {revision}; date: {date}\n"
        f"# Registry sha256: {hashlib.sha256(raw).hexdigest()}\n"
        f"# Command: {command}\n"
        "# do not hand-edit; edit axon.overrides.yaml\n"
    )
    return header + "overrides: axon.overrides.yaml\n" + dump_service_map(sm)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--date", required=True, help="Pinned generation date for reproducibility")
    args = parser.parse_args()
    print(generate(args.root, args.registry, args.revision, args.date), end="")


if __name__ == "__main__":
    main()
