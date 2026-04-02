from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


MASK_TOKEN_RE = re.compile(r"\[[^\[\]]+?_\d+\]")


@dataclass(frozen=True)
class MaskMatch:
    category: str
    raw_value: str
    masked_value: str
    start: int
    end: int


@dataclass
class MaskResult:
    raw_text: str
    masked_text: str
    matches: list[MaskMatch]
    stats: dict[str, int]

    @property
    def enabled(self) -> bool:
        return bool(self.matches)

    def raw_to_masked_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for match in self.matches:
            mapping[match.raw_value] = match.masked_value
        return mapping

    def masked_to_raw_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for match in self.matches:
            mapping[match.masked_value] = match.raw_value
        return mapping


DEFAULT_ENABLED_CATEGORIES = {
    "手机号",
    "邮箱",
    "身份证号",
    "统一社会信用代码",
    "银行账号",
    "公司名称",
    "姓名",
    "地址",
}

LINE_BREAK_RE = re.compile(r"[\r\n]+")
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")
ID_CARD_RE = re.compile(r"(?<![0-9A-Za-z])([1-9]\d{5}(?:18|19|20)?\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx])(?![0-9A-Za-z])")
USCC_RE = re.compile(r"(?<![0-9A-Z])([0-9A-Z]{18})(?![0-9A-Z])")
MASKED_BANK_LABEL_RE = re.compile(
    r"(?i)(?:银行账号|银行账户|收款账户|开户账号|账号|账户)\s*[：: ]\s*([0-9]{10,30})"
)

PARTY_LINE_PATTERNS = [
    re.compile(r"(?im)^\s*(甲方|乙方|丙方|丁方|委托方|受托方|采购方|供货方|买方|卖方|出租方|承租方)\s*[：:]\s*([^\r\n]{2,120})"),
]
PERSON_LINE_PATTERNS = [
    re.compile(r"(?im)(?:联系人|法定代表人|委托代理人|授权代表|签约代表|姓名)\s*[：:]\s*([A-Za-z\u4e00-\u9fa5]{2,12})"),
]
ADDRESS_LINE_PATTERNS = [
    re.compile(r"(?im)(?:地址|住所|通讯地址|联系地址)\s*[：:]\s*([^\r\n]{5,100})"),
]

COMPANY_SUFFIXES = (
    "有限公司",
    "有限责任公司",
    "股份有限公司",
    "集团有限公司",
    "合伙企业",
    "研究院",
    "事务所",
    "中心",
    "医院",
    "学校",
    "银行",
    "公司",
)


def contains_mask_token(text: str) -> bool:
    return bool(MASK_TOKEN_RE.search(text or ""))


def _clean_candidate(value: str) -> str:
    candidate = (value or "").strip().strip("，,；;。.")
    candidate = re.split(r"\s{2,}", candidate, maxsplit=1)[0]
    candidate = re.split(r"[，,；;。]\s*(?:联系人|电话|手机|邮箱|地址|开户行|账号|统一社会信用代码|身份证号)", candidate, maxsplit=1)[0]
    candidate = candidate.strip().strip("，,；;。.")
    return candidate


def _looks_like_company_name(value: str) -> bool:
    candidate = _clean_candidate(value)
    if len(candidate) < 4 or len(candidate) > 80:
        return False
    return any(candidate.endswith(suffix) for suffix in COMPANY_SUFFIXES)


