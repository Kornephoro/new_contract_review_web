import json
import html
import time
import re
import hashlib
import docx
import PyPDF2
from pathlib import Path
from typing import List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components
from openai import OpenAI

from legal_review.llm import completion_with_tool_loop
from legal_review.mcp_bridge import call_tool_sync, load_mcp_config
from legal_review.ocr import (
    extract_text_with_paddle,
    get_ocr_init_command,
    get_paddle_ocr_not_ready_message,
    get_paddle_ocr_status,
    get_python_runtime_requirement_message,
    is_image_file,
    is_required_python_version,
    should_use_ocr_for_pdf,
)
from legal_review.review_html import build_risk_deck_html
from legal_review.prompts import (
    CHAT_SYSTEM_PREFIX,
    REVIEW_SYSTEM_BASE,
    REVIEW_MCP_SUFFIX,
    RISK_FOLLOWUP_PREFIX,
    build_dynamic_review_system,
)
from legal_review.review_postprocess import (
    get_actionable_risk_indices,
    get_risk_suggestion_state,
    is_risk_suggestion_actionable,
    postprocess_review_risks,
)
from legal_review.templates import (
    CONTRACT_TYPE_LABELS,
    CONTRACT_TYPE_OPTIONS,
    format_template_option_label,
    get_builtin_template,
    get_default_review_templates,
    get_review_template_by_id,
)

LOCAL_CONFIG_PATH = Path(__file__).resolve().parent / "api_settings.json"

