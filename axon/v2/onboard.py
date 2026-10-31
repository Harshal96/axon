"""Deterministic onboarding scan: draft an axon.yaml + review report.

Heuristics only propose; a human reviews the draft by PR. Ambiguities are
listed in the report instead of being guessed silently.

Discovery is anchor-based so services at any depth are found: validated
OpenAPI/Swagger specs, app bootstraps, and framework signal clusters each
propose a service root. A spec may sit anywhere inside its service
(``api/``, ``src/main/resources/``, ...), so the root is inferred by
scoring the spec's ancestors for boundary markers instead of assuming
the spec's own directory. Nested candidates with their own boundary
markers are kept (longest root wins at index time); plain subpackages
collapse into their parent. ``.worktrees`` and other hidden/vendor
trees are pruned, never scanned as services.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from pydantic import BaseModel, Field

from axon.v2.client_discovery import discover_clients, suggest_base_urls
from axon.v2.servicemap import ClientDecl, ServiceDecl, ServiceMap, dump_service_map

# Settings-factory call sites, e.g. settings.GET_PAYMENTS_API() returning a
# generated client's API object, and get_V1Api(paymentscli, '...ENV').
_FACTORY_CALL_RE = re.compile(r"\b((?:[A-Za-z_]\w*\.)+GET_[A-Za-z0-9_]+)\s*\(")
_V1API_CALL_RE = re.compile(r"\bget_V1Api\(\s*([A-Za-z_][\w.]*)")

FRAMEWORK_SIGNALS = {
    "fastapi": ("fastapi", "FastAPI(", "APIRouter("),
    "django": ("django", "urlpatterns", "DJANGO_SETTINGS_MODULE"),
    "spring": ("springframework", "@RestController", "@SpringBootApplication"),
    "vertx": ("io.vertx", "AbstractVerticle", "Router.router"),
}
BOOTSTRAP_SIGNALS = {
    "fastapi": ("FastAPI(",),
    "django": ("DJANGO_SETTINGS_MODULE",),
    "spring": ("@SpringBootApplication",),
    "vertx": ("AbstractVerticle",),
    "go-http": ("func main(",),
}
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "target",
    "build",
    "dist",
    ".tox",
    "vendor",
    ".worktrees",
    "worktrees",
}
CODE_SUFFIXES = {".py", ".java", ".go"}
LANG_BY_SUFFIX = {".py": "python", ".java": "java", ".go": "go"}
BUILD_FILES = {
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "go.mod",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "dockerfile",
}
UMBRELLA_DIRS = {
    "services",
    "service",
    "apps",
    "app",
    "microservices",
    "cmd",
    "servers",
    "projects",
    "modules",
    "backend",
}
# Trailing segments that live *inside* a service, never at its boundary.
INNER_DIRS = {
    "api",
    "spec",
    "specs",
    "openapi",
    "swagger",
    "docs",
    "doc",
    "resources",
    "static",
    "public",
    "assets",
    "rest",
    "http",
}
# A spec under one of these is a vendored/client/test copy, not an owner.
CLIENTISH_DIRS = {
    "generated",
    "gen",
    "client",
    "clients",
    "test",
    "tests",
    "testdata",
    "fixtures",
    "fixture",
    "examples",
    "example",
    "mock",
    "mocks",
}


class OnboardReport(BaseModel):
    draft_yaml: str
    services: list[dict[str, Any]] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    # service id -> why this root was drafted (specs, markers, bootstraps)
    evidence: dict[str, list[str]] = Field(default_factory=dict)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "service"


def _factory_matches(dotted: str, slug: str, package: bool = False) -> bool:
    """settings.GET_PAYMENTS_API / paymentscli vs client slug paymentscli."""
    leaf = dotted.rsplit(".", 1)[-1].lower()
    if package:
        token = leaf
    else:
        token = leaf.removeprefix("get_").removesuffix("_api")
    core = slug.removesuffix("cli").removesuffix("-cli").replace("-", "")
    return bool(token) and (core.startswith(token) or token.startswith(core))


def _skipped(part: str) -> bool:
    return part in SKIP_DIRS or (part.startswith(".") and part not in (".", ".."))


def _git_files(root: Path) -> list[str] | None:
    try:
        top = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if top.returncode != 0:
            return None
        toplevel = Path(top.stdout.strip()).resolve()
        resolved = root.resolve()
        rel_base = Path(os.path.relpath(resolved, toplevel))
        out = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
                "--full-name",
                "--",
                ".",
            ],
            capture_output=True,
            timeout=60,
        )
        if out.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    files: list[str] = []
    for raw in out.stdout.split(b"\0"):
        if not raw:
            continue
        rel_to_root = os.path.relpath(raw.decode(errors="replace"), str(rel_base))
        if rel_to_root.startswith(".."):
            continue
        if any(_skipped(part) for part in Path(rel_to_root).parts):
            continue
        files.append(Path(rel_to_root).as_posix())
    return files or None


def _list_files(root: Path) -> tuple[list[str], list[str]]:
    """Repo-relative file list + skipped dir names, pruning as we go."""
    git = _git_files(root)
    if git is not None:
        return sorted(git), []
    files: list[str] = []
    skipped: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        kept = []
        for d in sorted(dirnames):
            if _skipped(d):
                skipped.append((rel_dir / d).as_posix())
            else:
                kept.append(d)
        dirnames[:] = kept
        for f in sorted(filenames):
            files.append((rel_dir / f).as_posix())
    return files, skipped


def _ancestors(rel_dir: str) -> list[str]:
    parts = [] if rel_dir == "." else rel_dir.split("/")
    out = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]
    return [*out, "."]


def _parent(rel_dir: str) -> str:
    if rel_dir == ".":
        return "."
    return _ancestors(rel_dir)[1]


def _strip_inner(rel_dir: str) -> str:
    """Climb from an anchor dir to the plausible service boundary area."""
    if rel_dir == ".":
        return "."
    parts = rel_dir.split("/")
    if "src" in parts:
        i = parts.index("src")
        parts = parts[:i] if i else []
    while parts and parts[-1].lower() in INNER_DIRS:
        parts.pop()
    return "/".join(parts) if parts else "."


def _is_under(path: str, root: str) -> bool:
    return root == "." or path == root or path.startswith(root + "/")


class ScanCancelled(Exception):
    """Raised when the cancelled event is set during a scan."""


class _Repo:
    """Everything the root scorer needs, gathered in one pass."""

    def __init__(
        self,
        root: Path,
        files: list[str],
        progress: Callable[[str, int, int], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> None:
        self.root = root
        self.files = files
        self._progress = progress
        self._cancelled = cancelled
        self.build_dirs: set[str] = set()
        self.bootstrap_dirs: set[str] = set()
        self.dirs_with_code: set[str] = set()
        self.dirs_with_bootstrap: set[str] = set()
        self.signal_dirs: dict[str, set[str]] = {}
        self.lang_counts: dict[str, Counter[str]] = {}
        self.specs: list[dict[str, Any]] = []
        self.client_dirs: list[str] = []
        self.factory_names: set[str] = set()
        self.v1api_packages: set[str] = set()
        self.code_files = 0
        self._gather()

    def _gather(self) -> None:
        for i, rel in enumerate(self.files, 1):
            if i % 100 == 0 or i == len(self.files):
                if self._cancelled is not None and self._cancelled.is_set():
                    raise ScanCancelled("scan cancelled")
                if self._progress is not None:
                    with contextlib.suppress(Exception):
                        self._progress("scan", i, len(self.files))
            p = Path(rel)
            d = p.parent.as_posix()
            name = p.name.lower()
            if name in BUILD_FILES or name.startswith("dockerfile"):
                self.build_dirs.add(d)
            if ("swagger" in name or "openapi" in name) and p.suffix in (".json", ".yaml", ".yml"):
                raw = (self.root / rel).read_bytes()
                title, valid = "", False
                try:
                    data = yaml.safe_load(raw.decode(errors="replace"))
                    if isinstance(data, dict) and ("swagger" in data or "openapi" in data):
                        valid = True
                        title = str((data.get("info") or {}).get("title") or "")
                except yaml.YAMLError:
                    pass
                self.specs.append(
                    {
                        "path": rel,
                        "dir": d,
                        "title": title,
                        "valid": valid,
                        "hash": hashlib.sha256(raw).hexdigest(),
                        "clientish": any(part.lower() in CLIENTISH_DIRS for part in p.parts[:-1]),
                    }
                )
            if p.suffix not in CODE_SUFFIXES:
                continue
            self.code_files += 1
            if (
                "generated" in rel.lower()
                or "client" in p.parent.name.lower()
                or p.parent.name.lower().endswith("cli")
            ):
                self.client_dirs.append(d)
            for anc in _ancestors(d):
                self.dirs_with_code.add(anc)
            lang = LANG_BY_SUFFIX[p.suffix]
            for anc in _ancestors(d):
                self.lang_counts.setdefault(anc, Counter())[lang] += 1
            try:
                text = (self.root / rel).read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                continue
            if "Swagger Codegen" in text or "class ApiClient" in text:
                self.client_dirs.append(d)
            if p.suffix == ".py":
                self.factory_names.update(m.group(1) for m in _FACTORY_CALL_RE.finditer(text))
                self.v1api_packages.update(m.group(1) for m in _V1API_CALL_RE.finditer(text))
            for fw, signals in FRAMEWORK_SIGNALS.items():
                if any(s in text for s in signals):
                    self.signal_dirs.setdefault(d, set()).add(fw)
            for boot in BOOTSTRAP_SIGNALS.values():
                if any(s in text for s in boot):
                    self.bootstrap_dirs.add(d)
                    for anc in _ancestors(d):
                        self.dirs_with_bootstrap.add(anc)
                    break
        # Sibling signal: parents with several boundary-marked children
        # look like umbrella dirs (services/, apps/, teams' own names).
        marked = self.build_dirs | {_strip_inner(s["dir"]) for s in self.specs}
        counts: Counter[str] = Counter(_parent(d) for d in marked if d != ".")
        self.busy_parents = {p for p, n in counts.items() if n >= 3}

    def infer_root(self, anchor_dir: str) -> tuple[str, str]:
        """Score the anchor's ancestors; return (root, runner-up or '')."""
        stripped = _strip_inner(anchor_dir)
        scored: list[tuple[float, str]] = []
        for depth, anc in enumerate(_ancestors(stripped)):
            score = -float(depth)
            if depth == 0:
                score += 2
            if anc in self.build_dirs:
                score += 4
            if anc in self.dirs_with_bootstrap:
                score += 3
            if anc in self.dirs_with_code:
                score += 1
            parent = _parent(anc) if anc != "." else "."
            if parent.split("/")[-1].lower() in UMBRELLA_DIRS or parent in self.busy_parents:
                if parent != anc:
                    score += 3
            if anc.split("/")[-1].lower() in UMBRELLA_DIRS:
                score -= 4
            if anc == "." and stripped != ".":
                score -= 5
            scored.append((score, anc))
        scored.sort(key=lambda t: (-t[0], -len(t[1])))
        best_score, best = scored[0]
        runner = ""
        if len(scored) > 1 and scored[1][1] != best and best_score - scored[1][0] <= 1:
            runner = scored[1][1]
        return best, runner


