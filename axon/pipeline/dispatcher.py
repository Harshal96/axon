from dataclasses import dataclass, field
from enum import StrEnum


class WorkerKind(StrEnum):
    BAZEL = "BAZEL"
    TREESITTER_PYTHON = "TREESITTER_PYTHON"
    TREE_SITTER_PY = "TREESITTER_PYTHON"
    TREESITTER_JAVA = "TREESITTER_JAVA"
    TREE_SITTER_JAVA = "TREESITTER_JAVA"
    SWAGGER = "SWAGGER"
    MAVEN = "MAVEN"
    PNPM = "PNPM"
    TREESITTER_JS = "TREESITTER_JS"
    TREE_SITTER_TS = "TREESITTER_JS"


@dataclass
class DispatchPlan:
    workers: list[WorkerKind]
    is_full_reindex: bool = False
    bazel_packages: list[str] = field(default_factory=list)
    python_files: list[str] = field(default_factory=list)
    java_files: list[str] = field(default_factory=list)
    swagger_files: list[str] = field(default_factory=list)
    pom_files: list[str] = field(default_factory=list)
    pnpm_lockfiles: list[str] = field(default_factory=list)


WORKER_ORDER = [
    WorkerKind.BAZEL,
    WorkerKind.TREESITTER_PYTHON,
    WorkerKind.TREESITTER_JAVA,
    WorkerKind.TREESITTER_JS,
    WorkerKind.SWAGGER,
    WorkerKind.MAVEN,
    WorkerKind.PNPM,
]


def _bazel_package(path: str) -> str:
    if path.endswith(("BUILD", "BUILD.bazel")):
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
    else:
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
    return f"//{directory}" if directory else "//"


def dispatch(changed_files: list[str]) -> DispatchPlan:
    is_full = not changed_files
    worker_set: set[WorkerKind] = set(WORKER_ORDER) if is_full else set()
    plan = DispatchPlan(workers=[], is_full_reindex=is_full)

    for path in changed_files:
        name = path.rsplit("/", 1)[-1]
        if name in {"BUILD", "BUILD.bazel", "WORKSPACE"} or path.endswith(".bzl"):
            worker_set.add(WorkerKind.BAZEL)
            plan.bazel_packages.append(_bazel_package(path))
        elif path.endswith(".py"):
            worker_set.add(WorkerKind.TREESITTER_PYTHON)
            plan.python_files.append(path)
        elif path.endswith(".java"):
            worker_set.add(WorkerKind.TREESITTER_JAVA)
            plan.java_files.append(path)
        elif path.endswith(("swagger.json", "openapi.yaml", "openapi.yml")):
            worker_set.add(WorkerKind.SWAGGER)
            worker_set.add(WorkerKind.BAZEL)
            plan.swagger_files.append(path)
            plan.bazel_packages.append(_bazel_package(path))
        elif name == "pom.xml":
            worker_set.add(WorkerKind.MAVEN)
            plan.pom_files.append(path)
        elif name in {"package.json", "pnpm-lock.yaml"}:
            worker_set.add(WorkerKind.PNPM)
            if name == "pnpm-lock.yaml":
                plan.pnpm_lockfiles.append(path)
        elif path.endswith((".ts", ".tsx", ".js", ".jsx")):
            worker_set.add(WorkerKind.TREESITTER_JS)

    plan.workers = [worker for worker in WORKER_ORDER if worker in worker_set]
    plan.bazel_packages = sorted(set(plan.bazel_packages))
    return plan


def determine_workers(changed_files: list[str]) -> set[WorkerKind]:
    return set(dispatch(changed_files).workers)
