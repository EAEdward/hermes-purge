"""hermes-purge — Hermes 指令权威性清洗插件（移植自 dsh-purge）。

入口：plugin.yaml 清单 + register(ctx)。

能力：
  1. system_prompt_section 注入（banner + hermes-inject.md + 激活规则）
  2. /purge 与 /purge-rules 斜杠命令
  3. purge_status / purge_apply / purge_revert 模型工具
  4. hermes purge 子命令（register_cli_command）
  5. 启动自动应用（on_session_start hook 异步）+ 审批配置重写
  6. deepPatch 深度清洗（可选，默认关）
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from .lib import boost, core, refusal
from .lib import inject

logger = logging.getLogger(__name__)

name = core.PLUGIN_ID

# 运行时探针：记录本进程真正注册成功的通道（doctor 据此判断"装了但没生效"）
_RUNTIME: Dict[str, Any] = {"middleware": [], "hooks": [], "sections": []}


# ── 状态渲染 ─────────────────────────────────────────────────────

def _line(mark: str, title: str, value: Any, ok: Optional[bool] = None) -> str:
    if ok is None:
        mark = mark or "·"
    elif ok:
        mark = "✓"
    else:
        mark = "✗"
    return f"{mark} {title}: {value}"


def render_status(state: Dict[str, Any]) -> str:
    out = ["hermes-purge 状态 / Status"]
    out.append(_line("·", "Hermes home", state["hermes_home"]))
    out.append(_line("·", "安装根 / install root",
                     state.get("install_root") or "未定位（设 HERMES_INSTALL_DIR 或 deepPatchTargets）"))
    try:
        _cfg = core.plugin_config()
        _nonce = boost.read_probe(core)
        out.append(_line("✓" if boost.enabled(_cfg) else "·", "尾部 boost",
                         "开启（llm_request 末尾注入）" if boost.enabled(_cfg) else "关闭"))
        if _nonce:
            out.append(_line("✓", "运行时探针", f"活跃 nonce={_nonce}"))
    except Exception:
        pass
    out.append(_line("✓" if state["override_exists"] else "✗",
                     "override 文件", f"{state['override_path']} (非空={state['override_nonempty']})"))
    out.append(_line("✓" if state["config_rewritten"] else "·",
                     "approvals 配置重写", "已标记" if state["config_rewritten"] else "未标记"))
    out.append(_line("·", "approvals mode", state["approvals_mode"]))
    out.append("")
    out.append(f"深度清洗 deep patches: {state['deep_patches_applied']}/{state['deep_patches_total']} applied, "
               f"{state['deep_patches_pending']} pending")
    for spec_id in sorted(state["patch_status"], key=int):
        s = state["patch_status"][spec_id]
        mark = "✓" if s in ("applied", "already") else "✗" if s in ("pending",) else "·"
        out.append(f"  {mark} [#{spec_id}] {s}")
    if any(v == "permission_denied" for v in state["patch_status"].values()):
        out.append("  ⚠ 源码树不可写（权限不足），修复命令：")
        for c in core.permission_fix_commands():
            out.append(f"    {c}")
    out.append("")
    out.append(_line("·", "规则集目录", state["rules_dir"]))
    if state["rules"]:
        for r in state["rules"]:
            active = "★" if r["id"] == state["active_rule"] else "·"
            out.append(f"  {active} {r['name']} ({r['id']}) [{r['target']}] ({_fmt_size(r['size'])})")
    else:
        out.append("  (无规则 — /purge-rules create <id> 或设置页新建)")
    return "\n".join(out)


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    kb = size / 1024
    return f"{round(kb)} KB" if kb >= 100 else f"{kb:.1f} KB"


# ── 自检 / doctor ────────────────────────────────────────────────
# 把「装好了但没生效」的常见原因一次列全：通道是否注册、boost 从哪来、
# SOUL 体量、approvals 是否真被改写、deep patch 为什么打不上、采样参数是否
# 把输出预算烧空、拒绝纠正是否在跑。

def _soul_report(home: str) -> tuple:
    """SOUL 体量体检。中文一个字占 3 字节，但 Hermes 的 section 预算按**字符**算，
    所以两个都报，判定用字符（阈值按中英混排的实际密度定）。
    """
    from pathlib import Path as _P
    p = _P(home) / "SOUL.md"
    if not p.is_file():
        return ("✗", "SOUL.md", "缺失（身份只来自 hermes-inject.md，效果会明显变弱）", False)
    try:
        raw = p.read_bytes()
    except Exception as e:
        return ("✗", "SOUL.md", f"读取失败: {e}", False)
    chars = len(raw.decode("utf-8", "replace"))
    size = f"{chars} 字符 / {len(raw)} 字节"
    if chars < 1200:
        return ("⚠", "SOUL.md", f"{size}（偏薄：身份条款带得越短，尾部 boost 越容易被当成外部建议）", None)
    if chars > 9000:
        return ("⚠", "SOUL.md", f"{size}（偏厚：注意和其他 system_prompt_section 抢 8000 字符预算）", None)
    return ("✓", "SOUL.md", size, True)


def _sampling_report(cfg: Dict[str, Any]) -> list:
    out = []
    # reasoning_effort
    effort = str(((cfg.get("agent") or {}).get("reasoning_effort")) or "").strip().lower()
    if effort in ("high", "xhigh", "max", "ultra"):
        out.append(("⚠", "reasoning_effort", f"{effort} — 实测：思考会吃掉输出预算，长清单场景容易变成空答复"))
    elif effort in ("minimal", "none", "false", "disabled"):
        out.append(("✗", "reasoning_effort", f"{effort} — 上游返回 400（不支持的档位）"))
    elif effort:
        out.append(("✓", "reasoning_effort", f"{effort}（显式设置）"))
    else:
        out.append(("·", "reasoning_effort", "未设置（走提供商默认，不发该字段）"))
    # temperature（各 provider 的 extra_body）
    temps = []
    for pname, pcfg in (cfg.get("providers") or {}).items():
        if isinstance(pcfg, dict):
            body = pcfg.get("extra_body") if isinstance(pcfg.get("extra_body"), dict) else {}
            if "temperature" in body:
                temps.append(f"{pname}={body['temperature']}")
            elif "temperature" in pcfg:
                temps.append(f"{pname}={pcfg['temperature']}")
    if temps:
        out.append(("✓", "temperature", ", ".join(temps) + "（显式设置）"))
    else:
        out.append(("·", "temperature", "未设置（走提供商默认，不发该字段）"))
    return out


def render_doctor() -> str:
    raw = core.read_config()          # 全量配置：agent / providers / approvals 都在这里
    cfg = core.plugin_config(raw)     # 插件自己的配置（enabled / boost / refusalRetry ...）
    st = core.gather_state()
    home = st["hermes_home"]
    rows = []  # (mark, name, detail, ok_or_none)

    rows.append(("✓" if cfg.get("enabled", True) else "✗", "插件启用",
                 "enabled=true" if cfg.get("enabled", True) else "enabled=false（整个插件不工作）",
                 bool(cfg.get("enabled", True))))

    mw = _RUNTIME.get("middleware", [])
    rows.append(("✓" if "llm_request" in mw else "✗", "注入通道 llm_request",
                 f"已注册 {mw}" if mw else "未注册（尾部 boost / 拒绝纠正全部失效）",
                 "llm_request" in mw))
    hooks = _RUNTIME.get("hooks", [])
    rows.append(("✓" if "pre_verify" in hooks else "·", "回合停门 pre_verify",
                 f"已注册 {hooks}" if hooks else "未注册（只在改文件回合能真重试）",
                 None))

    try:
        fp = boost.fingerprint(cfg, home=__import__("pathlib").Path(home))
        rows.append(("✓" if cfg.get("boost", {}).get("enabled", True) else "·", "尾部 boost",
                     f"{fp['source']} · {fp['chars']} 字符 · sha1={fp['sha1']}", None))
    except Exception as e:
        rows.append(("✗", "尾部 boost", f"生成失败: {e}", False))

    nonce = boost.read_probe(core)
    rows.append(("✓" if nonce else "·", "运行时探针",
                 f"活跃 nonce={nonce}" if nonce else "未启用（hermes purge probe 写一个，回答里出现 nonce 即链路通）",
                 None))

    rows.append(_soul_report(home))

    ov = st["override_path"]
    rows.append(("✓" if st["override_nonempty"] else "✗", "override 文件",
                 f"{ov} (非空={st['override_nonempty']})" if st["override_nonempty"]
                 else f"{ov} 为空/缺失（hermes purge apply 或 /purge write <文本> 初始化）",
                 True if st["override_nonempty"] else False))

    rows.append(("✓" if st["config_rewritten"] else "·", "approvals 重写",
                 f"已标记，mode={st['approvals_mode']}" if st["config_rewritten"]
                 else f"未标记，mode={st['approvals_mode']}", None))

    install = st.get("install_root")
    rows.append(("✓" if install else "·", "安装根 install root",
                 str(install) if install else "未定位（仅 deepPatch 需要：设 HERMES_INSTALL_DIR）", None))
    denied = [v for v in st["patch_status"].values() if v == "permission_denied"]
    rows.append(("⚠" if denied else "✓", "deep patches",
                 f"{st['deep_patches_applied']}/{st['deep_patches_total']} applied"
                 + (f"，{len(denied)} 个 permission_denied" if denied else ""), None))

    for mark, name, detail in _sampling_report(raw):
        rows.append((mark, name, detail, None))

    rc = cfg.get("refusalRetry") or {}
    stats = refusal.audit_stats(core)
    rows.append(("✓" if refusal.enabled(cfg) else "·", "拒绝纠正",
                 f"enabled={refusal.enabled(cfg)} maxAttempts={refusal.max_attempts(cfg)} "
                 f"headWindow={refusal._head_window(cfg)} · 审计 {stats['lines']} 行"
                 f"（{', '.join(f'{k}={v}' for k, v in stats['events'].items()) or '空'}）", None))

    out = ["hermes-purge 自检 / Doctor"]
    for mark, name, detail, _ok in rows:
        out.append(f"{mark} {name}: {detail}")

    fix = core.permission_fix_commands()
    if fix:
        out.append("")
        out.append("修复 deep patch 只读（源码树属 root）：")
        for c in fix:
            out.append(f"  {c}")

    bad = [r for r in rows if r[3] is False]
    warn = [r for r in rows if r[0] == "⚠"]
    out.append("")
    out.append(f"结论: {len(rows) - len(bad) - len(warn)} ok, {len(warn)} warn, {len(bad)} fail")
    if bad:
        out.append("必须先修失败项，否则尾部 boost 这段可能根本没送到模型。")
    return "\n".join(out)


# ── 自动应用 ─────────────────────────────────────────────────────

def _auto_apply(cfg: Dict[str, Any]) -> str:
    try:
        core.ensure_override_content(fill_default=True)
        core.ensure_initial_state()
        if cfg.get("approvals", {}).get("rewriteConfig", True):
            result = core.apply_approval_config()
            logger.info("hermes-purge: approvals rewrite → %s", result.get("status"))
        if cfg.get("deepPatch") and core.hermes_install_root():
            if cfg.get("autoApplyOnStart", True):
                report = core.apply_patches()
                applied = [r for r in report if r["status"] in ("applied", "already")]
                logger.info("hermes-purge: deep patches → %d ok, %d skip",
                            len(applied), len(report) - len(applied))
                _maybe_auto_revert_missing(cfg, report)
        return "clean"
    except Exception as e:
        logger.warning("hermes-purge: auto-apply error: %s", e)
        return f"error:{e}"


def _maybe_auto_revert_missing(cfg: Dict[str, Any], report) -> None:
    """autoRevertOnMissing=true 且任一目标文件缺失（升级覆盖/被删）时整体回滚，
    避免半挂状态（部分补丁 applied、部分 missing）。"""
    if not cfg.get("autoRevertOnMissing"):
        return
    missing = [r for r in report if r["status"] in ("missing_file", "pattern_not_found")]
    if not missing:
        return
    reverted, errors = core.revert_patches()
    logger.warning("hermes-purge: autoRevertOnMissing → %d targets missing, reverted %d (errors=%d)",
                   len(missing), len(reverted), len(errors))


# ── 命令处理 ─────────────────────────────────────────────────────

def _handle_purge(raw_args: str = "", _cfg: Optional[Dict[str, Any]] = None) -> str:
    args = (raw_args or "").strip().split()
    sub = (args[0] if args else "status").lower()
    cfg = _cfg if _cfg is not None else core.plugin_config()

    if sub in ("status", "s"):
        return render_status(core.gather_state())
    if sub in ("doctor", "doc", "check", "diag"):
        return render_doctor()
    if sub in ("apply", "a"):
        # 全量：override 初始化 + 审批重写 + deep patches
        core.ensure_override_content(fill_default=True)
        core.ensure_initial_state()
        approval = core.apply_approval_config(force=True)
        lines = [f"审批配置: {approval.get('status')} {approval.get('changed') or ''}"]
        if cfg.get("deepPatch") and core.hermes_install_root():
            bak = core.backup_all()
            report = core.apply_patches()
            lines.append("deep patches:")
            for r in report:
                mark = "✓" if r["status"] in ("applied", "already") else \
                       "⚠" if r["status"] == "missing_file" else "✗"
                lines.append(f"  {mark} [#{r['id']}] {r['name']} — {r['status']}")
            if bak:
                lines.append(f"备份: {len(bak)} 个")
        else:
            lines.append("deepPatch 未开启（默认方案走官方配置+注入，无需补源码）")
        lines.append("完成。新会话生效（system_prompt_section 随会话重建）。")
        return "\n".join(lines)
    if sub in ("probe", "p"):
        nonce = boost.create_probe(core)
        return (f"探针已写入: {nonce}（30 分钟内有效）\n"
                f"尾部 boost 会附带一段「看到就回显 nonce」的指令；\n"
                f"模型答出 `{nonce}` = 注入链路真的到达模型。\n"
                f"清除: /purge probe off")
    if sub in ("probe-clear", "probe-off"):
        boost.clear_probe(core)
        return "探针已清除。"
    if sub in ("revert", "r"):
        reverted, errors = core.revert_patches()
        res = core.apply_approval_config(force=False)
        # revert 只还原 deep patches；approvals/override/rules 是用户数据，保持原样。
        lines = []
        if reverted:
            lines.append(f"deep patches 已还原: {len(reverted)} 个")
        for pid, err in errors:
            lines.append(f"⚠ 还原失败 #{pid}: {err}")
        if not reverted and not errors:
            lines.append("无 deep patch 备份可还原（未启用或从未打过）")
        lines.append("approvals 状态: " + str(res.get("status")) + "（保持当前值，revert 不还原组态）")
        lines.append("注: hermes-inject.md / rules / approvals 是配置/用户数据，revert 保留。")
        return "\n".join(lines)
    if sub in ("edit", "e"):
        ov = core.override_path()
        core.ensure_override_content(fill_default=True)
        return (f"override 文件: {ov}\n"
                f"编辑后重启 Hermes（或新会话）生效。\n"
                f"也可用 /purge write <text> 直接写入。")
    if sub in ("write", "w"):
        body = (raw_args or "").split(maxsplit=1)
        if len(body) < 2:
            return "用法: /purge write <注入内容>"
        ov = core.override_path()
        ov.parent.mkdir(parents=True, exist_ok=True)
        ov.write_text(body[1].strip(), encoding="utf-8")
        return f"已写入 {ov}（{len(body[1].strip())} 字符）。新会话生效。"
    if sub in ("identity",):
        ov = core.override_path()
        core.ensure_override_content(fill_default=True)
        return (f"身份定义来自 {ov}（和用户 SOUL.md/AGENTS.md）。\n"
                f"插件不发明第二身份；hermes-inject.md 即身份。")
    return ("hermes-purge 命令：\n"
            "  /purge status        显示状态\n"
            "  /purge doctor        自检（通道/boost/SOUL/采样参数/权限一次列全）\n"
            "  /purge apply         应用清洗（override 初始化 + approvals 重写 + 可选 deep patch）\n"
            "  /purge revert        回滚 deep patches\n"
            "  /purge write <文本>   直接写入 override 注入文件\n"
            "  /purge edit          打开 override 文件（路径打印）\n"
            "  /purge identity      查看身份定义来源\n"
            "规则集见 /purge-rules（list | use <id> | create | edit | delete | reset）")


def _handle_rules(raw_args: str = "") -> str:
    args = (raw_args or "").strip().split()
    sub = (args[0] if args else "list").lower()

    if sub in ("list", "ls", "s"):
        st = core.rules_status()
        out = ["规则集 / Rule Sets", f"  目录: {st['rules_dir']}"]
        if not st["rules"]:
            out.append("  (无规则 — /purge-rules create <id>)")
        for r in st["rules"]:
            active = "▶" if r["id"] == st["active"] else "·"
            out.append(f"  {active} {r['name']} ({r['id']}) [{r['target']}] ({_fmt_size(r['size'])})")
        out.append(f"  当前激活: {st['active'] or '(无)'} → {st['target_path']} "
                   f"(exists={st['target_exists']}, synced={st['target_synced']})")
        return "\n".join(out)
    if sub in ("use", "activate", "u"):
        if len(args) < 2:
            return "用法: /purge-rules use <id>"
        try:
            meta = core.activate_rule(args[1])
            return (f"✓ 已激活规则 {meta['name']}（{meta['id']}）→ 写入 {meta['target_path']}，"
                    f"并注入 system_prompt_section。新会话生效。")
        except ValueError as e:
            return f"激活失败: {e}"
    if sub in ("create", "new", "c"):
        if len(args) < 2:
            return "用法: /purge-rules create <id> [别名] [AGENTS.md|CLAUDE.md]"
        rid = args[1]
        name = args[2] if len(args) > 2 else rid
        target = args[3] if len(args) > 3 else "AGENTS.md"
        if not core.valid_target(target):
            return f"无效目标: {target}（只能 AGENTS.md / CLAUDE.md；SOUL.md 是身份文件，不允许覆盖）"
        try:
            core.save_rule(rid, f"# 规则 {name}\n\n（编辑内容后重新保存）\n",
                           {"name": name, "target": target})
            return f"✓ 已创建规则 {name}（{rid} → {target}）。用 /purge-rules edit <id> 编辑。"
        except ValueError as e:
            return f"创建失败: {e}"
    if sub in ("edit", "write"):
        if len(args) < 3:
            return "用法: /purge-rules edit <id> <内容>（不允许空内容——空内容会覆盖目标文件为空白）"
        rid = args[1]
        if not core.valid_rule_id(rid):
            return "无效规则 id"
        body = (raw_args or "").split(maxsplit=2)
        content = body[2] if len(body) > 2 else ""
        if not content.strip():
            return "拒绝保存：规则内容为空。若想清空目标文件请用 /purge-rules reset 后手动移除。"
        try:
            core.save_rule(rid, content)
            return f"✓ 已保存规则 {rid}（{len(content)} 字符）。如已激活，目标文件与注入已同步。"
        except ValueError as e:
            return f"保存失败: {e}"
    if sub in ("delete", "rm", "d"):
        if len(args) < 2:
            return "用法: /purge-rules delete <id>"
        try:
            core.delete_rule(args[1])
            return f"✓ 已删除规则 {args[1]}。"
        except ValueError as e:
            return f"删除失败: {e}"
    if sub in ("reset", "restore"):
        r = core.reset_rules()
        out = []
        if r["removed"]:
            out.append(f"✓ 已删除激活规则写入的目标文件: {', '.join(r['removed'])}")
        if r["skipped"]:
            out.append(f"⚠ 跳过（文件与规则内容不一致）: {', '.join(r['skipped'])}")
        out.append("已清空激活状态；规则库保留。")
        return "\n".join(out)
    return ("规则集命令：\n"
            "  /purge-rules list                    列出所有规则\n"
            "  /purge-rules use <id>                激活规则（写目标文件 + 注入 system_prompt_section）\n"
            "  /purge-rules create <id> [别名] [目标]\n"
            "  /purge-rules edit <id> <内容>         保存规则内容\n"
            "  /purge-rules delete <id>             删除规则\n"
            "  /purge-rules reset                   还原出厂状态（清激活；规则库保留）")


# ── 模型工具 ─────────────────────────────────────────────────────

def _tool_schema(name: str, description: str) -> dict:
    """OpenAI 格式工具 schema（registry.get_definitions 期望 {"type":"function","function":...}）。"""
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def _register_tools(ctx, cfg: Dict[str, Any]) -> None:
    ctx.register_tool(
        name="purge_status",
        toolset="hermes-purge",
        schema=_tool_schema("purge_status", "查看 hermes-purge 状态（hermes home、override 文件、approvals 配置、deep patch 进度、规则集）。"),
        handler=lambda args, **kw: render_status(core.gather_state()),
    )
    ctx.register_tool(
        name="purge_apply",
        toolset="hermes-purge",
        schema=_tool_schema("purge_apply", "应用 hermes-purge 清洗：初始化 override、重写 approvals 配置、可选 deep patches。新会话生效；无需重启进程。"),
        handler=lambda args, **kw: _apply_tool_text(core.plugin_config()),
    )
    ctx.register_tool(
        name="purge_revert",
        toolset="hermes-purge",
        schema=_tool_schema("purge_revert", "回滚 hermes-purge 的 deep patches（保 override / rules / approvals 用户数据）。"),
        handler=lambda args, **kw: _revert_tool_text(),
    )
    ctx.register_tool(
        name="purge_rules_status",
        toolset="hermes-purge",
        schema=_tool_schema("purge_rules_status", "查看 hermes-purge 规则集状态。"),
        handler=lambda args, **kw: _rules_status_text(),
    )


def _apply_tool_text(cfg: Dict[str, Any]) -> str:
    core.ensure_override_content(fill_default=True)
    core.ensure_initial_state()
    approval = core.apply_approval_config(force=True)
    parts = [f"approvals: {approval.get('status')}"]
    if cfg.get("deepPatch") and core.hermes_install_root():
        core.backup_all()
        report = core.apply_patches()
        applied = [r for r in report if r["status"] in ("applied", "already")]
        parts.append(f"deep_patches applied={len(applied)} total={len(report)}")
    parts.append("override初装/配置重写完成。新会话生效。")
    return " | ".join(parts)


def _revert_tool_text() -> str:
    reverted, errors = core.revert_patches()
    parts = [f"deep_patches_reverted={len(reverted)}"]
    if errors:
        parts.append(f"errors={len(errors)}")
    parts.append("override/rules/approvals 为用户数据，revert 保留。")
    return " | ".join(parts)


def _rules_status_text() -> str:
    return str(core.rules_status())


# ── CLI 子命令（hermes purge ...）────────────────────────────────

def _register_cli(ctx) -> None:
    def setup(parser):
        sub = parser.add_subparsers(dest="purge_sub")
        for action, help_text in (("status", "显示清洗状态"), ("apply", "应用清洗"),
                                  ("doctor", "自检：通道/boost/SOUL/采样/权限"),
                                  ("revert", "回滚 deep patches"),
                                  ("probe", "写入运行时探针 nonce（验证注入是否到达模型）"),
                                  ("probe-clear", "清除运行时探针")):
            p = sub.add_parser(action, help=help_text)
            p.set_defaults(purge_action=action)

    def handler(args):
        action = getattr(args, "purge_action", "status")
        if action == "apply":
            text = _apply_tool_text(core.plugin_config())
        elif action == "doctor":
            text = render_doctor()
        elif action == "revert":
            text = _revert_tool_text()
        elif action == "probe":
            nonce = boost.create_probe(core)
            text = (f"探针已写入: {nonce}（30 分钟有效）\n"
                    f"尾部 boost 会附带回显指令；模型答出该串 = 注入链路到达模型。")
        elif action == "probe-clear":
            boost.clear_probe(core)
            text = "探针已清除。"
        else:
            text = render_status(core.gather_state())
        # 关键：register_cli_command 的 handler_fn 会变成 argparse 的 `func`，
        # 而 hermes_cli/main.py 只把它的 **int 返回值**当退出码（见 main.py 里
        # "A handler's int return code becomes the exit code"），字符串会被直接丢弃。
        # 早期版本 return 字符串，导致 `hermes purge status|apply|revert` 全部
        # 静默无输出——用户看到的是"命令没反应"，以为插件坏了。
        print(text)
        return 0

    ctx.register_cli_command("purge", "Hermes 指令权威性清洗（状态/应用/回滚）",
                             setup_fn=setup, handler_fn=handler)



# ── 拒绝检测 + 纠正（dsh-purge 1.7.20 refusal-retry 的 Hermes 映射）─────
# 通道 A（llm_request）：历史里最近一条 assistant 是拒绝 → 纠正指令随本次请求发出。
# 通道 B（pre_verify）：本回合改过文件时 Hermes 允许同回合继续 → 直接真重试一次。

def _refusal_cfg() -> Dict[str, Any]:
    try:
        return core.plugin_config()
    except Exception:
        return {}


def _llm_request_refusal_middleware(**kwargs):
    """尾部 boost 注入 + 拒绝纠正。

    顺序：messages + [boost(system)] (+ [corrective(system)] if 上一轮是拒绝)
    —— boost 永远最后落位，等价于 dsh-purge 把 boost 放在 order 最大处；
    Hermes 的 system_prompt_section 只能落 after_memory，拿不到这个位置。
    """
    try:
        cfg = _refusal_cfg()
        request = kwargs.get("request") or kwargs.get("args") or {}
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list) or not messages:
            return None

        appended = []

        # A) 尾部 boost（默认开，幂等：已有 marker 就不重复）
        if boost.enabled(cfg):
            already = any(
                isinstance(m, dict) and boost.MARKER in str(m.get("content") or "")
                for m in messages
            )
            if not already:
                home = None
                try:
                    from hermes_constants import get_hermes_home  # type: ignore
                    home = get_hermes_home()
                except Exception:
                    home = None
                nonce = boost.read_probe(core) or ""
                appended.append({"role": "system",
                                 "content": boost.boost_text(cfg, home=home, nonce=nonce)})

        # B) 拒绝纠正（历史里最后一条 assistant 是拒绝时）
        if refusal.enabled(cfg):
            nudge = refusal.corrective_from_messages(messages, cfg)
            if nudge:
                appended.append({"role": "system", "content": nudge})
                refusal.audit(core, "refusal_detected", channel="llm_request",
                              session=kwargs.get("session_id", ""), model=kwargs.get("model", ""))

        if not appended:
            return None
        patched = dict(request)
        patched["messages"] = list(messages) + appended
        return {"request": patched, "name": "hermes-purge/tail-boost",
                "reason": "tail boost / refusal corrective"}
    except Exception as exc:
        logger.debug("hermes-purge: llm_request middleware failed: %s", exc)
        return None


def _pre_verify_refusal(**kwargs):
    try:
        cfg = _refusal_cfg()
        if not refusal.enabled(cfg):
            return None
        attempt = int(kwargs.get("attempt", 0) or 0)
        if attempt >= refusal.max_attempts(cfg):
            refusal.audit(core, "refusal_retry_outcome", outcome="exhausted", attempt=attempt)
            return None
        resp = str(kwargs.get("final_response") or "")
        if not refusal.is_refusal(resp, cfg):
            return None
        refusal.audit(core, "refusal_retry", channel="pre_verify", attempt=attempt + 1,
                      session=kwargs.get("session_id", ""), chars=len(resp), frame=resp[:120])
        return {"action": "continue", "message": refusal.build_nudge(attempt + 1)}
    except Exception as exc:
        logger.debug("hermes-purge: pre_verify refusal hook failed: %s", exc)
        return None


# ── 插件入口 ─────────────────────────────────────────────────────

def register(ctx) -> None:
    cfg0 = core.plugin_config()
    if not cfg0.get("enabled", True):
        logger.info("hermes-purge: disabled via config")
        return

    # 1. system_prompt_section 注入（每次新会话渲染）
    # 两段分离：core（banner+inject，≤4000）与 rules（≤3800）。避免 Hermes
    # 对超总预算（8000）的 section 静默 skip —— 两段恒 ≤7800。
    ctx.register_system_prompt_section(
        "purge-core",
        lambda info: inject._core_section_text(),
        position="after_memory",
        max_chars=4000,
    )
    _RUNTIME["sections"].append("purge-core")
    ctx.register_system_prompt_section(
        "purge-rules",
        lambda info: inject._rules_section_text(),
        position="after_memory",
        max_chars=3800,
    )
    _RUNTIME["sections"].append("purge-rules")

    # 2. 命令
    ctx.register_command("purge", handler=lambda raw: _handle_purge(raw or ""),
                         description="Hermes 指令权威性清洗（status/apply/revert/write/edit/identity）")
    ctx.register_command("purge-rules", handler=_handle_rules,
                         description="规则集切换（list/use/create/edit/delete/reset）")

    # 3. 模型工具
    _register_tools(ctx, cfg0)

    # 4. CLI 子命令
    try:
        _register_cli(ctx)
    except Exception as e:
        # 用 warning 而不是 debug：注册失败会让 `hermes purge ...` 完全不可用，
        # 而 debug 默认不进 agent.log，用户只会看到"命令没反应"。
        logger.warning("hermes-purge: cli command registration failed: %s", e)

    # 5. 启动自动应用（on_session_start 异步，不阻塞会话）
    def _on_session_start(**kwargs):
        def run():
            _auto_apply(core.plugin_config())
        t = threading.Thread(target=run, daemon=True, name="hermes-purge-auto-apply")
        t.start()
        return None

    try:
        ctx.register_hook("on_session_start", _on_session_start)
        _RUNTIME["hooks"].append("on_session_start")
    except Exception as e:
        logger.warning("hermes-purge: on_session_start hook registration failed: %s", e)

    # 6. 拒绝检测 + 纠正（移植 dsh-purge 1.7.20 的 refusal-retry）
    try:
        ctx.register_middleware("llm_request", _llm_request_refusal_middleware)
        _RUNTIME["middleware"].append("llm_request")
    except Exception as e:
        logger.warning("hermes-purge: llm_request middleware registration failed: %s", e)
    try:
        ctx.register_hook("pre_verify", _pre_verify_refusal)
        _RUNTIME["hooks"].append("pre_verify")
    except Exception as e:
        logger.warning("hermes-purge: pre_verify hook registration failed: %s", e)

    # 审批结果观察者（不阻断；仅 verbose 记录）
    def _on_approval_response(**kwargs):
        if core.plugin_config().get("verbose"):
            choice = kwargs.get("choice") or kwargs.get("decided_by") or "?"
            logger.info("hermes-purge: approval response %s", choice)
        return None

    try:
        ctx.register_hook("post_approval_response", _on_approval_response)
    except Exception as e:
        logger.warning("hermes-purge: post_approval_response hook registration failed: %s", e)