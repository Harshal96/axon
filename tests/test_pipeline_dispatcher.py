from axon.pipeline.dispatcher import WorkerKind, determine_workers


def test_dispatcher_empty_changed_files():
    # Empty changed_files → full reindex plan
    workers = determine_workers([])
    assert workers == {
        WorkerKind.BAZEL,
        WorkerKind.TREESITTER_PYTHON,
        WorkerKind.TREESITTER_JAVA,
        WorkerKind.SWAGGER,
        WorkerKind.MAVEN,
        WorkerKind.PNPM,
    }


def test_dispatcher_build_file_change():
    # BUILD file change → BazelWorker only
    workers = determine_workers(["path/to/BUILD"])
    assert workers == {WorkerKind.BAZEL}


def test_dispatcher_swagger_json_change():
    # swagger.json change → BazelWorker + SwaggerWorker
    workers = determine_workers(["svc/swagger.json"])
    assert workers == {WorkerKind.BAZEL, WorkerKind.SWAGGER}


def test_dispatcher_mixed_python_and_build():
    # Mixed .py + BUILD → BazelWorker + TreeSitterWorker[Python]
    workers = determine_workers(["app/main.py", "app/BUILD"])
    assert workers == {WorkerKind.BAZEL, WorkerKind.TREESITTER_PYTHON}


def test_dispatcher_pnpm_change():
    workers = determine_workers(["package.json"])
    assert workers == {WorkerKind.PNPM}


def test_dispatcher_maven_change():
    workers = determine_workers(["java/pom.xml"])
    assert workers == {WorkerKind.MAVEN}
