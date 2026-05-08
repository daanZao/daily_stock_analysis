# -*- coding: utf-8 -*-
"""
===================================
Prompt Builder — Jinja2 System Prompt Assembly
===================================

Replaces hardcoded LEGACY_DEFAULT_SYSTEM_PROMPT / SYSTEM_PROMPT class
attributes in GeminiAnalyzer with template-driven rendering.

Templates live in ``templates/prompts/``:
    system.j2                     — main template with conditional legacy/default paths
    _dashboard_schema.j2          — shared JSON schema (used by both modes)
    _scoring_criteria_legacy.j2   — MA-based scoring (legacy mode only)
    _scoring_criteria_default.j2  — skill-based scoring (default mode)
    _principles.j2                — shared dashboard core principles
"""

import logging
from pathlib import Path
from typing import Optional

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    Environment = None  # type: ignore
    FileSystemLoader = None  # type: ignore

logger = logging.getLogger(__name__)

_JINJA_ENV: Optional[Environment] = None


def _resolve_prompt_templates_dir() -> Path:
    """Resolve prompt templates directory relative to project root."""
    base = Path(__file__).resolve().parent.parent
    return base / "templates" / "prompts"


def _get_jinja_env() -> Environment:
    """Lazy-init Jinja2 Environment for prompt templates."""
    global _JINJA_ENV
    if _JINJA_ENV is not None:
        return _JINJA_ENV
    if Environment is None or FileSystemLoader is None:
        raise ImportError("jinja2 is required for prompt rendering")
    templates_dir = _resolve_prompt_templates_dir()
    _JINJA_ENV = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return _JINJA_ENV


def _build_language_section(report_language: str) -> str:
    """Build language-specific output instructions appended at the end."""
    if report_language == "en":
        return (
            "## Output Language (highest priority)\n\n"
            "- Keep all JSON keys unchanged.\n"
            "- `decision_type` must remain `buy|hold|sell`.\n"
            "- All human-readable JSON values must be written in English.\n"
            "- Use the common English company name when you are confident; "
            "otherwise keep the original listed company name instead of inventing one.\n"
            "- This includes `stock_name`, `trend_prediction`, `operation_advice`, "
            "`confidence_level`, nested dashboard text, checklist items, "
            "and all narrative summaries.\n"
        )
    return (
        "## 输出语言（最高优先级）\n\n"
        "- 所有 JSON 键名保持不变。\n"
        "- `decision_type` 必须保持为 `buy|hold|sell`。\n"
        "- 所有面向用户的人类可读文本值必须使用中文。\n"
    )


def build_system_prompt(
    market_role: str,
    market_guidelines: str,
    report_language: str,
    *,
    skill_instructions: str = "",
    default_skill_policy: str = "",
    use_legacy_default_prompt: bool = False,
) -> str:
    """Build analyzer system prompt via Jinja2 template rendering.

    Replaces the previous LEGACY_DEFAULT_SYSTEM_PROMPT / SYSTEM_PROMPT
    class attributes with template-driven assembly.  The rendered output
    is byte-for-byte identical* to the legacy strings (minus trailing
    whitespace differences from ``trim_blocks``).

    * JSON schema uses the default (skill-based) example descriptions;
      these are only illustrative for the LLM and do not affect downstream
      parsing since Pydantic/integrity checks validate structure, not content.

    Args:
        market_role: e.g. " A 股", "美股", "港股"
        market_guidelines: Market-specific trading rules (T+1, limits, etc.)
        report_language: "zh" or "en"
        skill_instructions: Active skill instructions (injected in default mode)
        default_skill_policy: Default skill policy text (injected in default mode)
        use_legacy_default_prompt: If True, render legacy MA-based prompt with
            hardcoded CORE_TRADING_SKILL_POLICY_ZH.

    Returns:
        Fully rendered system prompt string.
    """
    env = _get_jinja_env()
    template = env.get_template("system.j2")

    skills_section = ""
    if skill_instructions:
        skills_section = f"## 激活的交易技能\n\n{skill_instructions}\n"

    default_skill_policy_section = ""
    if default_skill_policy:
        default_skill_policy_section = f"{default_skill_policy}\n"

    core_trading_skill_policy = ""
    if use_legacy_default_prompt:
        from src.agent.skills.defaults import CORE_TRADING_SKILL_POLICY_ZH
        core_trading_skill_policy = CORE_TRADING_SKILL_POLICY_ZH

    return template.render(
        legacy=use_legacy_default_prompt,
        market_role=market_role,
        market_guidelines=market_guidelines,
        core_trading_skill_policy=core_trading_skill_policy,
        default_skill_policy_section=default_skill_policy_section,
        skills_section=skills_section,
        language_section=_build_language_section(report_language),
    )
