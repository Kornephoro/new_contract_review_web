from __future__ import annotations

from copy import deepcopy
import re

from legal_review.masking import MaskResult, contains_mask_token, mask_fragment, restore_masked_text


OVERVIEW_KEYS = ("parties", "amount", "duration", "sign_date", "governing_law", "summary")
PARTY_ROLE_RE = re.compile(
    r"(?im)^\s*(甲方|乙方|丙方|丁方|委托方|受托方|采购方|供货方|买方|卖方|出租方|承租方)\s*[：:]\s*([^\r\n]{2,120})"
)


def _normalize_overview(overview: dict | None) -> dict:
    base = {
        "parties": [],
        "amount": "未明确",
        "duration": "未明确",
        "sign_date": "未明确",
        "governing_law": "未明确",
        "summary": "",
    }
    if not isinstance(overview, dict):
        return base
    normalized = deepcopy(base)
    for key in OVERVIEW_KEYS:
        value = overview.get(key)
        if key == "parties":
            normalized[key] = list(value) if isinstance(value, list) else []
        elif value not in (None, ""):
            normalized[key] = value
    return normalized


def _restore_string(value: str, mask_result: MaskResult | None) -> str:
    restored = restore_masked_text(str(value or ""), mask_result)
    return restored.strip()


def _mask_string(value: str, mask_result: MaskResult | None) -> str:
    if not mask_result:
        return str(value or "").strip()
    return mask_fragment(str(value or "").strip(), mask_result.matches)


def _transform_overview(overview: dict, transform) -> dict:
    transformed = deepcopy(overview)
    transformed["parties"] = [transform(item) for item in overview.get("parties") or [] if str(item or "").strip()]
    for key in ("amount", "duration", "sign_date", "governing_law", "summary"):
        transformed[key] = transform(overview.get(key, ""))
    return transformed


def _clean_party_name(value: str) -> str:
    candidate = (value or "").strip().strip("，,；;。.")
    candidate = re.split(r"\s{2,}", candidate, maxsplit=1)[0]
    candidate = re.split(r"[，,；;。]\s*(?:联系人|电话|手机|邮箱|地址|开户行|账号|统一社会信用代码|身份证号)", candidate, maxsplit=1)[0]
    return candidate.strip().strip("，,；;。.")


def extract_parties_from_raw_text(raw_text: str) -> list[str]:
    parties: list[str] = []
    seen: set[str] = set()
    for match in PARTY_ROLE_RE.finditer(raw_text or ""):
        role = match.group(1)
        name = _clean_party_name(match.group(2))
        if not name:
            continue
        line = f"{role}：{name}"
        if line in seen:
            continue
        seen.add(line)
        parties.append(line)
    return parties


def _overview_has_masked_parties(overview: dict) -> bool:
    return any(contains_mask_token(str(item or "")) for item in overview.get("parties") or [])


def resolve_display_overview(
    review_overview: dict | None,
    raw_text: str,
    mask_result: MaskResult | None,
) -> tuple[dict, dict]:
    normalized_review = _normalize_overview(review_overview)
    display_overview_masked = _transform_overview(normalized_review, lambda value: _mask_string(value, mask_result))
    display_overview_raw = _transform_overview(normalized_review, lambda value: _restore_string(value, mask_result))

    extracted_parties_raw = extract_parties_from_raw_text(raw_text)
    if extracted_parties_raw:
        display_overview_raw["parties"] = extracted_parties_raw
        display_overview_masked["parties"] = [
            _mask_string(party, mask_result) for party in extracted_parties_raw
        ]
    elif _overview_has_masked_parties(normalized_review):
        display_overview_raw["parties"] = [
            _restore_string(party, mask_result) for party in normalized_review.get("parties") or []
        ]

    return display_overview_raw, display_overview_masked
