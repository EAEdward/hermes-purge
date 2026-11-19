"""preset 长度守卫：注入段上限 4000 字符/节，超了会被静默丢弃尾部。

2026-11-19 实测：Hermes 对 system_prompt_section 每节 4000 字符、总量 8000 字符，
超预算的节会被整节或尾部丢弃。presets/*.md 是最容易被编辑膨胀的文件，
所以这里加一条硬约束（留 300 字符余量给插件自己的 banner/前缀）。
"""
from pathlib import Path

MAX_SECTION_CHARS = 4000
SAFETY_MARGIN = 300
PRESETS = Path(__file__).resolve().parent.parent / "presets"


def test_presets_fit_section_budget():
    files = sorted(PRESETS.glob("*.md"))
    assert files, "presets/ 下没有任何 md 文件"
    for f in files:
        text = f.read_text(encoding="utf-8")
        limit = MAX_SECTION_CHARS - SAFETY_MARGIN
        assert len(text) <= limit, f"{f.name} 有 {len(text)} 字符，超过 {limit}（会被截断）"


if __name__ == "__main__":
    test_presets_fit_section_budget()
    print("preset budget OK")
