"""Deterministically generate a synthetic monorepo for perf work.

Usage: python tests/perf/gen_monorepo.py OUT SERVICES FILES_PER_SERVICE FUNCS_PER_FILE

Presets (services x files x funcs):
  small              10 x 10 x 10
  synth-mono2        50 x 20 x 80   (~162k nodes, ~968k edges)
  large              200 x 20 x 40  (~160k symbols over 4,000 files)
  huge-file-count    400 x 100 x 2  (40,000 files, few symbols each)

Generated output must never be committed.
"""

from __future__ import annotations

import pathlib
import sys


def main() -> None:
    out = pathlib.Path(sys.argv[1])
    services, files_per, funcs = int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    decls = []
    for s in range(services):
        d = out / f"services/svc{s:03d}"
        d.mkdir(parents=True, exist_ok=True)
        decls.append(
            f"  - id: svc{s:03d}\n    roots: [services/svc{s:03d}]\n"
            "    language: python\n    framework: fastapi\n"
        )
        for f in range(files_per):
            lines = ["from fastapi import FastAPI", "app = FastAPI()", ""]
            for k in range(funcs):
                lines.append(f"def fn_{f}_{k}(x):")
                lines.append(f"    y = helper_{f}_{k}(x) + fn_{(f + 1) % files_per}_{k}(x)")
                lines.append("    return y")
                lines.append(f"def helper_{f}_{k}(x):")
                lines.append("    return x + 1")
                lines.append("")
            lines.append("@app.get('/items')")
            lines.append("def items():")
            lines.append("    return fn_0_0(1)")
            (d / f"mod{f}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "axon.yaml").write_text(
        "version: 1\nrepo: synth\nservices:\n" + "".join(decls), encoding="utf-8"
    )
    print(f"{services} services, {services * files_per} files at {out}")


if __name__ == "__main__":
    main()