def scan_repo(
    repo_root: str | Path,
    progress: Callable[[str, int, int], None] | None = None,
    cancelled: threading.Event | None = None,
) -> OnboardReport:
    root = Path(repo_root)
    files, skipped_dirs = _list_files(root)
    repo = _Repo(root, files, progress=progress, cancelled=cancelled)
    ambiguities: list[str] = []
    notes: list[str] = []

    # Candidates keyed by inferred root. Strong anchors (spec/bootstrap)
    # survive nesting; weak ones (signal clusters, lone build files under
    # an umbrella) collapse into a stronger or shallower neighbour.
    cand: dict[str, dict[str, Any]] = {}

    def propose(root_dir: str, strong: bool, why: str, title: str = "") -> None:
        c = cand.setdefault(
            root_dir, {"strong": False, "evidence": set(), "title": "", "specs": []}
        )
        c["strong"] = c["strong"] or strong
        c["evidence"].add(why)
        if title and not c["title"]:
            c["title"] = title

    def anchor(d: str, strong: bool, why: str, title: str = "") -> str:
        root_dir, runner = repo.infer_root(d)
        propose(root_dir, strong, why, title)
        if runner:
            ambiguities.append(
                f"Service root for {why}: drafted {root_dir}; {runner} scored nearly "
                "as high — confirm the boundary."
            )
        return root_dir

    # Specs: drop client/test copies and content duplicates first.
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for s in repo.specs:
        by_hash.setdefault(s["hash"], []).append(s)
    usable_specs: list[dict[str, Any]] = []
    for group in by_hash.values():
        group.sort(key=lambda s: (s["clientish"], len(s["path"]), s["path"]))
        head, rest = group[0], group[1:]
        for dup in rest:
            ambiguities.append(
                f"Duplicate spec {dup['path']} (identical to {head['path']}); "
                "not drafted as a separate service."
            )
        if head["clientish"]:
            ambiguities.append(
                f"Spec under a client/test tree, not drafted as a service: {head['path']}. "
                "Declare it as a client spec if some service consumes it."
            )
        else:
            usable_specs.append(head)

    for s in usable_specs:
        root_dir = anchor(s["dir"], True, f"spec {s['path']}", s["title"])
        cand[root_dir]["specs"].append(s["path"])
        if not s["valid"]:
            ambiguities.append(
                f"Spec {s['path']} does not parse as OpenAPI/Swagger; "
                "service drafted from its location only."
            )
    suppressed = 0
    for d in sorted(repo.bootstrap_dirs):
        if any(part.lower() in CLIENTISH_DIRS for part in d.split("/")):
            suppressed += 1
            continue
        anchor(d, True, f"app bootstrap under {d}")
    for d in sorted(repo.signal_dirs):
        if any(part.lower() in CLIENTISH_DIRS for part in d.split("/")):
            suppressed += 1
            continue
        anchor(d, False, f"framework signals under {d}")
    if suppressed:
        notes.append(
            f"Ignored {suppressed} bootstrap/signal anchor(s) under client/test/fixture trees."
        )
    for d in sorted(repo.build_dirs):
        if any(part.lower() in CLIENTISH_DIRS for part in d.split("/")):
            continue
        parent = _parent(d)
        if d in repo.dirs_with_code and (
            parent.split("/")[-1].lower() in UMBRELLA_DIRS or parent in repo.busy_parents
        ):
            propose(d, False, f"build marker in {d}")

    strong_roots = sorted(r for r, c in cand.items() if c["strong"])
    chosen = list(strong_roots)
    weak = sorted(
        (r for r, c in cand.items() if not c["strong"]),
        key=lambda s: (s.count("/"), s),
    )
    for r in weak:
        under_strong = any(r != s and _is_under(r, s) for s in strong_roots)
        over_strong = any(s != r and _is_under(s, r) for s in strong_roots)
        under_weak = any(r != w and _is_under(r, w) for w in chosen if w not in strong_roots)
        if not (under_strong or over_strong or under_weak):
            chosen.append(r)
    chosen.sort()
    if not chosen:
        chosen = ["."]
        cand["."] = {"strong": True, "evidence": {"fallback"}, "title": "", "specs": []}
        ambiguities.append(
            "No framework signals, specs, or bootstraps found; drafted one repo-wide "
            "service. Split it manually if this repo hosts several services."
        )

    def own_frameworks(r: str) -> set[str]:
        fws: set[str] = set()
        deeper = [c for c in chosen if c != r and _is_under(c, r)]
        for d, found in repo.signal_dirs.items():
            if _is_under(d, r) and not any(_is_under(d, c) for c in deeper):
                fws |= found
        return fws

    services: list[ServiceDecl] = []
    evidence: dict[str, list[str]] = {}
    used_ids: set[str] = set()
    for r in chosen:
        info = cand[r]
        title = info["title"]
        base = _slug(title or (root.name if r == "." else Path(r).name))
        sid, n = base, 2
        while sid in used_ids:
            sid = f"{base}-{n}"
            n += 1
        used_ids.add(sid)
        fws = own_frameworks(r)
        fw = sorted(fws)[0] if len(fws) == 1 else "auto"
        if len(fws) > 1:
            ambiguities.append(
                f"{r}: multiple framework signals {sorted(fws)}; set framework manually."
            )
        langs = repo.lang_counts.get(r, Counter())
        lang = langs.most_common(1)[0][0] if langs else ""
        if len([ln for ln, cnt in langs.items() if cnt]) > 1:
            lang = "mixed"
        services.append(
            ServiceDecl(
                id=sid,
                name=title or sid.replace("-", " ").title(),
                roots=[r],
                language=lang,
                framework=fw,
                specs=sorted(info["specs"]),
            )
        )
        evidence[sid] = sorted(info["evidence"])

    # Generated/disconnected clients outside any drafted service: attach
    # to the first service as a draft for human completion.
    if services:
        draft_dirs = sorted(set(repo.client_dirs))[:5]
        for cd in draft_dirs:
            if any(_is_under(cd, s.roots[0]) for s in services):
                continue
            slug = _slug(Path(cd).name)
            factories = [f for f in sorted(repo.factory_names) if _factory_matches(f, slug)]
            if not factories and len(draft_dirs) == 1:
                factories = sorted(repo.factory_names)
            packages = [
                p for p in sorted(repo.v1api_packages) if _factory_matches(p, slug, package=True)
            ]
            services[0].clients.append(
                ClientDecl(
                    id=slug,
                    paths=[cd],
                    packages=packages,
                    factories=factories,
                    target_service="",
                    spec="",
                )
            )
            if factories:
                ambiguities.append(
                    f"Possible generated client at {cd}: drafted factories {factories} "
                    "from settings-factory call sites; verify each returns this client, "
                    "then set target_service/spec (or delete the draft entry)."
                )
            else:
                ambiguities.append(
                    f"Possible generated client at {cd}: set target_service/spec "
                    "in axon.yaml (or delete the draft entry). If consumers get it "
                    "from a settings factory (e.g. settings.GET_PAYMENTS_API), list that "
                    "dotted name under the client's factories: so call sites resolve."
                )
    assigned = {sp for s in services for sp in s.specs}
    for s in repo.specs:
        if s["path"] not in assigned and not s["clientish"]:
            ambiguities.append(f"Spec not owned by any drafted service: {s['path']}")

    sm = discover_clients(root, ServiceMap(repo=root.resolve().name, services=services), files)
    notes.extend(suggest_base_urls(root, sm, files))
    services = sm.services
    notes.append(f"Scanned {repo.code_files} code files; drafted {len(services)} service(s).")
    if skipped_dirs:
        shown = ", ".join(skipped_dirs[:8])
        notes.append(f"Skipped {len(skipped_dirs)} hidden/vendor dir(s): {shown}.")
    notes.append("Review service ids, roots, and client targets, then commit via PR.")
    return OnboardReport(
        draft_yaml=dump_service_map(sm),
        services=[s.model_dump() for s in services],
        ambiguities=ambiguities,
        notes=notes,
        evidence=evidence,
    )