def load_settings():
    if LOCAL_CONFIG_PATH.exists():
        try:
            with open(LOCAL_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings():
    s = {}
    for k in [
        "ai_provider_radio", "anthropic_api_key", "anthropic_model", 
        "openai_api_key", "openai_base_url", "openai_model",
        "ollama_base_url", "ollama_model",
        "review_templates", "selected_review_template_id",
    ]:
        if k in st.session_state:
            s[k] = st.session_state[k]
    try:
        with open(LOCAL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def _clear_template_editor_state(template_id: str) -> None:
    for prefix in ("template_name_", "template_scope_", "template_prompt_"):
        key = f"{prefix}{template_id}"
        if key in st.session_state:
            del st.session_state[key]


def _get_review_templates() -> list[dict]:
    templates = get_default_review_templates(st.session_state.get("review_templates"))
    st.session_state["review_templates"] = templates
    return templates


def _persist_review_templates(templates: list[dict], notice: Optional[str] = None) -> None:
    normalized_templates = get_default_review_templates(templates)
    st.session_state["review_templates"] = normalized_templates

    selected_template_id = st.session_state.get("selected_review_template_id", "auto")
    if selected_template_id != "auto" and not get_review_template_by_id(normalized_templates, selected_template_id):
        st.session_state["selected_review_template_id"] = "auto"

    if notice:
        st.session_state["template_notice"] = notice

    save_settings()


def _create_review_template(name: str, prompt: str, bound_contract_type: Optional[str]) -> None:
    templates = _get_review_templates()
    templates.append(
        {
            "id": f"ut-{time.time_ns()}",
            "name": name.strip(),
            "prompt": prompt.strip(),
            "is_builtin": False,
            "bound_contract_type": bound_contract_type or None,
        }
    )
    _persist_review_templates(templates, notice=f"已新增模板：{name.strip()}")


def _save_review_template(
    template_id: str,
    name: str,
    prompt: str,
    bound_contract_type: Optional[str],
) -> None:
    updated_templates: list[dict] = []
    target_name = ""
    for template in _get_review_templates():
        if template["id"] == template_id:
            target_name = name.strip() or template["name"]
            updated_templates.append(
                {
                    **template,
                    "name": target_name,
                    "prompt": prompt.strip(),
                    "bound_contract_type": bound_contract_type or None,
                }
            )
        else:
            updated_templates.append(template)

    _clear_template_editor_state(template_id)
    _persist_review_templates(updated_templates, notice=f"已保存模板：{target_name or template_id}")


def _reset_builtin_review_template(template_id: str) -> None:
    builtin_template = get_builtin_template(template_id)
    if not builtin_template:
        return

    reset_templates = [
        builtin_template if template["id"] == template_id else template
        for template in _get_review_templates()
    ]
    _clear_template_editor_state(template_id)
    _persist_review_templates(reset_templates, notice=f"已恢复默认模板：{builtin_template['name']}")


def _delete_review_template(template_id: str) -> None:
    removed_template = get_review_template_by_id(_get_review_templates(), template_id)
    remaining_templates = [
        template for template in _get_review_templates() if template["id"] != template_id
    ]

    if st.session_state.get("selected_review_template_id") == template_id:
        st.session_state["selected_review_template_id"] = "auto"

    _clear_template_editor_state(template_id)
    notice = f"已删除模板：{removed_template['name']}" if removed_template else "模板已删除"
    _persist_review_templates(remaining_templates, notice=notice)

MCP_CONFIG_PATH = Path(__file__).resolve().parent / "mcp_servers.json"

RISK_DECK_DIR = Path(__file__).resolve().parent / "legal_review" / "components" / "risk_deck"
DROPZONE_DIR = Path(__file__).resolve().parent / "legal_review" / "components" / "dropzone"

risk_deck_component = components.declare_component("risk_deck", path=str(RISK_DECK_DIR))
dropzone_component = components.declare_component("dropzone", path=str(DROPZONE_DIR))


@st.cache_resource
def cached_mcp_tools(path_str: str, mtime: float, enabled: bool) -> Tuple[list, dict]:
    if not enabled:
        return [], {}
    from legal_review.mcp_bridge import list_openai_tools_sync

    cfg = load_mcp_config(Path(path_str))
    if not cfg or not cfg.get("enabled"):
        return [], {}
    return list_openai_tools_sync(cfg)


THEME_MAP = {"跟随系统": "system", "浅色": "light", "深色": "dark"}


def _active_theme_key() -> str:
    base = (st.get_option("theme.base") or "light").strip().lower()
    return "dark" if base == "dark" else "light"


def _spans_overlap(a0, a1, b0, b1):
    return not (a1 <= b0 or b1 <= a0)


def _panel_palette(theme_key: str) -> dict:
    """合同面板固定对比色——墨律设计系统。"""
    return {
        "panel_bg": "var(--ml-shell-surface)",
        "panel_fg": "var(--ml-shell-text)",
        "border": "var(--ml-shell-border)",
        "muted": "var(--ml-shell-muted)",
    }


def _risk_level_styles(theme_key: str) -> dict:
    return {
        "高风险": ("rgba(207, 99, 92, 0.16)", "#cf635c"),
        "中风险": ("rgba(184, 135, 49, 0.16)", "#b88731"),
        "低风险": ("rgba(98, 130, 167, 0.16)", "#6282a7"),
    }


def _highlight_border_for_risk(risk: dict, theme_key: str) -> Tuple[str, str]:
    """优先按四维 dimension 着色，否则按风险等级。"""
    dim = (risk.get("dimension") or "").strip()
    dm = {
        "法律合规": ("rgba(98, 130, 167, 0.16)", "#6282a7"),
        "风险防控": ("rgba(207, 99, 92, 0.16)", "#cf635c"),
        "条款完善": ("rgba(184, 135, 49, 0.16)", "#b88731"),
        "利益保护": ("rgba(91, 160, 110, 0.16)", "#5ba06e"),
    }
    if dim in dm:
        return dm[dim]
    level = risk.get("level", "低风险")
    return _risk_level_styles(theme_key).get(level, _risk_level_styles(theme_key)["低风险"])


def build_highlighted_contract_html(
    text: str, risks: list, theme_key: str, applied_risks: set = None
) -> Tuple[str, List[int]]:
    """
    在合同正文中为每条风险的 original 片段添加高亮 HTML。
    返回 (html, not_found_indices)。
    """
    if not risks:
        return "", []

    if not (text or "").strip():
        pal = _panel_palette(theme_key)
        empty = (
            f'<div style="max-height:520px;overflow-y:auto;padding:16px 18px;border:1px solid {pal["border"]};'
            f'border-radius:10px;background:{pal["panel_bg"]};color:{pal["muted"]};'
            f'font-family:Outfit,Noto Sans SC,sans-serif;">（合同正文为空，无法标注）</div>'
        )
        return empty, []

    from legal_review.text_matcher import find_best_text_span
    
    candidates = []
    for idx, risk in enumerate(risks):
        orig = (risk.get("original") or "").strip()
        if not orig:
            continue
            
        start_idx, end_idx = find_best_text_span(text, orig)
        if start_idx != -1 and end_idx != -1:
            candidates.append((start_idx, end_idx, idx, orig))

    # Sort candidates by exact position, break ties using shorter spans (less likely to envelop everything)
    candidates.sort(key=lambda x: (x[0], (x[1] - x[0])))

    chosen = []
    used_ranges = []
    for pos, end, idx, orig in candidates:
        if any(_spans_overlap(pos, end, u0, u1) for u0, u1 in used_ranges):
            continue
        chosen.append((pos, end, idx))
        used_ranges.append((pos, end))

    chosen.sort(key=lambda x: x[0])
    # The fuzzy locator never fails entirely; 'not_found' is always empty effectively.
    not_found = []

    parts = []
    last = 0
    for pos, end, idx in chosen:
        parts.append(html.escape(text[last:pos]))
        num = idx + 1
        
        if applied_risks and idx in applied_risks:
            bg = "rgba(91, 160, 110, 0.16)"
            border = "#5ba06e"
            inner_text = risks[idx].get("suggestion", "")
            orig_txt = html.escape(text[pos:end])
            sug_txt = html.escape(inner_text)
            applied_text_color = "#5ba06e"
            inner = f'<span style="color:{applied_text_color};font-weight:600;">{sug_txt}</span>'
            parts.append(
                f'<span id="risk-anchor-{idx}" style="scroll-margin-top:88px;background:{bg};border-bottom:2px solid {border};'
                f'padding:2px 4px;border-radius:4px;color:inherit;" title="已应用修改，原文本为：{orig_txt}">{inner}'
                f'<sup style="font-size:0.7em;font-weight:700;margin-left:4px;color:{border};'
                f'font-family:Outfit,Noto Sans SC,sans-serif;">已修订</sup></span>'
            )
        else:
            bg, border = _highlight_border_for_risk(risks[idx], theme_key)
            inner = html.escape(text[pos:end])
            parts.append(
                f'<span id="risk-anchor-{idx}" style="scroll-margin-top:88px;background:{bg};border-bottom:2px solid {border};'
                f'padding:0 1px;color:inherit;" title="风险点 {num}">{inner}'
                f'<sup style="font-size:0.75em;font-weight:700;margin-left:2px;color:{border};">{num}</sup></span>'
            )
        last = end
    parts.append(html.escape(text[last:]))

    pal = _panel_palette(theme_key)
    inner = "".join(parts)

    shell_bg = "var(--ml-shell-surface-strong)"
    shell_border = "var(--ml-shell-border)"
    shell_shadow = "0 18px 36px rgba(0,0,0,0.12)"
    paper_bg = "var(--ml-shell-surface)"
    paper_border = "var(--ml-shell-border)"
    paper_shadow = "0 14px 28px rgba(0,0,0,0.10)"
    line_color = "color-mix(in srgb, var(--ml-accent-gold) 18%, transparent)"
    title_color = "var(--ml-shell-muted)"
    body_color = pal["panel_fg"]
    header_strip = "linear-gradient(90deg, var(--ml-accent-gold), rgba(44,62,107,0.18))"
    wrapper = (
        f'<div style="padding:18px;border-radius:24px;background:{shell_bg};border:1px solid {shell_border};'
        f'box-shadow:{shell_shadow};">'
        f'<div style="max-width:860px;margin:0 auto;border-radius:20px;background:{paper_bg};'
        f'border:1px solid {paper_border};box-shadow:{paper_shadow};overflow:hidden;">'
        f'<div style="height:5px;background:{header_strip};"></div>'
        f'<div style="padding:16px 24px 12px 24px;border-bottom:1px solid {line_color};'
        f'font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.76rem;letter-spacing:0.14em;'
        f'text-transform:uppercase;color:{title_color};">Contract Draft</div>'
        f'<div style="max-height:780px;overflow-y:auto;padding:22px 30px 34px 30px;color:{body_color};'
        f'white-space:pre-wrap;word-break:break-word;line-height:1.88;font-size:1rem;'
        f'font-family:Outfit,Noto Sans SC,sans-serif;'
        f'background-image:linear-gradient(to bottom, {line_color} 1px, transparent 1px);'
        f'background-size:100% 2.05rem;">{inner}</div>'
        f'</div></div>'
    )
    return wrapper, not_found


def _legend_html(theme_key: str) -> str:
    return (
        '<div style="margin-bottom:10px;font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.82rem;'
        'display:flex;gap:14px;align-items:center;">'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#cf635c;display:inline-block;"></span>'
        '<span style="color:#cf635c;font-weight:600;">高风险</span></span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#b88731;display:inline-block;"></span>'
        '<span style="color:#b88731;font-weight:600;">中风险</span></span>'
        '<span style="display:inline-flex;align-items:center;gap:5px;">'
        '<span style="width:8px;height:8px;border-radius:50%;background:#6282a7;display:inline-block;"></span>'
        '<span style="color:#6282a7;font-weight:600;">低风险</span></span>'
        "</div>"
    )


def _legend_dimensions_html(theme_key: str) -> str:
    return (
        '<div style="margin:6px 0 10px 0;font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.78rem;'
        'color:var(--ml-shell-muted);line-height:1.6;display:flex;flex-wrap:wrap;gap:4px;align-items:center;">'
        '<span style="font-weight:600;color:var(--ml-shell-text);">四维审查</span>'
        '<span style="color:var(--ml-shell-border);">|</span>'
        '<span style="color:#6282a7;font-weight:500;">法律合规</span>'
        '<span style="color:var(--ml-shell-border);">·</span>'
        '<span style="color:#cf635c;font-weight:500;">风险防控</span>'
        '<span style="color:var(--ml-shell-border);">·</span>'
        '<span style="color:#b88731;font-weight:500;">条款完善</span>'
        '<span style="color:var(--ml-shell-border);">·</span>'
        '<span style="color:#5ba06e;font-weight:500;">利益保护</span>'
        "</div>"
    )


def _get_llm_client_and_model() -> Tuple[OpenAI, str, str, bool]:
    """返回 (client, model_name, provider_name, use_tools) 基于系统配置"""
    provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
    
    if provider_choice == "Anthropic":
        api_key = st.session_state.get("anthropic_api_key", "")
        base_url = "https://api.anthropic.com/v1" 
        model_name = st.session_state.get("anthropic_model", "claude-3-opus-20240229")
        use_tools = True
    elif provider_choice == "Ollama (本地)":
        api_key = "ollama"
        base_url = st.session_state.get("ollama_base_url", "http://localhost:11434/v1")
        model_name = st.session_state.get("ollama_model", "qwen2.5:latest")
        use_tools = False
    else: # OpenAI 兼容
        api_key = st.session_state.get("openai_api_key", "")
        base_url = st.session_state.get("openai_base_url", "https://api.deepseek.com")
        model_name = st.session_state.get("openai_model", "deepseek-chat")
        use_tools = True

    client = OpenAI(api_key=api_key or "sk-dummy", base_url=base_url)
    return client, model_name, provider_choice, use_tools

def inject_page_theme_css() -> None:
    """读取前端真实明暗状态，只切换 DOM 标记，不触发 rerun。"""
    components.html(
        """
        <!doctype html>
        <html>
          <body style="margin:0;background:transparent;">
            <script>
              function parseColor(value) {
                const v = (value || "").trim();
                if (!v) return null;
                if (v.startsWith("#")) {
                  const hex = v.slice(1);
                  const full = hex.length === 3 ? hex.split("").map(ch => ch + ch).join("") : hex;
                  if (full.length !== 6) return null;
                  return {
                    r: parseInt(full.slice(0, 2), 16),
                    g: parseInt(full.slice(2, 4), 16),
                    b: parseInt(full.slice(4, 6), 16),
                  };
                }
                const m = v.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/i);
                if (!m) return null;
                return { r: parseInt(m[1], 10), g: parseInt(m[2], 10), b: parseInt(m[3], 10) };
              }

              function detectTheme() {
                try {
                  const parentDoc = window.parent.document;
                  const root = parentDoc.documentElement;
                  const app = parentDoc.querySelector(".stApp");
                  const main = parentDoc.querySelector('[data-testid="stAppViewContainer"] > .main');
                  const mainStyles = window.parent.getComputedStyle(main || app || root);
                  const appStyles = window.parent.getComputedStyle(app || root);
                  const rootStyles = window.parent.getComputedStyle(root);
                  const bg =
                    parseColor(mainStyles.backgroundColor) ||
                    parseColor(appStyles.backgroundColor) ||
                    parseColor(rootStyles.backgroundColor);
                  const fg =
                    parseColor(mainStyles.color) ||
                    parseColor(appStyles.color) ||
                    parseColor(rootStyles.color);
                  if (!bg && !fg) return "light";
                  const luminance = bg
                    ? (0.2126 * bg.r + 0.7152 * bg.g + 0.0722 * bg.b) / 255
                    : 1;
                  const textLuminance = fg
                    ? (0.2126 * fg.r + 0.7152 * fg.g + 0.0722 * fg.b) / 255
                    : 0;
                  if (luminance < 0.42 || textLuminance > 0.72) return "dark";
                  return "light";
                } catch (e) {
                  return "light";
                }
              }

              function applyThemeFlag() {
                try {
                  const theme = detectTheme();
                  const parentDoc = window.parent.document;
                  const root = parentDoc.documentElement;
                  const body = parentDoc.body;
                  root.setAttribute("data-ml-theme", theme);
                  if (body) body.setAttribute("data-ml-theme", theme);
                  const app = parentDoc.querySelector(".stApp");
                  if (app) app.setAttribute("data-ml-theme", theme);
                  const sidebar = parentDoc.querySelector('[data-testid="stSidebar"]');
                  if (sidebar) sidebar.setAttribute("data-ml-theme", theme);
                } catch (e) {}
              }

              applyThemeFlag();
              new MutationObserver(applyThemeFlag).observe(window.parent.document.documentElement, {
                attributes: true,
                attributeFilter: ["style", "class"],
              });
              const appNode = window.parent.document.querySelector(".stApp");
              if (appNode) {
                new MutationObserver(applyThemeFlag).observe(appNode, {
                  attributes: true,
                  attributeFilter: ["style", "class"],
                });
              }
              setInterval(applyThemeFlag, 1200);
            </script>
          </body>
        </html>
        """,
        height=0,
        width=0,
    )


# 初始化持久化配置
if "settings_loaded" not in st.session_state:
    _init_cfg = load_settings()
    for _k, _v in _init_cfg.items():
        st.session_state[_k] = _v
    
    # 初始化缺少默认值的配置项
    st.session_state.setdefault("anthropic_model", "claude-3-sonnet-20240229")
    st.session_state.setdefault("openai_base_url", "https://api.deepseek.com")
    st.session_state.setdefault("openai_model", "deepseek-chat")
    st.session_state.setdefault("ollama_base_url", "http://localhost:11434/v1")
    st.session_state.setdefault("ollama_model", "qwen2.5:latest")
    st.session_state["review_templates"] = get_default_review_templates(_init_cfg.get("review_templates"))
    st.session_state.setdefault("selected_review_template_id", "auto")
    if (
        st.session_state["selected_review_template_id"] != "auto"
        and not get_review_template_by_id(
            st.session_state["review_templates"],
            st.session_state["selected_review_template_id"],
        )
    ):
        st.session_state["selected_review_template_id"] = "auto"
    
    st.session_state["settings_loaded"] = True

# 页面基础设置
st.set_page_config(page_title="智审法务 - AI 合同审查助手", layout="wide")

if not is_required_python_version():
    st.error(get_python_runtime_requirement_message())

# ── 全局设计系统：字体 + 基础变量 + Streamlit 覆盖 ──
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=Outfit:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500;700&display=swap');

    :root {
        --ml-font-display: "Cormorant Garamond", "Noto Serif SC", "STSong", serif;
        --ml-font-body: "Outfit", "Noto Sans SC", "Microsoft YaHei", sans-serif;
        --ml-bg-primary: #faf8f5;
        --ml-bg-secondary: #f2efe9;
        --ml-bg-surface: #ffffff;
        --ml-text-primary: #1a1f2e;
        --ml-text-secondary: #5a5650;
        --ml-text-muted: #8a857f;
        --ml-accent-gold: #b8945f;
        --ml-accent-navy: #2c3e6b;
        --ml-border: #e2ddd5;
        --ml-border-light: #ece8e1;
        --ml-risk-high: #9b3030;
        --ml-risk-mid: #8b6a25;
        --ml-risk-low: #3a5a8b;
        --ml-shell-surface: #f2efe9;
        --ml-shell-surface-strong: #faf8f5;
        --ml-shell-surface-soft: #f6f2eb;
        --ml-shell-border: rgba(26, 31, 46, 0.14);
        --ml-shell-muted: rgba(26, 31, 46, 0.68);
        --ml-shell-text: #1a1f2e;
        --ml-shell-primary: #405989;
        --ml-shell-primary-strong: #31466d;
    }

    html[data-ml-theme="light"] {
        --ml-shell-surface: #f2efe9;
        --ml-shell-surface-strong: #faf8f5;
        --ml-shell-surface-soft: #f6f2eb;
        --ml-shell-border: rgba(26, 31, 46, 0.14);
        --ml-shell-muted: rgba(26, 31, 46, 0.68);
        --ml-shell-text: #1a1f2e;
        --ml-shell-primary: #405989;
        --ml-shell-primary-strong: #31466d;
    }

    html[data-ml-theme="dark"] {
        --ml-shell-surface: #1d2431;
        --ml-shell-surface-strong: #151b26;
        --ml-shell-surface-soft: #232b39;
        --ml-shell-border: rgba(232, 228, 223, 0.16);
        --ml-shell-muted: rgba(232, 228, 223, 0.68);
        --ml-shell-text: #e8e4df;
        --ml-shell-primary: #4d689d;
        --ml-shell-primary-strong: #3d5480;
    }

    /* ── Streamlit 全局覆盖 ── */
    .stApp {
        font-family: var(--ml-font-body) !important;
    }
    [data-testid="stAppViewContainer"] > .main .block-container {
        max-width: 1580px;
        padding-top: 2rem;
        padding-bottom: 3rem;
        padding-left: 2.15rem;
        padding-right: 2.15rem;
    }
    .stApp h1, .stApp h2, .stApp h3, .stApp h4 {
        font-family: var(--ml-font-display) !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em;
    }
    .stApp h1 {
        font-size: 2.2rem !important;
        color: var(--ml-shell-text) !important;
    }

    /* Sidebar */
    div[data-testid="stSidebar"] {
        font-family: var(--ml-font-body) !important;
    }
    div[data-testid="stSidebar"] .stRadio label,
    div[data-testid="stSidebar"] .stCheckbox label {
        font-family: var(--ml-font-body) !important;
        font-size: 0.9rem !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0;
        border-bottom: 2px solid var(--ml-border) !important;
    }
    .stTabs [data-baseweb="tab"] {
        font-family: var(--ml-font-body) !important;
        font-weight: 500 !important;
        font-size: 0.95rem !important;
        padding: 10px 24px !important;
        color: var(--ml-shell-text) !important;
        opacity: 0.68;
        border-bottom: 2px solid transparent !important;
        transition: all 0.2s ease;
    }
    .stTabs [aria-selected="true"] {
        color: var(--ml-shell-primary) !important;
        opacity: 1;
        border-bottom-color: var(--ml-shell-primary) !important;
        font-weight: 600 !important;
    }
    .stTabs [data-baseweb="tab-panel"] {
        padding-top: 1rem;
    }

    /* Buttons */
    .stButton > button[kind="primary"],
    .stButton > button[data-testid="stBaseButton-primary"] {
        font-family: var(--ml-font-body) !important;
        font-weight: 600 !important;
        border-radius: 8px !important;
        background: linear-gradient(135deg, #344a77, #263657) !important;
        border: none !important;
        color: #ffffff !important;
        letter-spacing: 0.02em;
        transition: all 0.25s ease;
    }
    .stButton > button[kind="primary"]:hover,
    .stButton > button[data-testid="stBaseButton-primary"]:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 14px rgba(44, 62, 107, 0.35) !important;
    }
    .stButton > button[kind="secondary"],
    .stButton > button[data-testid="stBaseButton-secondary"] {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
        border: 1.5px solid var(--ml-shell-border) !important;
        background: var(--ml-shell-surface) !important;
        color: var(--ml-shell-text) !important;
        transition: all 0.2s ease;
    }
    .stButton > button[kind="secondary"]:hover,
    .stButton > button[data-testid="stBaseButton-secondary"]:hover {
        border-color: var(--ml-accent-gold) !important;
        color: var(--ml-accent-gold) !important;
    }

    /* Dividers */
    hr {
        border-color: var(--ml-border-light) !important;
    }

    /* Text inputs */
    .stTextInput input, .stTextArea textarea {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
        border-color: var(--ml-shell-border) !important;
        background: var(--ml-shell-surface) !important;
        color: var(--ml-shell-text) !important;
        -webkit-text-fill-color: var(--ml-shell-text) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus {
        border-color: var(--ml-accent-gold) !important;
        box-shadow: 0 0 0 2px rgba(184, 148, 95, 0.15) !important;
    }
    .stTextInput input::placeholder, .stTextArea textarea::placeholder {
        color: var(--ml-shell-muted) !important;
        -webkit-text-fill-color: var(--ml-shell-muted) !important;
    }
    .stSelectbox [data-baseweb="select"] > div,
    .stMultiSelect [data-baseweb="select"] > div {
        background: var(--ml-shell-surface) !important;
        border-color: var(--ml-shell-border) !important;
        color: var(--ml-shell-text) !important;
    }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--ml-border); border-radius: 3px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--ml-text-muted); }

    /* Download button */
    .stDownloadButton > button {
        font-family: var(--ml-font-body) !important;
        border-radius: 8px !important;
    }
    div[data-testid="stToolbar"] {
        right: 1rem;
    }
    .intake-shell {
        padding: 18px 22px 16px 22px;
        border-radius: 26px;
        background:
            radial-gradient(circle at 88% 84%, rgba(184,148,95,0.10), transparent 16%),
            radial-gradient(circle at 12% 10%, rgba(184,148,95,0.08), transparent 18%),
            linear-gradient(145deg, rgba(255,255,255,0.05) 0%, rgba(255,255,255,0.02) 100%),
            var(--ml-shell-surface);
        border: 1px solid rgba(184,148,95,0.20);
        box-shadow: 0 18px 38px rgba(26,31,46,0.12);
        position: relative;
        overflow: hidden;
        margin-bottom: 14px;
    }
    .intake-shell::before {
        content: "";
        position: absolute;
        top: 0;
        left: 0;
        right: 0;
        height: 5px;
        background: linear-gradient(90deg, rgba(184,148,95,0.95), rgba(44,62,107,0.18));
    }
    .intake-shell::after {
        content: "";
        position: absolute;
        width: 160px;
        height: 160px;
        border-radius: 50%;
        right: -48px;
        bottom: -64px;
        background: rgba(184,148,95,0.08);
    }
    .intake-kicker {
        font-size: 0.72rem;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: var(--ml-accent-gold);
        font-weight: 700;
        margin-bottom: 6px;
        position: relative;
        z-index: 1;
    }
    .intake-title {
        font-family: var(--ml-font-display);
        font-size: 1.72rem;
        line-height: 1.05;
        color: var(--ml-shell-text);
        margin: 0;
        position: relative;
        z-index: 1;
    }
    .intake-copy {
        font-size: 0.9rem;
        color: var(--ml-shell-text);
        opacity: 0.76;
        line-height: 1.72;
        max-width: 48rem;
        margin-top: 8px;
        position: relative;
        z-index: 1;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-radius: 24px !important;
        border: 1px solid rgba(184,148,95,0.16) !important;
        background: var(--ml-shell-surface) !important;
        box-shadow: 0 18px 38px rgba(26,31,46,0.06) !important;
        padding: 0.7rem 0.9rem 0.95rem 0.9rem !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"],
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] div,
    div[data-testid="stVerticalBlockBorderWrapper"] label,
    div[data-testid="stVerticalBlockBorderWrapper"] small,
    div[data-testid="stVerticalBlockBorderWrapper"] span {
        color: var(--ml-shell-text) !important;
    }
    .home-card-title {
        font-family: var(--ml-font-display);
        font-size: 1.42rem;
        font-weight: 700;
        color: var(--ml-shell-text) !important;
        margin-bottom: 0;
    }
    .home-card-copy {
        font-family: var(--ml-font-body);
        font-size: 0.88rem;
        color: var(--ml-shell-text) !important;
        opacity: 0.78;
        line-height: 1.72;
        margin: 4px 0 10px 0;
        max-width: 36rem;
    }
    .home-section-label {
        font-family: var(--ml-font-body);
        font-size: 0.78rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--ml-shell-primary) !important;
        margin: 10px 0 6px 0;
    }
    .home-chip-row {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin: 8px 0 10px 0;
    }
    .home-chip {
        display: inline-flex;
        align-items: center;
        padding: 6px 10px;
        border-radius: 999px;
        background: var(--ml-shell-surface-strong) !important;
        border: 1px solid rgba(184,148,95,0.16);
        color: var(--ml-shell-text) !important;
        font-size: 0.8rem;
        line-height: 1.2;
    }
    .home-note {
        padding: 12px 14px;
        border-radius: 16px;
        background: var(--ml-shell-surface-strong) !important;
        border: 1px solid rgba(184,148,95,0.14);
        color: var(--ml-shell-text) !important;
        font-size: 0.86rem;
        line-height: 1.68;
        margin-top: 4px;
    }
    .home-placeholder {
        padding: 14px 16px;
        border-radius: 16px;
        background: var(--ml-shell-surface-strong) !important;
        border: 1px solid rgba(44,62,107,0.16);
        color: var(--ml-shell-text) !important;
        font-size: 0.92rem;
        line-height: 1.68;
        margin-top: 12px;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] .stCaptionContainer,
    div[data-testid="stVerticalBlockBorderWrapper"] .stCaptionContainer p {
        color: var(--ml-shell-text) !important;
        opacity: 0.75;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] [data-baseweb="radio"] label,
    div[data-testid="stVerticalBlockBorderWrapper"] [data-baseweb="radio"] div {
        color: var(--ml-shell-text) !important;
    }
    div[data-testid="stFileUploader"] section {
        background: var(--ml-shell-surface-strong) !important;
        border: 1px dashed rgba(184,148,95,0.26) !important;
        border-radius: 16px !important;
    }
    div[data-testid="stFileUploader"] button {
        background: var(--ml-shell-surface) !important;
        color: var(--ml-shell-text) !important;
        border: 1px solid rgba(184,148,95,0.20) !important;
    }
    div[data-testid="stFileUploader"] small,
    div[data-testid="stFileUploader"] span,
    div[data-testid="stFileUploader"] label {
        color: var(--ml-shell-text) !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-baseweb="select"] > div,
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-baseweb="base-input"] > div {
        background: var(--ml-shell-surface-strong) !important;
        color: var(--ml-shell-text) !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] textarea,
    div[data-testid="stVerticalBlockBorderWrapper"] input {
        color: var(--ml-shell-text) !important;
        -webkit-text-fill-color: var(--ml-shell-text) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ 系统配置")
    provider_choice = st.radio(
        "AI 提供商",
        ["Anthropic", "OpenAI", "Ollama (本地)"],
        captions=["Claude 系列模型", "GPT 系列及兼容接口 (DeepSeek 等)", "完全离线，保护数据安全"],
        key="ai_provider_radio",
        on_change=save_settings,
    )
    
    if provider_choice == "Anthropic":
        st.text_input("Anthropic API Key", type="password", key="anthropic_api_key", on_change=save_settings)
        st.text_input("模型", placeholder="例如：claude-3-7-sonnet-20250219", key="anthropic_model", on_change=save_settings)
    elif provider_choice == "OpenAI":
        st.text_input("OpenAI API Key", type="password", key="openai_api_key", on_change=save_settings)
        st.text_input("API Base URL", key="openai_base_url", on_change=save_settings)
        st.text_input("模型", key="openai_model", on_change=save_settings)
    else:
        st.text_input("Ollama 地址", key="ollama_base_url", on_change=save_settings)
        st.text_input("本地模型", placeholder="输入模型名称，如 qwen2.5:32b", key="ollama_model", on_change=save_settings)
        
    st.checkbox(
        "启用 MCP 工具（审查与对话可检索外部资料）",
        value=False,
        key="use_mcp",
        help="需在同目录配置 mcp_servers.json，并确保本机可启动对应 MCP Server（如 npx/uv/python）。",
    )
    st.caption("MCP：可将 `mcp_servers.example.json` 复制为 `mcp_servers.json` 后按需修改。")
    st.markdown("---")
    with st.expander("审校模板库", expanded=False):
        template_notice = st.session_state.pop("template_notice", None)
        if template_notice:
            st.success(template_notice)

        review_templates = _get_review_templates()
        scope_options = ["none"] + [option_id for option_id, _ in CONTRACT_TYPE_OPTIONS]

        st.caption("模板会把专项审校重点追加到 AI system prompt。内置模板可改写并恢复默认，自定义模板会保存在本地。")

        with st.form("create_review_template_form", clear_on_submit=True):
            new_template_name = st.text_input("新模板名称")
            new_template_scope = st.selectbox(
                "绑定合同类型",
                options=scope_options,
                format_func=lambda value: "不限定合同类型" if value == "none" else CONTRACT_TYPE_LABELS.get(value, value),
            )
            new_template_prompt = st.text_area(
                "专项审校重点",
                height=180,
                placeholder="例如：\n- 重点检查付款与验收条款是否互相衔接。\n- 重点识别单方免责、责任上限和通知机制风险。",
            )
            create_template_submitted = st.form_submit_button("新增模板", use_container_width=True)

        if create_template_submitted:
            if not new_template_name.strip():
                st.warning("请输入模板名称。")
            elif not new_template_prompt.strip():
                st.warning("请输入专项审校重点。")
            else:
                _create_review_template(
                    new_template_name,
                    new_template_prompt,
                    None if new_template_scope == "none" else new_template_scope,
                )
                st.rerun()

        st.markdown("#### 现有模板")
        for template in review_templates:
            template_id = template["id"]
            scope_value = template.get("bound_contract_type") or "none"
            if scope_value not in scope_options:
                scope_value = "none"

            with st.expander(format_template_option_label(template), expanded=False):
                if template.get("is_builtin"):
                    st.caption("内置模板：可调整专项审校重点，也可一键恢复默认内容。")
                else:
                    st.caption("自定义模板：可编辑名称、适用合同类型和专项审校重点。")

                with st.form(f"template_form_{template_id}", clear_on_submit=False):
                    edited_name = st.text_input(
                        "模板名称",
                        value=template["name"],
                        key=f"template_name_{template_id}",
                        disabled=bool(template.get("is_builtin")),
                    )
                    edited_scope = st.selectbox(
                        "绑定合同类型",
                        options=scope_options,
                        index=scope_options.index(scope_value),
                        key=f"template_scope_{template_id}",
                        format_func=lambda value: "不限定合同类型" if value == "none" else CONTRACT_TYPE_LABELS.get(value, value),
                        disabled=bool(template.get("is_builtin")),
                    )
                    edited_prompt = st.text_area(
                        "专项审校重点",
                        value=template.get("prompt", ""),
                        height=180,
                        key=f"template_prompt_{template_id}",
                    )

                    action_col1, action_col2 = st.columns(2)
                    save_clicked = action_col1.form_submit_button("保存模板", use_container_width=True)
                    reset_clicked = False
                    delete_clicked = False
                    if template.get("is_builtin"):
                        reset_clicked = action_col2.form_submit_button("恢复默认", use_container_width=True)
                    else:
                        delete_clicked = action_col2.form_submit_button("删除模板", use_container_width=True)

                if save_clicked:
                    if not edited_prompt.strip():
                        st.warning("专项审校重点不能为空。")
                    elif not template.get("is_builtin") and not edited_name.strip():
                        st.warning("模板名称不能为空。")
                    else:
                        _save_review_template(
                            template_id,
                            edited_name,
                            edited_prompt,
                            None if edited_scope == "none" else edited_scope,
                        )
                        st.rerun()

                if reset_clicked:
                    _reset_builtin_review_template(template_id)
                    st.rerun()

                if delete_clicked:
                    _delete_review_template(template_id)
                    st.rerun()

    st.markdown("---")
    st.markdown("### 关于系统")
    st.markdown(
        "本系统利用大语言模型结合**法律专家提示词**，支持合同审查、**上下文追问对话**，"
        "以及可选的 **MCP 工具**挂接外部法律数据库。"
    )

inject_page_theme_css()

# --- 主页面 ---
st.markdown(
    '<div style="margin-bottom:4px;">'
    '<h1 style="font-family:Cormorant Garamond,Noto Serif SC,serif !important;font-size:2.4rem !important;'
    'font-weight:700;color:var(--ml-shell-text) !important;margin:0;letter-spacing:-0.02em;">智审法务</h1>'
    '<p style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.92rem;color:var(--ml-shell-text) !important;opacity:0.72;margin:4px 0 0 0;'
    'letter-spacing:0.03em;">AI-Powered Contract Risk Analysis</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.divider()

if "review_snapshot" not in st.session_state:
    st.session_state.review_snapshot = None
if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []
if "contract_text_for_chat" not in st.session_state:
    st.session_state.contract_text_for_chat = ""
if "risk_followup_chats" not in st.session_state:
    st.session_state.risk_followup_chats = {}
if "focus_risk_idx" not in st.session_state:
    st.session_state.focus_risk_idx = None
if "review_later_risks" not in st.session_state:
    st.session_state.review_later_risks = set()
if "modified_contract_text" not in st.session_state:
    st.session_state.modified_contract_text = ""
if "applied_risks" not in st.session_state:
    st.session_state.applied_risks = set()
if "workspace_notice" not in st.session_state:
    st.session_state.workspace_notice = None
if "original_file_bytes" not in st.session_state:
    st.session_state.original_file_bytes = None
if "original_file_name" not in st.session_state:
    st.session_state.original_file_name = None
if "last_uploaded_file_id" not in st.session_state:
    st.session_state.last_uploaded_file_id = None
if "contract_input_mode" not in st.session_state:
    st.session_state.contract_input_mode = "上传合同文件"
if "show_export_dialog" not in st.session_state:
    st.session_state.show_export_dialog = False


def _hash_text(value: str) -> str:
    return hashlib.sha1((value or "").encode("utf-8")).hexdigest()


def _build_highlight_cache_key(text: str, risks: list, theme_key: str, applied_risks: set | None) -> str:
    risk_payload = [
        {
            "original": (risk or {}).get("original"),
            "suggestion": (risk or {}).get("suggestion"),
            "level": (risk or {}).get("level"),
            "dimension": (risk or {}).get("dimension"),
        }
        for risk in (risks or [])
    ]
    applied = sorted(int(idx) for idx in (applied_risks or set()))
    payload = {
        "text_hash": _hash_text(text or ""),
        "theme": theme_key,
        "risks": risk_payload,
        "applied": applied,
    }
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _count_review_statuses(
    risks: list[dict],
    applied_risks: set | None,
    review_later_risks: set | None = None,
) -> dict[str, int]:
    applied_set = applied_risks or set()
    review_later_set = review_later_risks or set()
    accepted = len(applied_set)
    reviewable = 0
    info_only = 0
    pending = 0
    review_later = 0
    for idx, risk in enumerate(risks or []):
        state = get_risk_suggestion_state(risk)
        if idx in applied_set:
            continue
        if idx in review_later_set:
            review_later += 1
            continue
        if state["actionable"]:
            reviewable += 1
            pending += 1
        elif state["has_display"]:
            info_only += 1
            pending += 1
    return {
        "accepted": accepted,
        "review_later": review_later,
        "reviewable": reviewable,
        "info_only": info_only,
        "pending": pending,
    }


def _build_workspace_risk_list(risks: list[dict], dim_filter: str, sort_order: str, status_filter: str) -> list[tuple[int, dict]]:
    level_rank = {"高风险": 0, "中风险": 1, "低风险": 2}
    risks_with_idx = [(i, r) for i, r in enumerate(risks or [])]
    if dim_filter != "全部":
        risks_with_idx = [(i, r) for i, r in risks_with_idx if r.get("dimension") == dim_filter]
    if status_filter != "全部":
        filtered: list[tuple[int, dict]] = []
        applied_set = st.session_state.get("applied_risks", set())
        review_later_set = st.session_state.get("review_later_risks", set())
        for idx, risk in risks_with_idx:
            state = get_risk_suggestion_state(risk)
            if status_filter == "待处理" and idx not in applied_set and idx not in review_later_set:
                filtered.append((idx, risk))
            elif status_filter == "待复核" and idx not in applied_set and idx in review_later_set:
                filtered.append((idx, risk))
            elif status_filter == "已采纳" and idx in applied_set:
                filtered.append((idx, risk))
            elif (
                status_filter == "仅说明性意见"
                and idx not in applied_set
                and idx not in review_later_set
                and (not state["actionable"])
                and state["has_display"]
            ):
                filtered.append((idx, risk))
        risks_with_idx = filtered
    if sort_order == "风险高到低":
        risks_with_idx = sorted(risks_with_idx, key=lambda x: level_rank.get(x[1].get("level", "低风险"), 2))
    elif sort_order == "风险低到高":
        risks_with_idx = sorted(risks_with_idx, key=lambda x: -level_rank.get(x[1].get("level", "低风险"), 2))
    return risks_with_idx


def _resolve_component_idx(new_idx: int, risks_with_idx: list[tuple[int, dict]]) -> int:
    return risks_with_idx[new_idx][0] if 0 <= new_idx < len(risks_with_idx) else new_idx


def _toggle_applied_risk(risk_idx: int) -> None:
    applied_set = set(st.session_state.get("applied_risks", set()))
    review_later_set = set(st.session_state.get("review_later_risks", set()))
    if risk_idx in applied_set:
        applied_set.remove(risk_idx)
    else:
        applied_set.add(risk_idx)
        review_later_set.discard(risk_idx)
    st.session_state.applied_risks = applied_set
    st.session_state.review_later_risks = review_later_set


def _toggle_review_later_risk(risk_idx: int) -> bool:
    review_later_set = set(st.session_state.get("review_later_risks", set()))
    applied_set = set(st.session_state.get("applied_risks", set()))
    if risk_idx in review_later_set:
        review_later_set.remove(risk_idx)
        marked = False
    else:
        review_later_set.add(risk_idx)
        applied_set.discard(risk_idx)
        marked = True
    st.session_state.review_later_risks = review_later_set
    st.session_state.applied_risks = applied_set
    return marked


def _build_review_snapshot(final_text: str, contract_type: str, overview: dict, risks: list[dict], selected_template: Optional[dict]) -> dict:
    return {
        "text": final_text,
        "contract_type": contract_type,
        "overview": overview,
        "risks": risks,
        "selected_template_name": selected_template["name"] if selected_template else None,
    }


def extract_text(file, file_bytes: bytes | None = None):
    text = ""
    suffix = Path(file.name).suffix.lower()
    file_bytes = file_bytes if file_bytes is not None else file.getvalue()

    if suffix == ".docx":
        doc = docx.Document(file)
        text = "\n".join([para.text for para in doc.paragraphs])
    elif suffix == ".pdf":
        pdf_reader = PyPDF2.PdfReader(file)
        page_texts = []
        non_empty_pages = 0
        for page in pdf_reader.pages:
            page_text = (page.extract_text() or "").strip()
            if page_text:
                non_empty_pages += 1
            page_texts.append(page_text)
        text = "\n".join(page_texts).strip()

        if should_use_ocr_for_pdf(text, len(page_texts), non_empty_pages):
            ocr_status = get_paddle_ocr_status()
            if not ocr_status["ready"]:
                raise RuntimeError(
                    "当前 PDF 识别为扫描件或图片型 PDF，需使用 OCR。"
                    + get_paddle_ocr_not_ready_message()
                )
            text = extract_text_with_paddle(file_bytes, suffix)
    elif is_image_file(file.name):
        ocr_status = get_paddle_ocr_status()
        if not ocr_status["ready"]:
            raise RuntimeError(
                "图片合同识别依赖 OCR。"
                + get_paddle_ocr_not_ready_message()
            )
        text = extract_text_with_paddle(file_bytes, suffix)

    if not re.sub(r"\s+", "", text or ""):
        raise ValueError("未能从文件中提取出可用文本，请确认文件内容清晰可读。")
    return text


@st.cache_data(show_spinner=False)
def _cached_highlight_contract_html(
    _cache_key: str,
    text: str,
    risks: list,
    theme_key: str,
    applied_risks: list[int],
) -> Tuple[str, List[int]]:
    return build_highlighted_contract_html(text, risks, theme_key, set(applied_risks))


def get_highlighted_contract_html(text: str, risks: list, theme_key: str, applied_risks: set | None = None) -> Tuple[str, List[int]]:
    cache_key = _build_highlight_cache_key(text, risks, theme_key, applied_risks)
    return _cached_highlight_contract_html(cache_key, text, risks, theme_key, sorted(applied_risks or set()))


def build_chat_system_prompt(contract: str, review_snap: Optional[dict]) -> str:
    body = CHAT_SYSTEM_PREFIX + "\n\n--- 合同正文（节选） ---\n" + (contract or "")[:12000]
    if review_snap:
        ct = review_snap.get("contract_type")
        if ct:
            body += f"\n\n--- 合同类型 ---\n{ct}"
        if review_snap.get("risks"):
            body += "\n\n--- 最近一次审查结果（JSON 节选） ---\n"
            body += json.dumps(review_snap["risks"], ensure_ascii=False)[:8000]
    return body


def _render_workspace_header(
    snap: dict,
    theme_key: str,
    shell_bg: str,
    shell_border: str,
    shell_shadow: str,
    shell_text: str,
    shell_muted: str,
) -> None:
    risks = snap.get("risks") or []
    selected_template_name = (snap.get("selected_template_name") or "通用合同审校").strip()
    contract_type_name = snap.get("contract_type") or "未识别"
    status_counts = _count_review_statuses(
        risks,
        st.session_state.get("applied_risks", set()),
        st.session_state.get("review_later_risks", set()),
    )
    level_counts = {
        "高风险": sum(1 for risk in risks if risk.get("level") == "高风险"),
        "中风险": sum(1 for risk in risks if risk.get("level") == "中风险"),
        "低风险": sum(1 for risk in risks if risk.get("level") == "低风险"),
    }
    meta_html = (
        f'<div style="padding:16px 18px;border-radius:20px;background:{shell_bg};'
        f'border:1px solid {shell_border};box-shadow:{shell_shadow};font-family:Outfit,Noto Sans SC,sans-serif;">'
        f'<div style="display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;align-items:flex-start;">'
        f'<div>'
        f'<div style="font-size:0.72rem;letter-spacing:0.12em;text-transform:uppercase;color:var(--ml-accent-gold);font-weight:700;margin-bottom:6px;">Review Desk</div>'
        f'<div style="font-size:1.08rem;color:{shell_text};font-weight:600;">{html.escape(contract_type_name)}</div>'
        f'<div style="font-size:0.86rem;color:{shell_muted};margin-top:4px;line-height:1.7;">当前模板：{html.escape(selected_template_name)}</div>'
        f'</div>'
        f'<div style="display:grid;grid-template-columns:repeat(3, minmax(90px, 1fr));gap:10px;min-width:min(100%, 330px);">'
        f'<div style="padding:10px 12px;border-radius:14px;background:var(--ml-shell-surface);border:1px solid {shell_border};">'
        f'<div style="font-size:0.72rem;color:{shell_muted};">风险总数</div><div style="font-size:1.08rem;color:{shell_text};font-weight:700;">{len(risks)}</div></div>'
        f'<div style="padding:10px 12px;border-radius:14px;background:var(--ml-shell-surface);border:1px solid {shell_border};">'
        f'<div style="font-size:0.72rem;color:{shell_muted};">已采纳</div><div style="font-size:1.08rem;color:{shell_text};font-weight:700;">{status_counts["accepted"]}</div></div>'
        f'<div style="padding:10px 12px;border-radius:14px;background:var(--ml-shell-surface);border:1px solid {shell_border};">'
        f'<div style="font-size:0.72rem;color:{shell_muted};">待处理</div><div style="font-size:1.08rem;color:{shell_text};font-weight:700;">{status_counts["pending"]}</div></div>'
        f'</div></div>'
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;">'
        f'<span style="padding:6px 12px;border-radius:999px;background:rgba(191,98,90,0.14);color:#cf635c;border:1px solid rgba(196,101,94,0.18);font-size:0.8rem;font-weight:600;">高风险 {level_counts["高风险"]}</span>'
        f'<span style="padding:6px 12px;border-radius:999px;background:rgba(212,176,74,0.14);color:#b88731;border:1px solid rgba(212,168,74,0.18);font-size:0.8rem;font-weight:600;">中风险 {level_counts["中风险"]}</span>'
        f'<span style="padding:6px 12px;border-radius:999px;background:rgba(122,158,196,0.16);color:#6282a7;border:1px solid rgba(122,158,196,0.18);font-size:0.8rem;font-weight:600;">低风险 {level_counts["低风险"]}</span>'
        f'<span style="padding:6px 12px;border-radius:999px;background:var(--ml-shell-surface);color:{shell_text};border:1px solid {shell_border};font-size:0.8rem;font-weight:600;">待复核 {status_counts["review_later"]}</span>'
        f'<span style="padding:6px 12px;border-radius:999px;background:var(--ml-shell-surface);color:{shell_muted};border:1px solid {shell_border};font-size:0.8rem;font-weight:600;">说明性意见 {status_counts["info_only"]}</span>'
        f'</div></div>'
    )
    st.markdown(meta_html, unsafe_allow_html=True)


def _render_contract_canvas(theme_key: str, shell_bg: str, shell_border: str, shell_shadow: str, shell_text: str, shell_muted: str, hl_html: str) -> None:
    st.markdown(
        f'<div style="padding:14px 16px 10px 16px;border-radius:20px;background:{shell_bg};'
        f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:12px;">'
        f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.2rem;'
        f'font-weight:700;color:{shell_text};">合同正文画布</div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
        f'line-height:1.7;margin-top:4px;max-width:30rem;">用于在原文上下文中查看风险定位、修订效果和条款变化。</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.markdown(_legend_html(theme_key), unsafe_allow_html=True)
    st.markdown(_legend_dimensions_html(theme_key), unsafe_allow_html=True)
    st.markdown(hl_html, unsafe_allow_html=True)


def _render_change_review_panel(snap: dict, shell_bg: str, shell_border: str, shell_shadow: str, shell_text: str, shell_muted: str) -> None:
    applied_indices = sorted(st.session_state.get("applied_risks", set()))
    risks = snap.get("risks") or []
    if not applied_indices:
        st.markdown(
            f'<div style="padding:18px 20px;border-radius:20px;background:{shell_bg};border:1px solid {shell_border};box-shadow:{shell_shadow};margin-top:18px;">'
            f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.22rem;font-weight:700;color:{shell_text};">变更确认区</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};line-height:1.72;margin-top:6px;max-width:34rem;">当前还没有已采纳的修改。处理左侧风险后，这里会汇总即将进入导出的条款修订。</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        return

    st.markdown(
        f'<div style="padding:18px 20px;border-radius:20px;background:{shell_bg};border:1px solid {shell_border};box-shadow:{shell_shadow};margin-top:18px;">'
        f'<div style="display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:flex-end;">'
        f'<div><div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.24rem;font-weight:700;color:{shell_text};">变更确认区</div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};line-height:1.72;margin-top:4px;max-width:42rem;">查看本次已采纳的修改内容，并在导出前完成最后确认。</div></div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.82rem;color:{shell_muted};">已采纳 {len(applied_indices)} 条</div></div></div>',
        unsafe_allow_html=True,
    )
    preview_card_bg = "var(--ml-shell-surface)"
    preview_rows = []
    for idx in applied_indices[:8]:
        if idx >= len(risks):
            continue
        risk = risks[idx]
        state = get_risk_suggestion_state(risk)
        preview_rows.append(
            f'<div style="padding:14px 16px;border-radius:16px;border:1px solid {shell_border};background:{preview_card_bg};margin-top:10px;">'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.8rem;color:{shell_muted};margin-bottom:6px;">风险点 {idx + 1}</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.88rem;color:{shell_text};line-height:1.7;"><strong>原文：</strong>{html.escape((risk.get("original") or "")[:120])}</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.88rem;color:{shell_text};line-height:1.7;margin-top:6px;"><strong>替换后：</strong>{html.escape((state["display"] or "")[:160])}</div>'
            f'</div>'
        )
    st.markdown("".join(preview_rows), unsafe_allow_html=True)
    if len(applied_indices) > 8:
        st.caption(f"还有 {len(applied_indices) - 8} 条已采纳修改将在导出区继续展示。")


def _render_decision_panel(
    snap: dict,
    theme_key: str,
    shell_bg: str,
    shell_border: str,
    shell_shadow: str,
    shell_text: str,
    shell_muted: str,
) -> None:
    contract_ctx = snap.get("text") or ""
    st.markdown(
        f'<div style="padding:16px 18px 12px 18px;border-radius:20px;background:{shell_bg};'
        f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:12px;">'
        f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.2rem;'
        f'font-weight:700;color:{shell_text};">当前风险决策台</div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
        f'line-height:1.7;margin-top:4px;max-width:19rem;">这里集中展示当前风险的摘要、建议和补充追问。</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    focus_idx = st.session_state.get("focus_risk_idx")

    if focus_idx is None:
        st.caption("把左侧风险卡拖到这里，或在卡片上点“深入追问”后进入当前处理状态。")
        focus_from_js_dz = dropzone_component(
            title="将风险卡放入当前处理台",
            subtitle="这里会成为当前风险的决策与追问中心",
            min_height=200,
            theme_key=theme_key,
            key="dropzone_large_workspace"
        )
        if focus_from_js_dz and isinstance(focus_from_js_dz, dict):
            new_idx = int(focus_from_js_dz.get("idx", -1))
            new_ts = focus_from_js_dz.get("ts", 0)
            if new_idx >= 0 and new_ts != st.session_state.get("last_dz_ts_v2"):
                st.session_state["last_dz_ts_v2"] = new_ts
                st.session_state.focus_risk_idx = new_idx
                st.rerun()
        return

    if focus_idx < 0 or focus_idx >= len(snap["risks"]):
        st.warning("当前处理对象无效，请重新选择。")
        st.session_state.focus_risk_idx = None
        return

    risk = snap["risks"][focus_idx]
    hist = st.session_state.risk_followup_chats.setdefault(focus_idx, [])
    state = get_risk_suggestion_state(risk)
    applied_set = st.session_state.get("applied_risks", set())
    review_later_set = st.session_state.get("review_later_risks", set())
    is_review_later = focus_idx in review_later_set
    st.markdown(f"**当前处理：** 风险点 {focus_idx + 1} · {risk.get('dimension', '')} · {risk.get('level', '')}")
    st.caption((risk.get("original", "") or "")[:180] + ("…" if len(risk.get("original", "") or "") > 180 else ""))

    mini_dz_val = dropzone_component(
        title="拖入新卡片可替换当前处理对象",
        subtitle="",
        min_height=52,
        theme_key=theme_key,
        key="dropzone_mini_replace_workspace"
    )
    if mini_dz_val and isinstance(mini_dz_val, dict):
        new_idx = int(mini_dz_val.get("idx", -1))
        new_ts = mini_dz_val.get("ts", 0)
        if new_idx >= 0 and new_ts != st.session_state.get("last_mini_dz_ts_v2"):
            st.session_state["last_mini_dz_ts_v2"] = new_ts
            st.session_state.focus_risk_idx = new_idx
            st.rerun()

    state_badges = []
    applied_badge_bg = "rgba(184,148,95,0.14)"
    applied_badge_border = "rgba(184,148,95,0.28)"
    applied_badge_fg = "#b8945f"
    review_badge_bg = "rgba(98,130,167,0.14)"
    review_badge_border = "rgba(98,130,167,0.26)"
    review_badge_fg = "#6282a7"
    detail_panel_bg = "var(--ml-shell-surface-strong)"
    detail_subpanel_bg = "var(--ml-shell-surface)"
    chat_panel_bg = "var(--ml-shell-surface)"
    if focus_idx in applied_set:
        state_badges.append(
            f'<span style="padding:4px 10px;border-radius:999px;background:{applied_badge_bg};'
            f'border:1px solid {applied_badge_border};color:{applied_badge_fg};font-size:0.76rem;font-weight:600;">已采纳</span>'
        )
    if is_review_later:
        state_badges.append(
            f'<span style="padding:4px 10px;border-radius:999px;background:{review_badge_bg};'
            f'border:1px solid {review_badge_border};color:{review_badge_fg};font-size:0.76rem;font-weight:600;">待复核</span>'
        )
    if not state_badges:
        state_badges.append(
            f'<span style="padding:4px 10px;border-radius:999px;background:{detail_panel_bg};'
            f'border:1px solid {shell_border};color:{shell_muted};font-size:0.76rem;font-weight:600;">处理中</span>'
        )

    info_box = (
        f'<div style="padding:14px 14px 12px 14px;border-radius:16px;border:1px solid {shell_border};'
        f'background:{detail_panel_bg};margin:10px 0 12px 0;">'
        f'<div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap;margin-bottom:8px;">'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.8rem;color:{shell_muted} !important;">当前风险摘要</div>'
        f'<div style="display:flex;gap:6px;flex-wrap:wrap;">{"".join(state_badges)}</div></div>'
        f'<div style="display:grid;gap:10px;">'
        f'<div style="padding:10px 12px;border-radius:12px;background:{detail_subpanel_bg};max-height:124px;overflow:auto;">'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.76rem;color:{shell_muted} !important;margin-bottom:4px;">风险说明</div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.88rem;color:{shell_text} !important;line-height:1.72;">{html.escape(risk.get("issue", "无"))}</div>'
        f'</div>'
        f'<div style="padding:10px 12px;border-radius:12px;background:{detail_subpanel_bg};max-height:124px;overflow:auto;">'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.76rem;color:{shell_muted} !important;margin-bottom:4px;">建议条款</div>'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.86rem;color:{shell_text} !important;line-height:1.72;">{html.escape(str(state["display"]) or "无")}</div>'
        f'</div></div></div>'
    )
    st.markdown(info_box, unsafe_allow_html=True)

    if state["warning"]:
        st.caption(f"建议状态：{state['warning']}")

    action_col1, action_col2, action_col3 = st.columns([1, 1, 1])
    with action_col1:
        if state["actionable"]:
            label = "采纳到正文" if focus_idx not in applied_set else "撤销采纳"
            if st.button(label, key=f"apply_risk_workspace_{focus_idx}", use_container_width=True, type="primary" if focus_idx not in applied_set else "secondary"):
                _toggle_applied_risk(focus_idx)
                st.session_state["workspace_notice"] = (
                    f"风险点 {focus_idx + 1} 已采纳到正文。" if focus_idx not in applied_set else f"风险点 {focus_idx + 1} 已撤销采纳。"
                )
                st.rerun()
        elif state["has_display"]:
            st.button("仅说明性意见", key=f"apply_risk_disabled_workspace_{focus_idx}", use_container_width=True, disabled=True)
    with action_col2:
        review_label = "取消待复核" if is_review_later else "标记待复核"
        if st.button(review_label, key=f"review_later_workspace_{focus_idx}", use_container_width=True):
            marked = _toggle_review_later_risk(focus_idx)
            st.session_state["workspace_notice"] = (
                f"风险点 {focus_idx + 1} 已标记为待复核。"
                if marked
                else f"风险点 {focus_idx + 1} 已取消待复核。"
            )
            st.session_state.focus_risk_idx = focus_idx
            st.rerun()
    with action_col3:
        if st.button("清除当前对象", key="clear_focus_risk_workspace", use_container_width=True):
            st.session_state.focus_risk_idx = None
            st.rerun()

    if st.button("清空当前风险对话", key="risk_chat_clear_focus_workspace", use_container_width=True):
        st.session_state.risk_followup_chats[focus_idx] = []
        st.session_state["workspace_notice"] = f"风险点 {focus_idx + 1} 的追问记录已清空。"
        st.rerun()

    st.markdown(
        f'<div style="padding:10px 12px;border-radius:14px;border:1px solid {shell_border};'
        f'background:{chat_panel_bg};margin:12px 0 8px 0;">'
        f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.8rem;color:{shell_muted} !important;margin-bottom:8px;">追问结果</div>'
        f'</div>',
        unsafe_allow_html=True,
    )
    chat_container = st.container(height=220 if hist else 116)
    if hist:
        for m in hist:
            with chat_container.chat_message(m["role"]):
                st.markdown(m["content"])
    else:
        with chat_container:
            st.caption("这里会保留围绕当前风险的问答记录，这样你在看回复时不需要离开当前风险摘要。")

    with st.form(key="risk_followup_form_focus_workspace", clear_on_submit=True):
        q = st.text_area(
            "围绕当前风险继续追问",
            height=92,
            placeholder="例如：若对方拒绝该修改，我方还能保留哪些权利？请给更稳妥的替代表述。",
        )
        submitted = st.form_submit_button("发送给 AI", type="primary", use_container_width=True)

    if submitted and (q or "").strip():
        provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
        if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
            st.error("请先在侧栏填写 OpenAI API Key。")
        elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
            st.error("请先在侧栏填写 Anthropic API Key。")
        else:
            with st.spinner("思考中..."):
                try:
                    client, model_name, _, use_tools = _get_llm_client_and_model()
                    tools, router = resolve_mcp_bundle()
                    if not use_tools:
                        tools = []
                    sys_p = build_risk_followup_system(contract_ctx, risk, focus_idx)
                    thread = [{"role": "system", "content": sys_p}]
                    thread.extend(hist)
                    thread.append({"role": "user", "content": q.strip()})

                    def exec_rf(name: str, args: dict) -> str:
                        return call_tool_sync(router, name, args)

                    reply = completion_with_tool_loop(
                        client,
                        model_name,
                        thread,
                        tools if tools else None,
                        exec_rf,
                        max_tool_rounds=6 if use_tools else 0,
                        temperature=0.35,
                    )
                    hist.append({"role": "user", "content": q.strip()})
                    hist.append({"role": "assistant", "content": reply})
                    st.session_state.risk_followup_chats[focus_idx] = hist
                    st.session_state["workspace_notice"] = f"风险点 {focus_idx + 1} 的追问结果已更新。"
                    st.rerun()
                except Exception as e:
                    st.error(str(e))


def build_risk_followup_system(contract: str, risk: dict, risk_idx: int) -> str:
    suggestion_text = risk.get("suggestion_display") or risk.get("suggestion") or "无"
    suggestion_warning = risk.get("suggestion_warning") or ""
    suggestion_warning_line = f"修改建议状态：{suggestion_warning}\n" if suggestion_warning else ""
    return (
        f"{RISK_FOLLOWUP_PREFIX}\n\n"
        f"=========================\n"
        f"【当前探讨的风险焦点】\n"
        f"风险等级：{risk.get('level', '未知')}\n"
        f"涉事维度：{risk.get('dimension', '未知')}\n"
        f"原文摘录：\n{risk.get('original', '无')}\n"
        f"系统最初指出的问题：\n{risk.get('issue', '无')}\n"
        f"系统初步的修改建议：\n{suggestion_text}\n"
        f"{suggestion_warning_line}"
        f"=========================\n\n"
        f"注意：以上是目前双方正在讨论的核心风险点！请紧密围绕上述【原文摘录】和【指出的问题】来回答用户的提问。\n\n"
        f"以下附上合同部分正文作为背景参考：\n\n--- 合同正文（节选） ---\n"
        f"{(contract or '')[:10000]}"
    )


def resolve_mcp_bundle():
    use = bool(st.session_state.get("use_mcp"))
    if not use or not MCP_CONFIG_PATH.exists():
        return [], {}
    mtime = MCP_CONFIG_PATH.stat().st_mtime
    return cached_mcp_tools(str(MCP_CONFIG_PATH), mtime, True)

def _render_overview_panel(snap: dict) -> None:
    """在右上方渲染合同概览面板。"""
    ct = snap.get("contract_type") or "未识别"
    ov = snap.get("overview") or {}
    risks = snap.get("risks") or []
    selected_template_name = (snap.get("selected_template_name") or "").strip()
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid = sum(1 for r in risks if r.get("level") == "中风险")
    low = sum(1 for r in risks if r.get("level") == "低风险")

    gold = "var(--ml-accent-gold)"
    text_fg = "var(--ml-shell-text)"
    header_bg = "linear-gradient(145deg, var(--ml-shell-surface-strong) 0%, var(--ml-shell-surface) 100%)"
    header_border = "var(--ml-shell-border)"
    header_title = "var(--ml-shell-text)"
    header_meta = "var(--ml-shell-muted)"
    info_bg = "var(--ml-shell-surface)"
    info_border = "var(--ml-shell-border)"
    accent_bg = "var(--ml-shell-surface)"
    accent_border = "var(--ml-shell-border)"
    block_shadow = "0 12px 24px rgba(0,0,0,0.08)"
    header_shadow = "0 16px 34px rgba(0,0,0,0.12)"
    divider_color = "var(--ml-shell-border)"
    badge_styles = {
        "高风险": (
            "rgba(191,98,90,0.14)",
            "#cf635c",
            "rgba(196,101,94,0.18)",
        ),
        "中风险": (
            "rgba(212,176,74,0.14)",
            "#b88731",
            "rgba(212,168,74,0.18)",
        ),
        "低风险": (
            "rgba(122,158,196,0.16)",
            "#6282a7",
            "rgba(122,158,196,0.18)",
        ),
    }

    template_html = ""
    if selected_template_name:
        template_html = (
            f'<div style="margin:10px 0 0 0;font-family:Outfit,Noto Sans SC,sans-serif;'
            f'font-size:0.84rem;color:{header_meta};">'
            f'审校模板：{html.escape(selected_template_name)}</div>'
        )

    badge_html = []
    for label, count in [("高风险", high), ("中风险", mid), ("低风险", low)]:
        bg, fg, border = badge_styles[label]
        badge_html.append(
            f'<span style="padding:6px 15px;border-radius:999px;font-size:0.8rem;font-weight:600;'
            f'background:{bg};color:{fg};border:1px solid {border};">{label} {count}</span>'
        )

    status_counts = _count_review_statuses(
        risks,
        st.session_state.get("applied_risks", set()),
        st.session_state.get("review_later_risks", set()),
    )

    st.markdown(
        f'<div style="padding:18px 20px 16px 20px;border-radius:22px;font-family:Outfit,Noto Sans SC,sans-serif;'
        f'background:{header_bg};border:1px solid {header_border};margin-bottom:14px;position:relative;overflow:hidden;'
        f'box-shadow:{header_shadow};">'
        f'<div style="position:absolute;inset:auto -40px -50px auto;width:148px;height:148px;border-radius:50%;'
        f'background:rgba(184,148,95,0.08);"></div>'
        f'<div style="position:absolute;top:0;left:0;right:0;height:5px;'
        f'background:linear-gradient(90deg, {gold}, rgba(44,62,107,0.14));"></div>'
        f'<div style="position:relative;z-index:1;">'
        f'<div style="display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;align-items:flex-start;">'
        f'<div>'
        f'<div style="font-size:0.71rem;color:{gold};font-weight:700;letter-spacing:0.14em;text-transform:uppercase;">AI 审查报告</div>'
        f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.38rem;font-weight:700;'
        f'color:{header_title};margin-top:4px;letter-spacing:0.01em;">{html.escape(ct)}</div>'
        f'{template_html}'
        f'</div>'
        f'<div style="max-width:280px;font-size:0.82rem;line-height:1.62;color:{header_meta};">'
        f'查看合同概览、风险分布与导出入口。'
        f'</div>'
        f'</div>'
        f'<div style="height:1px;background:{divider_color};margin:12px 0 12px 0;"></div>'
        f'<div style="display:flex;gap:8px;flex-wrap:wrap;">{"".join(badge_html)}</div>'
        f'<div style="margin-top:10px;font-size:0.8rem;color:{header_meta};">待处理 {status_counts["pending"]} · 待复核 {status_counts["review_later"]} · 已采纳 {status_counts["accepted"]}</div>'
        f'</div></div>',
        unsafe_allow_html=True,
    )

    if ov:
        info_items = [
            ("参与方", "、".join(ov.get("parties") or []) or "未明确"),
            ("合同金额", ov.get("amount") or "未明确"),
            ("合同期限", ov.get("duration") or "未明确"),
            ("签署日期", ov.get("sign_date") or "未明确"),
            ("适用法律", ov.get("governing_law") or "未明确"),
        ]

        cols = st.columns(2, gap="medium")
        for i, (label, value) in enumerate(info_items):
            with cols[i % 2]:
                st.markdown(
                    f'<div style="padding:12px 14px;border-radius:16px;margin-bottom:8px;'
                    f'font-family:Outfit,Noto Sans SC,sans-serif;background:{info_bg};'
                    f'border:1px solid {info_border};box-shadow:{block_shadow};">'
                    f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:6px;'
                    f'letter-spacing:0.12em;text-transform:uppercase;">{label}</div>'
                    f'<div style="font-size:0.88rem;color:{text_fg};line-height:1.58;font-weight:400;">'
                    f'{html.escape(str(value))}</div></div>',
                    unsafe_allow_html=True,
                )

        summary = (ov.get("summary") or "").strip()
        if summary:
            st.markdown(
                f'<div style="padding:12px 16px;border-radius:16px;margin-top:2px;'
                f'font-family:Outfit,Noto Sans SC,sans-serif;background:{info_bg};'
                f'border:1px solid {info_border};box-shadow:{block_shadow};">'
                f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:7px;'
                f'letter-spacing:0.12em;text-transform:uppercase;">内容概览</div>'
                f'<div style="font-size:0.88rem;color:{text_fg};line-height:1.65;">{html.escape(summary)}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )

    if risks:
        level_colors = {"高风险": "#cf635c", "中风险": "#b88731", "低风险": "#6282a7"}
        notable = sorted(
            risks,
            key=lambda r: {"高风险": 0, "中风险": 1, "低风险": 2}.get(r.get("level", "低风险"), 2),
        )[:3]
        summary_item_bg = "var(--ml-shell-surface-soft)"
        items_html = "".join(
            f'<div style="display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:12px;'
            f'background:{summary_item_bg};border:1px solid {accent_border};">'
            f'<span style="width:8px;height:8px;margin-top:8px;border-radius:50%;display:inline-block;flex-shrink:0;'
            f'background:{level_colors.get(r.get("level","低风险"), text_fg)};"></span>'
            f'<div style="flex:1;min-width:0;">'
            f'<div style="color:{level_colors.get(r.get("level","低风险"), text_fg)} !important;font-weight:700;'
            f'font-size:0.8rem;line-height:1.4;margin-bottom:3px;">{html.escape(r.get("level", ""))}</div>'
            f'<div style="color:{text_fg} !important;font-size:0.88rem;line-height:1.62;">'
            f'{html.escape((r.get("issue") or "")[:90] + ("…" if len(r.get("issue", "")) > 90 else ""))}'
            f'</div></div></div>'
            for r in notable
        )
        st.markdown(
            f'<div style="padding:12px 15px;border-radius:16px;margin-top:8px;'
            f'font-family:Outfit,Noto Sans SC,sans-serif;background:{accent_bg};'
            f'border:1px solid {accent_border};box-shadow:{block_shadow};">'
            f'<div style="font-size:0.72rem;color:{gold};font-weight:700;margin-bottom:8px;'
            f'letter-spacing:0.12em;text-transform:uppercase;">重点风险摘要</div>'
            f'<div style="display:grid;gap:10px;color:{text_fg} !important;">'
            f'{items_html}</div></div>',
            unsafe_allow_html=True,
        )

def build_export_report_html(snap: dict) -> str:
    """生成可下载的 HTML 分析报告。"""
    import datetime as _dt
    ct = snap.get("contract_type") or "未识别"
    ov = snap.get("overview") or {}
    risks = snap.get("risks") or []
    selected_template_name = (snap.get("selected_template_name") or "").strip()
    high = sum(1 for r in risks if r.get("level") == "高风险")
    mid  = sum(1 for r in risks if r.get("level") == "中风险")
    low  = sum(1 for r in risks if r.get("level") == "低风险")
    now_str = _dt.datetime.now().strftime("%Y年%m月%d日 %H:%M")

    _lc = {"高风险": ("#9b3030", "#fdf5f4"), "中风险": ("#8b6a25", "#fdf8f0"), "低风险": ("#3a5a8b", "#f2f6fb")}
    _dc = {"法律合规": "#2c4a6e", "风险防控": "#8b3535", "条款完善": "#8b6a25", "利益保护": "#2e6b45"}

    # 概览信息行
    ov_rows = ""
    if ov:
        parties = "、".join(ov.get("parties") or []) or "未明确"
        if selected_template_name:
            ov_rows += f"<tr><td class='label'>审校模板</td><td>{html.escape(selected_template_name)}</td></tr>"
        for label, value in [
            ("参与方", parties),
            ("合同金额", ov.get("amount") or "未明确"),
            ("合同期限", ov.get("duration") or "未明确"),
            ("签署日期", ov.get("sign_date") or "未明确"),
            ("适用法律", ov.get("governing_law") or "未明确"),
        ]:
            ov_rows += f"<tr><td class='label'>{label}</td><td>{html.escape(str(value))}</td></tr>"

    # 主要风险
    notable = sorted(risks, key=lambda r: {"高风险":0,"中风险":1,"低风险":2}.get(r.get("level","低风险"),2))[:3]
    issues_li = "".join(
        f'<li><span style="color:{_lc.get(r.get("level","低风险"), ("#5a5650","#f5f5f5"))[0]};font-weight:700;font-size:.82rem;">'
        f'{html.escape(r.get("level",""))}</span> ' +
        html.escape((r.get("issue") or "")[:100] + ("…" if len(r.get("issue",""))>100 else "")) + "</li>"
        for r in notable
    )

    # 逐条风险
    risk_rows = ""
    for i, risk in enumerate(risks):
        level = risk.get("level", "低风险")
        dim   = risk.get("dimension", "")
        lc, lb = _lc.get(level, ("#546e7a", "#f5f5f5"))
        dc = _dc.get(dim, "#546e7a")
        sugg = risk.get("suggestion_display") or risk.get("suggestion") or ""
        sugg_warning = risk.get("suggestion_warning") or ""
        lb_  = risk.get("legal_basis") or ""
        sugg_html = f'<div class="rs suggestion"><strong>修改建议：</strong>{html.escape(sugg)}</div>' if sugg else ""
        if sugg_warning:
            sugg_html += f'<div class="rs legal"><strong>应用状态：</strong>{html.escape(sugg_warning)}</div>'
        lb_html   = f'<div class="rs legal"><strong>法律依据：</strong>{html.escape(lb_)}</div>' if lb_ and lb_ != "暂无明确法条依据" else ""
        risk_rows += (
            f'<div class="ri" style="border-left:4px solid {lc};background:{lb};">' +
            f'<div class="rh"><span class="rn" style="color:{lc};">风险点 {i+1} · {html.escape(level)}</span>' +
            f'<span class="db" style="color:{dc};border-color:{dc};">{html.escape(dim)}</span></div>' +
            f'<div class="rs"><strong>原文摘录：</strong><span class="orig">{html.escape(risk.get("original",""))}</span></div>' +
            f'<div class="rs"><strong>风险说明：</strong>{html.escape(risk.get("issue",""))}</div>' +
            sugg_html + lb_html +
            f'</div>'
        )

    ov_section = ""
    if ov:
        summary_html = ""
        if ov.get("summary"):
            summary_html = f'<div class="summary-box" style="margin-top:10px;"><strong>内容概览：</strong>{html.escape(ov["summary"])}</div>'
        ov_section = f'<div class="section"><h2>📋 合同概览</h2><table>{ov_rows}</table>{summary_html}</div>'

    issues_section = f'<div class="section"><h2>⚠️ 主要风险概括</h2><div class="alert"><ul>{issues_li}</ul></div></div>' if issues_li else ""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>合同审查报告 - {html.escape(ct)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=Outfit:wght@300;400;500;600;700&family=Noto+Serif+SC:wght@400;600;700&family=Noto+Sans+SC:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
body{{font-family:"Outfit","Noto Sans SC","Microsoft YaHei",sans-serif;margin:0;padding:32px;background:#f6f2ea;color:#1a1f2e;line-height:1.65;}}
.wrap{{max-width:960px;margin:0 auto;background:#ffffff;border-radius:22px;box-shadow:0 18px 40px rgba(26,31,46,0.08);overflow:hidden;}}
.hdr{{background:linear-gradient(145deg,#fcfaf6 0%,#efe6d8 100%);color:#1a1f2e;padding:36px 40px;position:relative;overflow:hidden;border-bottom:1px solid #e9dece;}}
.hdr::before{{content:'';position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,#b8945f,rgba(44,62,107,0.12));}}
.hdr::after{{content:'';position:absolute;bottom:-50px;right:-30px;width:160px;height:160px;border-radius:50%;background:rgba(184,148,95,0.08);}}
.hdr h1{{margin:0 0 6px 0;font-size:1.6rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;font-weight:700;color:#1a1f2e;letter-spacing:0.02em;}}
.hdr .ct{{font-size:1rem;font-weight:600;color:#3b4760;margin:6px 0;}}
.hdr .meta{{font-size:.82rem;color:#7d756a;margin-top:4px;}}
.badges{{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap;}}
.badge{{padding:4px 14px;border-radius:20px;font-size:.78rem;font-weight:600;backdrop-filter:blur(4px);}}
.bh{{background:rgba(196,101,94,0.10);color:#9b4c46;border:1px solid rgba(196,101,94,0.18);}}
.bm{{background:rgba(212,168,74,0.10);color:#99742f;border:1px solid rgba(212,168,74,0.18);}}
.bl{{background:rgba(122,158,196,0.10);color:#4d6d93;border:1px solid rgba(122,158,196,0.18);}}
.section{{padding:24px 40px;border-bottom:1px solid #ece8e1;}}
.section h2{{font-size:1.1rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;color:#1a1f2e;margin:0 0 14px 0;padding-bottom:8px;border-bottom:2px solid #e2ddd5;font-weight:700;}}
table{{width:100%;border-collapse:collapse;}}
td{{padding:8px 12px;font-size:.88rem;border-bottom:1px solid #ece8e1;vertical-align:top;}}
td.label{{color:#b8945f;font-weight:600;width:90px;white-space:nowrap;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em;}}
.summary-box{{padding:10px 14px;border-radius:8px;background:#faf8f5;border-left:3px solid #b8945f;font-size:.87rem;line-height:1.7;}}
.alert{{background:#fdf6ec;border-left:3px solid #b8945f;padding:12px 16px;border-radius:8px;border:1px solid #e8dcc8;}}
.alert ul{{margin:0;padding-left:16px;}}
.alert li{{margin-bottom:5px;font-size:.86rem;line-height:1.55;}}
.ri{{padding:14px 16px;border-radius:10px;margin-bottom:12px;border:1px solid #ece8e1;}}
.rh{{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;}}
.rn{{font-weight:700;font-size:.92rem;font-family:"Cormorant Garamond","Noto Serif SC",serif;}}
.db{{font-size:.72rem;padding:2px 10px;border-radius:10px;border:1px solid;font-weight:500;}}
.rs{{font-size:.86rem;margin-bottom:6px;line-height:1.6;color:#2d2d2d;}}
.orig{{color:#5a5650;background:rgba(184,148,95,0.06);padding:3px 6px;border-radius:4px;border-left:2px solid #d4cfc7;display:inline;}}
.suggestion{{padding:8px 12px;background:#faf8f5;border-radius:6px;border-left:2px solid #b8945f;}}
.legal{{font-size:.8rem;color:#8a857f;}}
.foot{{text-align:center;padding:18px;font-size:.76rem;color:#8a857f;background:#f2efe9;letter-spacing:.02em;}}
</style></head>
<body><div class="wrap">
<div class="hdr">
  <h1>合同审查分析报告</h1>
  <div class="ct">{html.escape(ct)}</div>
  <div class="meta">生成时间：{now_str} · 智审法务 AI 系统</div>
  {"<div class='meta'>审校模板：" + html.escape(selected_template_name) + "</div>" if selected_template_name else ""}
  <div class="badges">
    <span class="badge bh">高风险 {high}</span>
    <span class="badge bm">中风险 {mid}</span>
    <span class="badge bl">低风险 {low}</span>
  </div>
</div>
{ov_section}
{issues_section}
<div class="section"><h2>逐条风险分析（共 {len(risks)} 条）</h2>{risk_rows}</div>
<div class="foot">本报告由 AI 自动生成，仅供参考，不构成正式法律意见。如需专业法律建议，请咨询执业律师。</div>
</div></body></html>"""


tab_review = st.tabs(["合同审查"])[0]

with tab_review:
    st.markdown(
        """
        <div class="intake-shell">
          <div class="intake-kicker">Partner's Review Desk</div>
          <h1 class="intake-title">合同审查</h1>
          <div class="intake-copy">
            上传或粘贴合同文本，系统将生成风险概览、条款建议和可导出的修订结果。
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    col1, col2 = st.columns([1.02, 0.98], gap="large")

    review_templates = _get_review_templates()
    template_option_ids = ["auto"] + [template["id"] for template in review_templates]

    with col1:
        with st.container(border=True):
            st.markdown('<div class="home-card-title">输入合同</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="home-card-copy">选择一种输入方式即可。上传文件适合 Word / PDF / 图片扫描件，粘贴文本适合已经拿到纯文本的合同内容。</div>',
                unsafe_allow_html=True,
            )
            input_mode = st.radio(
                "输入方式",
                ["上传合同文件", "直接粘贴文本"],
                horizontal=True,
                key="contract_input_mode",
            )

            uploaded_file = None
            current_upload_id = None
            contract_text = ""
            if input_mode == "上传合同文件":
                st.markdown('<div class="home-section-label">上传文件</div>', unsafe_allow_html=True)
                uploaded_file = st.file_uploader(
                    "拖拽文件到此或点击上传 (支持 .docx, .pdf, .png, .jpg, .jpeg)",
                    type=["docx", "pdf", "png", "jpg", "jpeg"],
                    label_visibility="collapsed",
                )
                st.caption("支持 .docx、.pdf、.png、.jpg、.jpeg")

                if uploaded_file is not None:
                    current_upload_id = (
                        uploaded_file.name,
                        uploaded_file.size,
                        getattr(uploaded_file, "type", ""),
                    )
                    if st.session_state.get("last_uploaded_file_id") != current_upload_id:
                        st.session_state["last_uploaded_file_id"] = current_upload_id

                ocr_status = get_paddle_ocr_status()
                if ocr_status["ready"]:
                    st.caption(f"OCR 状态：已初始化，可识别扫描件。模型缓存目录：{ocr_status['cache_dir']}")
                else:
                    st.warning(
                        f"OCR 状态：未初始化。若需识别扫描 PDF 或图片，请先运行 `{get_ocr_init_command()}`。"
                    )
            else:
                st.markdown('<div class="home-section-label">粘贴文本</div>', unsafe_allow_html=True)
                contract_text = st.text_area(
                    "直接粘贴合同文本：",
                    height=260,
                    placeholder="在此输入需要审查的合同内容...",
                    key="contract_input",
                    label_visibility="collapsed",
                )

            st.markdown('<div class="home-section-label">审查参数</div>', unsafe_allow_html=True)
            st.radio(
                "审校深度",
                ["快速审查", "标准审查", "深度审查"],
                captions=["约 30s, 聚焦核心风险", "约 1-2min, 适合大多数合同", "约 3-5min, 更细的逐条审查"],
                horizontal=True,
                key="review_depth",
                index=1,
            )
            st.radio(
                "审校立场",
                ["中立视角", "委托方视角", "相对方视角"],
                horizontal=True,
                key="review_perspective",
                index=0,
            )
            st.selectbox(
                "审校模板",
                options=template_option_ids,
                key="selected_review_template_id",
                on_change=save_settings,
                help="模板会把专项审校重点附加到本次审查提示词中。",
                format_func=lambda template_id: (
                    "自动识别（使用通用四维策略）"
                    if template_id == "auto"
                    else format_template_option_label(
                        get_review_template_by_id(review_templates, template_id) or {"name": template_id}
                    )
                ),
            )
            selected_template = get_review_template_by_id(
                review_templates,
                st.session_state.get("selected_review_template_id", "auto"),
            )
            if selected_template:
                template_scope = CONTRACT_TYPE_LABELS.get(
                    selected_template.get("bound_contract_type") or "",
                    "不限定合同类型",
                )
                st.caption(f"当前模板：{selected_template['name']} · {template_scope}")
                with st.expander("查看模板重点", expanded=False):
                    if selected_template.get("prompt"):
                        st.markdown(selected_template["prompt"].replace("\n", "  \n"))
                    else:
                        st.caption("当前模板未设置额外专项审校重点，将沿用基础四维审查框架。")
            analyze_button = st.button("开始审查", type="primary", use_container_width=True)

    with col2:
        with st.container(border=True):
            st.markdown('<div class="home-card-title">审查结果</div>', unsafe_allow_html=True)
            st.markdown(
                '<div class="home-card-copy">这里展示本次审查的提示摘要，以及审查完成后的合同概览、风险分布和导出入口。</div>',
                unsafe_allow_html=True,
            )

            template_display = selected_template["name"] if selected_template else "自动识别（通用四维策略）"
            prompt_preview = (
                (selected_template.get("prompt") or "").strip()
                if selected_template
                else "将使用基础四维审查策略，围绕法律合规、风险防控、条款完善和利益保护展开审查。"
            )
            if len(prompt_preview) > 180:
                prompt_preview = prompt_preview[:180] + "…"

            st.markdown(
                f'<div class="home-section-label">本次审查提示</div>'
                f'<div class="home-note">将沿用左侧当前 AI 配置与参数执行本次审查。'
                f'当前模板：{html.escape(template_display)}。'
                f'<br><br>{html.escape(prompt_preview)}</div>',
                unsafe_allow_html=True,
            )

            if not analyze_button:
                snap_preview = st.session_state.get("review_snapshot")
                if snap_preview and snap_preview.get("contract_type"):
                    st.markdown('<div class="home-section-label">最近一次审查结果</div>', unsafe_allow_html=True)
                    _render_overview_panel(snap_preview)
                    if snap_preview.get("risks"):
                        import datetime as _dt
                        _fname = f"合同审查报告_{_dt.datetime.now().strftime('%Y%m%d_%H%M')}.html"
                        st.download_button(
                            "导出分析报告 (HTML)",
                            data=build_export_report_html(snap_preview).encode("utf-8"),
                            file_name=_fname,
                            mime="text/html",
                            use_container_width=True,
                            key="export_btn_top",
                        )
                else:
                    st.markdown(
                        '<div class="home-placeholder">配置好左侧内容后点击“开始审查”，这里会直接展示合同概览、风险分布和导出入口，不再单独占一块空白区域。</div>',
                        unsafe_allow_html=True,
                    )

    if analyze_button:
            provider_choice = st.session_state.get("ai_provider_radio", "OpenAI")
            depth_choice = st.session_state.get("review_depth", "标准审查")
            persp_choice = st.session_state.get("review_perspective", "中立视角")
            input_mode = st.session_state.get("contract_input_mode", "上传合同文件")
            selected_template = get_review_template_by_id(
                _get_review_templates(),
                st.session_state.get("selected_review_template_id", "auto"),
            )

            if provider_choice == "OpenAI" and not st.session_state.get("openai_api_key"):
                st.error("请先在左侧边栏输入 OpenAI API Key！")
            elif provider_choice == "Anthropic" and not st.session_state.get("anthropic_api_key"):
                st.error("请先在左侧边栏输入 Anthropic API Key！")
            elif input_mode == "上传合同文件" and not uploaded_file:
                st.warning("请先上传需要审查的合同文件！")
            elif input_mode == "直接粘贴文本" and not contract_text.strip():
                st.warning("请先粘贴需要审查的合同内容！")
            else:
                final_text = ""
                try:
                    if input_mode == "上传合同文件" and uploaded_file:
                        file_suffix = Path(uploaded_file.name).suffix.lower()
                        parse_message = "正在解析上传文件..."
                        if file_suffix in {".pdf", ".png", ".jpg", ".jpeg"}:
                            parse_message = "正在解析上传文件，扫描件会先执行 OCR，请稍候..."
                        with st.spinner(parse_message):
                            file_bytes = uploaded_file.getvalue()
                            final_text = extract_text(uploaded_file, file_bytes=file_bytes)
                            st.session_state.original_file_bytes = file_bytes
                            st.session_state.original_file_name = uploaded_file.name
                    else:
                        final_text = contract_text.strip()
                        st.session_state.original_file_bytes = None
                        st.session_state.original_file_name = None
                except Exception as e:
                    st.error(f"文件解析失败：{str(e)}")
                    final_text = ""

                if final_text:
                    st.session_state.contract_text_for_chat = final_text
                    st.session_state.modified_contract_text = final_text
                    st.session_state.applied_risks = set()
                    st.session_state.show_export_dialog = False

                    with st.spinner("AI 正在逐条比对审查中，请稍候..."):
                        try:
                            client, model_name, provider_choice, use_tools = _get_llm_client_and_model()
                            if provider_choice == "Anthropic":
                                st.warning("注：Anthropic需确保URL指向兼容网关(如LiteLLM)。")

                            tools, router = resolve_mcp_bundle()
                            if not use_tools:
                                tools = []

                            system_prompt = build_dynamic_review_system(
                                depth_choice,
                                persp_choice,
                                selected_template=selected_template,
                            )
                            if tools:
                                system_prompt += REVIEW_MCP_SUFFIX

                            messages = [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": f"请审查以下合同文本：\n\n{final_text[:4000]}"},
                            ]

                            def exec_tool(name: str, args: dict) -> str:
                                return call_tool_sync(router, name, args)

                            result_content = completion_with_tool_loop(
                                client,
                                model_name,
                                messages,
                                tools if tools else None,
                                exec_tool,
                                max_tool_rounds=2 if depth_choice == "快速审查" else 6,
                                temperature=0.4 if depth_choice == "快速审查" else 0.1,
                            )

                            if result_content.startswith("```json"):
                                result_content = result_content[7:-3].strip()
                            elif result_content.startswith("```"):
                                result_content = result_content[3:-3].strip()

                            parsed = json.loads(result_content)
                            if isinstance(parsed, dict) and "risks" in parsed:
                                contract_type = parsed.get("contract_type") or "未识别"
                                overview = parsed.get("overview") or {}
                                risks = parsed.get("risks") or []
                                if not isinstance(risks, list):
                                    risks = []
                            elif isinstance(parsed, list):
                                contract_type = "未分类"
                                overview = {}
                                risks = parsed
                            else:
                                raise ValueError("模型返回既不是对象也不是数组")

                            risks = postprocess_review_risks(risks, final_text)
                            actionable_risk_indices = get_actionable_risk_indices(risks)
                            non_actionable_count = max(0, len(risks) - len(actionable_risk_indices))
                            snapshot = _build_review_snapshot(final_text, contract_type, overview, risks, selected_template)
                            st.session_state.review_snapshot = snapshot
                            st.session_state.risk_followup_chats = {}
                            st.session_state.focus_risk_idx = None
                            st.session_state.review_later_risks = set()
                            st.session_state.workspace_notice = None

                            if not risks:
                                st.success("✅ 审查完成！未发现明显法律风险。")
                            else:
                                st.success(f"✅ 审查完成！共发现 **{len(risks)}** 处风险。")
                                if non_actionable_count:
                                    st.warning(
                                        f"其中 {non_actionable_count} 条仅生成了说明性修改意见，未给出可直接替换的合同条款，已禁止一键应用。"
                                    )

                            st.rerun()

                        except Exception as e:
                            st.error(f"调用 AI 服务时出错，请检查 API Key、Base URL 或网络连接：{str(e)}")

    snap = st.session_state.review_snapshot
    if snap and snap.get("risks"):
        theme_key = _active_theme_key()
        shell_bg = "var(--ml-shell-surface-strong)"
        shell_border = "var(--ml-shell-border)"
        shell_shadow = "0 18px 36px rgba(0,0,0,0.12)"
        shell_text = "var(--ml-shell-text)"
        shell_muted = "var(--ml-shell-muted)"

        st.divider()
        st.markdown(
            f'<div style="margin:2px 0 16px 0;">'
            f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-weight:700;'
            f'font-size:1.82rem;color:{shell_text};margin-bottom:4px;">合同审稿</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.92rem;'
            f'color:{shell_muted};line-height:1.72;max-width:920px;">'
            f'查看风险、核对正文、确认修改并导出最终文档。'
            f'</div></div>',
            unsafe_allow_html=True,
        )

        _render_workspace_header(snap, theme_key, shell_bg, shell_border, shell_shadow, shell_text, shell_muted)
        workspace_notice = st.session_state.pop("workspace_notice", None)
        if workspace_notice:
            st.success(workspace_notice)
        st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

        action_col1, action_col2, action_col3 = st.columns([1.0, 1.0, 1.0], gap="medium")
        with action_col1:
            only_pending = st.toggle("只看未处理", key="workspace_only_pending")
        with action_col2:
            if st.button("一键应用全部可替换建议", use_container_width=True, key="apply_all_workspace"):
                actionable_indices = get_actionable_risk_indices(snap["risks"])
                if actionable_indices:
                    st.session_state.applied_risks.update(actionable_indices)
                    st.session_state.review_later_risks.difference_update(actionable_indices)
                    st.session_state.workspace_notice = f"已批量采纳 {len(actionable_indices)} 条可替换建议。"
                    st.session_state.show_export_dialog = True
                    st.rerun()
                else:
                    st.warning("当前没有可直接替换回正文的修订条款。")
        with action_col3:
            if st.button("打开变更确认与导出", use_container_width=True, type="primary", key="export_workspace"):
                st.session_state.show_export_dialog = True
                st.rerun()

        filter_col, status_col, sort_col = st.columns([0.9, 1.0, 1.25], gap="medium")
        with filter_col:
            available_dimensions = [
                dimension
                for dimension in ["法律合规", "风险防控", "条款完善", "利益保护"]
                if any(r.get("dimension") == dimension for r in snap["risks"])
            ]
            dim_filter = st.selectbox(
                "按风险类型筛选",
                ["全部"] + available_dimensions,
                key="dim_filter_workspace",
            )
        with status_col:
            status_filter = st.selectbox(
                "处理状态",
                ["全部", "待处理", "待复核", "已采纳", "仅说明性意见"],
                index=1 if only_pending else 0,
                key="risk_status_filter_workspace",
            )
        with sort_col:
            sort_order = st.radio(
                "风险排序",
                ["原文顺序", "风险高到低", "风险低到高"],
                horizontal=True,
                key="risk_sort_order_workspace",
            )

        risks_with_idx = _build_workspace_risk_list(
            snap["risks"],
            dim_filter,
            sort_order,
            "待处理" if only_pending and status_filter == "全部" else status_filter,
        )
        deck_html = build_risk_deck_html(
            risks_with_idx,
            theme_key,
            st.session_state.get("applied_risks", set()),
            st.session_state.get("review_later_risks", set()),
        )
        applied = st.session_state.get("applied_risks", set())
        hl_html, _ = get_highlighted_contract_html(snap["text"], snap["risks"], theme_key, applied)

        c1, c2, c3 = st.columns([0.92, 1.46, 1.34], gap="large")

        with c1:
            st.markdown(
                f'<div style="padding:16px 18px 12px 18px;border-radius:20px;background:{shell_bg};'
                f'border:1px solid {shell_border};box-shadow:{shell_shadow};margin-bottom:12px;">'
                f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-size:1.22rem;'
                f'font-weight:700;color:{shell_text};">风险队列</div>'
                f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.84rem;color:{shell_muted};'
                f'line-height:1.72;margin-top:4px;max-width:20rem;">左侧用于浏览和筛选风险，定位原文并切换当前处理对象。</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            focus_from_js_deck = risk_deck_component(cards_html=deck_html, theme_key=theme_key, key="risk_deck_v2")
            if focus_from_js_deck and isinstance(focus_from_js_deck, dict):
                new_idx = int(focus_from_js_deck.get("idx", -1))
                new_ts = focus_from_js_deck.get("ts", 0)
                action = focus_from_js_deck.get("action", "focus")
                if new_idx >= 0 and new_ts != st.session_state.get("last_deck_ts_v2"):
                    st.session_state["last_deck_ts_v2"] = new_ts
                    actual_idx = _resolve_component_idx(new_idx, risks_with_idx)
                    if action == "apply":
                        _toggle_applied_risk(actual_idx)
                    else:
                        st.session_state.focus_risk_idx = actual_idx
                    st.rerun()

        with c2:
            _render_contract_canvas(theme_key, shell_bg, shell_border, shell_shadow, shell_text, shell_muted, hl_html)

        with c3:
            _render_decision_panel(snap, theme_key, shell_bg, shell_border, shell_shadow, shell_text, shell_muted)

        _render_change_review_panel(snap, shell_bg, shell_border, shell_shadow, shell_text, shell_muted)

        if st.session_state.get("show_export_dialog"):
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)
            from legal_review.document_editor import execute_export_pipeline
            execute_export_pipeline()

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
        st.markdown(
            f'<div style="padding:0 2px 4px 2px;">'
            f'<div style="font-family:Cormorant Garamond,Noto Serif SC,serif;font-weight:700;'
            f'font-size:1.3rem;color:{shell_text};">补充视角</div>'
            f'<div style="font-family:Outfit,Noto Sans SC,sans-serif;font-size:0.86rem;color:{shell_muted};'
            f'line-height:1.65;margin-top:4px;">'
            f'从条款修订和履约双方两个方向继续阅读，不打断中间的合同工作区。'
            f'</div></div>',
            unsafe_allow_html=True,
        )
        tab_clause, tab_party = st.tabs(["条款修订", "履约方影响"])
        with tab_clause:
            from legal_review.perspectives import render_clause_centric_view
            render_clause_centric_view(snap["risks"], theme_key)
        with tab_party:
            from legal_review.perspectives import render_party_centric_view
            render_party_centric_view(snap["overview"], snap["risks"], theme_key)


