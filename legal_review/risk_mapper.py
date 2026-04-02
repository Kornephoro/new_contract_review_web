from __future__ import annotations

from copy import deepcopy
import re
from typing import Iterable

from legal_review.masking import MaskResult, restore_masked_text


ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
WORDISH_RE = re.compile(r"[\w\u4e00-\u9fa5]")


def _normalize_text(value: str) -> str:
    value = ZERO_WIDTH_RE.sub("", value or "")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _build_filtered_mapping(full_text: str, keep_char) -> tuple[str, list[int]]:
    chars: list[str] = []
    mapping: list[int] = []
    for idx, ch in enumerate(full_text or ""):
        if keep_char(ch):
            chars.append(ch)
            mapping.append(idx)
    return "".join(chars), mapping


def _strict_find_text_span(full_text: str, query: str) -> tuple[int, int] | None:
    query = _normalize_text(query)
    if not query:
        return None

    exact_pos = (full_text or "").find(query)
    if exact_pos != -1:
        return exact_pos, exact_pos + len(query)

    filtered_full, full_map = _build_filtered_mapping(full_text, lambda ch: not ch.isspace())
    filtered_query, _ = _build_filtered_mapping(query, lambda ch: not ch.isspace())
    if filtered_query:
        pos = filtered_full.find(filtered_query)
        if pos != -1:
            return full_map[pos], full_map[pos + len(filtered_query) - 1] + 1

    filtered_full_punc, full_map_punc = _build_filtered_mapping(full_text, lambda ch: bool(WORDISH_RE.match(ch)))
    filtered_query_punc, _ = _build_filtered_mapping(query, lambda ch: bool(WORDISH_RE.match(ch)))
    if filtered_query_punc:
        pos = filtered_full_punc.find(filtered_query_punc)
        if pos != -1:
            return full_map_punc[pos], full_map_punc[pos + len(filtered_query_punc) - 1] + 1

    return None


def _restore_risk_value(value, mask_result: MaskResult | None):
    if isinstance(value, str):
        return restore_masked_text(value, mask_result)
    if isinstance(value, list):
        return [_restore_risk_value(item, mask_result) for item in value]
    if isinstance(value, dict):
        return {key: _restore_risk_value(item, mask_result) for key, item in value.items()}
    return value


def _map_single_risk_to_raw(risk: dict, raw_text: str, mask_result: MaskResult | None) -> dict:
    raw_risk = _restore_risk_value(deepcopy(risk or {}), mask_result)

    original = _normalize_text(str(raw_risk.get("original") or ""))
    suggestion = _normalize_text(str(raw_risk.get("suggestion") or ""))

    span = _strict_find_text_span(raw_text, original) if original else None
    export_original = original
    export_ready = bool(suggestion)
    export_warning = ""

    if original:
        if span is not None:
            start, end = span
            export_original = raw_text[start:end]
            raw_risk["raw_original_span"] = {"start": start, "end": end}
        else:
            export_ready = False
            export_warning = "该风险条款未能从脱敏结果可靠映射回原文，已跳过正式导出。"
    elif suggestion:
        export_ready = False
        export_warning = "该风险缺少可定位的原文摘录，已跳过正式导出。"

    raw_risk["original"] = export_original
    raw_risk["export_actionable"] = bool(export_ready)
    if export_warning:
        raw_risk["export_warning"] = export_warning
    elif "export_warning" in raw_risk:
        raw_risk.pop("export_warning", None)
    return raw_risk


def build_raw_export_risks(
    review_risks: Iterable[dict],
    raw_text: str,
    mask_result: MaskResult | None,
) -> list[dict]:
    return [
        _map_single_risk_to_raw(risk, raw_text or "", mask_result)
        for risk in (review_risks or [])
    ]