def _looks_like_person_name(value: str) -> bool:
    candidate = _clean_candidate(value)
    return bool(re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", candidate))


def _add_value(bucket: dict[str, set[str]], category: str, value: str) -> None:
    candidate = _clean_candidate(value)
    if not candidate:
        return
    if category == "公司名称" and not _looks_like_company_name(candidate):
        return
    if category == "姓名" and not _looks_like_person_name(candidate):
        return
    if category == "地址" and len(candidate) < 6:
        return
    bucket.setdefault(category, set()).add(candidate)


def _collect_contextual_values(text: str, enabled_categories: set[str]) -> dict[str, set[str]]:
    bucket: dict[str, set[str]] = {}

    if "公司名称" in enabled_categories:
        for pattern in PARTY_LINE_PATTERNS:
            for match in pattern.finditer(text or ""):
                _add_value(bucket, "公司名称", match.group(2))

    if "姓名" in enabled_categories:
        for pattern in PERSON_LINE_PATTERNS:
            for match in pattern.finditer(text or ""):
                _add_value(bucket, "姓名", match.group(1))

    if "地址" in enabled_categories:
        for pattern in ADDRESS_LINE_PATTERNS:
            for match in pattern.finditer(text or ""):
                _add_value(bucket, "地址", match.group(1))

    if "银行账号" in enabled_categories:
        for match in MASKED_BANK_LABEL_RE.finditer(text or ""):
            _add_value(bucket, "银行账号", match.group(1))

    return bucket


def _collect_pattern_values(text: str, enabled_categories: set[str]) -> dict[str, set[str]]:
    bucket: dict[str, set[str]] = {}
    pattern_map: list[tuple[str, re.Pattern[str]]] = [
        ("手机号", PHONE_RE),
        ("邮箱", EMAIL_RE),
        ("身份证号", ID_CARD_RE),
        ("统一社会信用代码", USCC_RE),
    ]
    for category, pattern in pattern_map:
        if category not in enabled_categories:
            continue
        for match in pattern.finditer(text or ""):
            _add_value(bucket, category, match.group(1))
    return bucket


def _merge_value_buckets(*buckets: dict[str, set[str]]) -> dict[str, set[str]]:
    merged: dict[str, set[str]] = {}
    for bucket in buckets:
        for category, values in bucket.items():
            merged.setdefault(category, set()).update(values)
    return merged


def _first_appearance_order(raw_text: str, value: str) -> tuple[int, int, str]:
    idx = (raw_text or "").find(value)
    return (idx if idx >= 0 else 10**9, -len(value), value)


def _build_replacement_plan(raw_text: str, values_by_category: dict[str, set[str]]) -> tuple[dict[str, str], dict[str, int]]:
    raw_to_masked: dict[str, str] = {}
    stats: dict[str, int] = {}
    for category in sorted(values_by_category.keys()):
        ordered_values = sorted(
            values_by_category[category],
            key=lambda value: _first_appearance_order(raw_text, value),
        )
        if not ordered_values:
            continue
        stats[category] = len(ordered_values)
        for index, raw_value in enumerate(ordered_values, start=1):
            raw_to_masked[raw_value] = f"[{category}_{index}]"
    return raw_to_masked, stats


def _replace_exact_values(text: str, raw_to_masked: dict[str, str]) -> str:
    if not raw_to_masked:
        return text or ""
    ordered_values = sorted(raw_to_masked.keys(), key=lambda value: (-len(value), value))
    pattern = re.compile("|".join(re.escape(value) for value in ordered_values))
    return pattern.sub(lambda match: raw_to_masked[match.group(0)], text or "")


def _expand_matches(text: str, raw_to_masked: dict[str, str]) -> list[MaskMatch]:
    matches: list[MaskMatch] = []
    category_by_raw = {
        raw_value: masked_value[1:masked_value.rfind("_")]
        for raw_value, masked_value in raw_to_masked.items()
    }
    for raw_value in sorted(raw_to_masked.keys(), key=lambda value: (-len(value), value)):
        for match in re.finditer(re.escape(raw_value), text or ""):
            matches.append(
                MaskMatch(
                    category=category_by_raw[raw_value],
                    raw_value=raw_value,
                    masked_value=raw_to_masked[raw_value],
                    start=match.start(),
                    end=match.end(),
                )
            )
    matches.sort(key=lambda item: (item.start, item.end, item.raw_value))
    deduped: list[MaskMatch] = []
    occupied: list[tuple[int, int]] = []
    for match in matches:
        if any(not (match.end <= start or end <= match.start) for start, end in occupied):
            continue
        occupied.append((match.start, match.end))
        deduped.append(match)
    return deduped


def mask_contract_text(text: str, *, enabled_categories: Iterable[str] | None = None) -> MaskResult:
    raw_text = text or ""
    enabled = set(enabled_categories or DEFAULT_ENABLED_CATEGORIES)
    contextual = _collect_contextual_values(raw_text, enabled)
    patterned = _collect_pattern_values(raw_text, enabled)
    values_by_category = _merge_value_buckets(contextual, patterned)
    raw_to_masked, stats = _build_replacement_plan(raw_text, values_by_category)
    masked_text = _replace_exact_values(raw_text, raw_to_masked)
    matches = _expand_matches(raw_text, raw_to_masked)
    return MaskResult(
        raw_text=raw_text,
        masked_text=masked_text,
        matches=matches,
        stats=stats,
    )


def mask_fragment(text: str, matches: list[MaskMatch]) -> str:
    raw_to_masked: dict[str, str] = {}
    for match in matches or []:
        raw_to_masked[match.raw_value] = match.masked_value
    return _replace_exact_values(text or "", raw_to_masked)


def restore_masked_text(text: str, mask_result: MaskResult | None) -> str:
    if not text or not mask_result:
        return text or ""
    masked_to_raw = mask_result.masked_to_raw_map()
    if not masked_to_raw:
        return text
    ordered_values = sorted(masked_to_raw.keys(), key=lambda value: (-len(value), value))
    pattern = re.compile("|".join(re.escape(value) for value in ordered_values))
    return pattern.sub(lambda match: masked_to_raw[match.group(0)], text)
