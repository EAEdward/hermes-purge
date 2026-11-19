"""验证 DEEP_PATCHES 的 needle 是否命中真实 Hermes 源码（只读，不写）。

移植后可移植性修复（2026-11-19）：
- 去掉原作者硬编码的 Windows 路径（C:\\Users\\... / E:\\codex\\...）。
- 用 HERMES_SRC / HERMES_HOME / PLUGIN_DIR 环境变量定位源码与插件根；
- 默认按 Hermes 官方 HERMES_HOME 解析，容器内常见路径 /opt/hermes 亦可。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR") or Path(__file__).resolve().parent.parent)

def _resolve_root() -> Path:
    env = os.environ.get("HERMES_SRC")
    if env:
        return Path(env).expanduser().resolve()
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        home = Path(get_hermes_home())
        for cand in (home / "hermes-agent", home):
            if (cand / "agent" / "prompt_builder.py").is_file():
                return cand
    except Exception:
        pass
    for cand in (Path("/opt/hermes"), PLUGIN_DIR.parent):
        if (cand / "agent" / "prompt_builder.py").is_file():
            return cand
    raise SystemExit("找不到 Hermes 源码树：请设置 HERMES_SRC=/path/to/hermes-agent")

ROOT = _resolve_root()
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("hp_core_probe", PLUGIN_DIR / "lib" / "core.py")
core = importlib.util.module_from_spec(spec)
sys.modules["hp_core_probe"] = core
fake = types.ModuleType("hermes_constants")
fake.get_hermes_home = lambda: PLUGIN_DIR.parent
sys.modules.setdefault("hermes_constants", fake)
spec.loader.exec_module(core)

print(f"source root: {ROOT}")
ok = True
for p in core.DEEP_PATCHES:
    fp = ROOT.joinpath(*p.rel)
    if not fp.is_file():
        print(f"[{p.id}] MISSING_FILE {p.rel}")
        ok = False
        continue
    text = fp.read_text(encoding="utf-8")
    hit = p.needle in text
    new = text.replace(p.needle, p.replacement, 1)
    changed = new != text
    has_marker = p.marker in new
    print(f"[{p.id}] {p.name}: file={fp.name} needle_hit={hit} will_change={changed} marker_after={has_marker}")
    ok = ok and hit and changed and has_marker

print("RESULT:", "ALL_OK" if ok else "DRIFT (源码结构已变化，deepPatch 需更新 needle)")
raise SystemExit(0 if ok else 1)
