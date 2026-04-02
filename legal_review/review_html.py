"""审查结果区：可拖拽风险卡片 + 追问投放区（iframe 内，通过 URL 参数与 Streamlit 同步）。"""

from __future__ import annotations

import html

from legal_review.review_postprocess import get_risk_suggestion_state


def build_risk_deck_html(
    risks_with_idx: list,
    _theme_key: str,
    applied_risks: set = None,
    review_later_risks: set = None,
) -> str:
    """
    左：风险卡片（可拖拽、可点击选为追问）。
    「墨律」editorial design — premium warm aesthetic with serif titles,
    colored dot indicators, and refined dimension badges.
    """
    # -- 墨律 Design Tokens --------------------------------------------------
    FONT_BODY = '"Outfit","Noto Sans SC",sans-serif'
    FONT_TITLE = '"Cormorant Garamond","Noto Serif SC",serif'

    CARD_BG = "var(--deck-card-bg, #fffaf7)"
    CARD_BORDER = "var(--deck-card-border, #eadfd2)"
    BODY_FG = "var(--deck-body-fg, #1f2633)"
    MUTED_FG = "var(--deck-muted-fg, #6f7682)"
    INSET_BG = "var(--deck-card-inset, rgba(0, 0, 0, 0.025))"
    NEUTRAL_BADGE_BG = "var(--deck-neutral-badge-bg, rgba(255, 255, 255, 0.78))"
    NEUTRAL_BADGE_FG = "var(--deck-neutral-badge-fg, #4f6278)"

    dim_colors = {
        "法律合规": ("#6282a7", "rgba(98,130,167,0.10)"),
        "风险防控": ("#cf635c", "rgba(207,99,92,0.10)"),
        "条款完善": ("#b88731", "rgba(184,135,49,0.10)"),
        "利益保护": ("#5ba06e", "rgba(91,160,110,0.10)"),
    }
    level_styles = {
        "高风险": ("#cf635c", CARD_BG, CARD_BORDER, "#cf635c", "#cf635c"),
        "中风险": ("#b88731", CARD_BG, CARD_BORDER, "#b88731", "#b88731"),
        "低风险": ("#6282a7", CARD_BG, CARD_BORDER, "#6282a7", "#6282a7"),
    }

    ACCENT_GOLD = "#b8945f"

    applied_risks = applied_risks or set()
    review_later_risks = review_later_risks or set()
    cards = []

    # risks_with_idx -> list of (actual_global_idx, risk_dict)
    for dom_idx, (actual_idx, risk) in enumerate(risks_with_idx):
        level = risk.get("level", "低风险")
        dim = risk.get("dimension") or "风险防控"

        dot_color, card_bg, card_border, left_color, title_color = level_styles.get(
            level, level_styles["低风险"]
        )
        dim_fg, dim_bg = dim_colors.get(dim, ("#546e7a", "rgba(84,110,122,0.08)"))

        issue_text = risk.get("issue") or ""
        issue_snippet = issue_text[:160] + ("\u2026" if len(issue_text) > 160 else "")

        suggestion_state = get_risk_suggestion_state(risk)
        suggestion = str(suggestion_state["display"] or "")
        suggestion_actionable = bool(suggestion_state["actionable"])
        suggestion_warning = str(suggestion_state["warning"] or "")
        legal_basis = (risk.get("legal_basis") or "").strip()

        has_sugg = bool(suggestion_state["has_display"])
        is_applied = (actual_idx in applied_risks)
        is_review_later = (actual_idx in review_later_risks)

        loc_js = (
            f"try{{var el=window.parent.document.getElementById('risk-anchor-{actual_idx}');"
            f"if(el)el.scrollIntoView({{behavior:'smooth',block:'center'}});}}catch(e){{}}"
        )

        # -- Colored dot indicator (replaces emoji circles) -------------------
        dot_html = (
            f'<span style="display:inline-block;width:8px;height:8px;'
            f'border-radius:50%;background:{dot_color};flex-shrink:0;'
            f'margin-top:2px;"></span>'
        )

        # -- 修改建议（折叠） -------------------------------------------------
        suggestion_html = ""
        if suggestion:
            sugg_esc = html.escape(suggestion)
            suggestion_html = (
                f'<details style="margin-top:10px;">'
                f'<summary style="font-family:{FONT_BODY};font-size:0.8rem;'
                f'color:{left_color};cursor:pointer;font-weight:600;'
                f'list-style:none;display:flex;align-items:center;gap:5px;'
                f'letter-spacing:0.02em;">'
                f'<span style="font-size:0.85rem;line-height:1;">&#9998;</span>'
                f'<span>修改建议</span></summary>'
                f'<div style="margin-top:8px;padding:10px 14px;border-radius:6px;'
                f'background:{INSET_BG};'
                f'border-left:3px solid {ACCENT_GOLD};'
                f'font-family:{FONT_BODY};font-size:0.82rem;'
                f'line-height:1.6;color:{BODY_FG};'
                f'letter-spacing:0.01em;">{sugg_esc}</div>'
                f'</details>'
            )
        if suggestion_warning:
            suggestion_html += (
                f'<div style="margin-top:8px;padding:6px 10px;border-radius:6px;'
                f'background:{INSET_BG};'
                f'font-family:{FONT_BODY};font-size:0.76rem;color:{MUTED_FG};line-height:1.5;">'
                f'{html.escape(suggestion_warning)}</div>'
            )

        # -- 法律依据 ---------------------------------------------------------
        legal_html = ""
        if legal_basis and legal_basis != "暂无明确法条依据":
            legal_esc = html.escape(legal_basis)
            legal_html = (
                f'<div style="margin-top:8px;padding:5px 10px;border-radius:5px;'
                f'background:{INSET_BG};'
                f'font-family:{FONT_BODY};font-size:0.76rem;color:{MUTED_FG};'
                f'line-height:1.5;letter-spacing:0.01em;'
                f'display:flex;align-items:flex-start;gap:5px;">'
                f'<span style="flex-shrink:0;color:{MUTED_FG};font-size:0.78rem;">\u00a7</span>'
                f'<span>{legal_esc}</span></div>'
            )

        # -- Card shell -------------------------------------------------------
        state_label = (
            "已采纳"
            if is_applied
            else (
                "待复核"
                if is_review_later
                else ("待处理" if suggestion_actionable else ("说明性意见" if has_sugg else "待查看"))
            )
        )
        state_bg = (
            "rgba(212,176,112,0.14)" if is_applied else
            (
                "rgba(92,110,128,0.18)" if is_review_later else
                (NEUTRAL_BADGE_BG if not suggestion_actionable else NEUTRAL_BADGE_BG)
            )
        )
        state_fg = (
            ACCENT_GOLD if is_applied else
            (NEUTRAL_BADGE_FG if is_review_later else (MUTED_FG if not suggestion_actionable else left_color))
        )
        cards.append(
            f'<div class="risk-card" draggable="true" data-risk-index="{actual_idx}" '
            f'ondragstart="event.dataTransfer.setData(\'text/plain\',\'{actual_idx}\');" '
            f'style="margin-bottom:12px;padding:16px 18px;border-radius:18px;'
            f'border:1px solid {card_border};border-left:3px solid {left_color};'
            f'background:{card_bg};cursor:grab;box-shadow:0 10px 22px rgba(26,31,46,0.04);'
            f'font-family:{FONT_BODY};transition:box-shadow 0.15s ease;">'

            f'<div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;margin-bottom:8px;">'
            f'<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center;">'
            f'{dot_html}'
            f'<span style="font-family:{FONT_TITLE};font-weight:700;'
            f'color:{title_color};font-size:0.95rem;letter-spacing:0.02em;">'
            f'风险点 {actual_idx + 1} · {html.escape(level)}</span>'
            f'<span style="font-family:{FONT_BODY};font-size:0.72rem;'
            f'padding:2px 9px;border-radius:4px;background:{dim_bg};color:{dim_fg};'
            f'font-weight:500;letter-spacing:0.03em;">{html.escape(dim)}</span>'
            f'</div>'
            f'<span style="padding:5px 10px;border-radius:999px;background:{state_bg};color:{state_fg};'
            f'border:1px solid {card_border};font-size:0.72rem;font-weight:600;white-space:nowrap;">{state_label}</span>'
            f'</div>'

            f'<p style="margin:0;font-family:{FONT_BODY};font-size:0.84rem;'
            f'line-height:1.6;color:{BODY_FG};letter-spacing:0.01em;">'
            f'{html.escape(issue_snippet)}</p>'

            f'{suggestion_html}'
            f'{legal_html}'

            f'<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;">'
        )

        # -- 应用 / 撤销 button (only if suggestion exists) -------------------
        if suggestion_actionable:
            if is_applied:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                    f'padding:5px 14px;border-radius:6px;'
                    f'border:1px solid {ACCENT_GOLD};background:rgba(184,148,95,0.1);'
                    f'color:{ACCENT_GOLD};cursor:pointer;font-weight:600;'
                    f'letter-spacing:0.02em;transition:all 0.15s ease;">'
                    f'\u64a4\u9500</button>'
                )
            else:
                cards.append(
                    f'<button type="button" class="apply-btn" data-idx="{dom_idx}" '
                    f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                    f'padding:5px 14px;border-radius:6px;'
                    f'border:1px solid {ACCENT_GOLD};background:{ACCENT_GOLD};'
                    f'color:#fff;cursor:pointer;font-weight:600;'
                    f'letter-spacing:0.02em;transition:all 0.15s ease;">'
                    f'\u5e94\u7528</button>'
                )
        elif has_sugg:
            cards.append(
                f'<button type="button" disabled '
                f'style="font-family:{FONT_BODY};font-size:0.8rem;'
                f'padding:5px 14px;border-radius:6px;'
                f'border:1px solid {CARD_BORDER};background:transparent;'
                f'color:{MUTED_FG};cursor:not-allowed;font-weight:500;letter-spacing:0.02em;">'
                f'\u4e0d\u53ef\u76f4\u63a5\u5e94\u7528</button>'
            )

        # -- 深入追问 & 定位原文 buttons --------------------------------------
        cards.append(
            f'<button type="button" class="pick-btn" data-idx="{dom_idx}" '
            f'style="font-family:{FONT_BODY};font-size:0.8rem;'
            f'padding:5px 14px;border-radius:6px;'
            f'border:1px solid {left_color};background:transparent;'
            f'color:{left_color};cursor:pointer;font-weight:500;'
            f'letter-spacing:0.02em;transition:all 0.15s ease;">'
            f'\u6df1\u5165\u8ffd\u95ee</button>'
            f'<button type="button" onclick="{loc_js}" '
            f'style="font-family:{FONT_BODY};font-size:0.8rem;'
            f'padding:5px 14px;border-radius:6px;'
            f'border:1px solid {CARD_BORDER};background:transparent;'
            f'color:{MUTED_FG};cursor:pointer;font-weight:500;'
            f'letter-spacing:0.02em;transition:all 0.15s ease;">'
            f'\u5b9a\u4f4d\u539f\u6587</button>'
            f'</div></div>'
        )

    return "\n".join(cards)
