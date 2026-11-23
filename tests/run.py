#!/usr/bin/env python3
"""零依赖测试入口：python3 tests/run.py

为什么不用 pytest：插件根目录自带 __init__.py（Hermes 插件包，必须存在），
pytest 的 prepend/importlib 两种模式都会把根 __init__.py 当测试模块收集，
而它内部是相对导入（`from .lib import ...`），必然 ImportError。
本机/容器里跑测试不该为此装依赖或改目录名，所以直接用 stdlib 跑。
requires-python: 3.9+
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

MODULES = ["test_refusal.py", "test_preset_budget.py", "test_doctor.py"]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = mod
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    if len(sys.argv) > 1:
        targets = [HERE / f for f in sys.argv[1:]]
    else:
        targets = [HERE / m for m in MODULES if (HERE / m).is_file()]
    passed = failed = 0
    failures = []
    for path in targets:
        mod = load(path)
        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            fn = getattr(mod, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"PASS {path.name}::{name}")
            except Exception as exc:
                failed += 1
                failures.append(f"{path.name}::{name}")
                print(f"FAIL {path.name}::{name}: {exc}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("failures:", ", ".join(failures))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
