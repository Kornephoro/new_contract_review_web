# 合同敏感信息脱敏功能设计

## 1. 背景

当前系统的合同审查链路如下：

1. 用户上传 `.docx/.pdf/.png/.jpg` 或直接粘贴合同文本。
2. `app.py` 中的 `extract_text()` 负责解析文本，扫描件会先走 OCR。
3. 解析后的 `final_text` 会被直接写入：
   - `st.session_state.contract_text_for_chat`
   - `st.session_state.modified_contract_text`
   - `st.session_state.review_snapshot["text"]`
4. 审查提示词、风险追问、导出预览等流程都默认使用原始合同文本。

这意味着当前版本对以下敏感信息没有做保护：

- 自然人姓名
- 手机号、邮箱
- 身份证号、统一社会信用代码
- 银行账号、开户地址
- 公司名称、地址
- 印章信息、签署日期中的个人身份信息
- 其他业务编号、项目编号、设备编号等可识别字段

如果后续需要把合同内容发送给大模型、展示给非原始起草人、导出内部评审报告，就需要先补齐一层“可控脱敏”能力。

## 2. 目标

本次脱敏功能设计目标：

- 支持在“发送给模型前”对合同文本做脱敏，减少真实身份信息外发。
- 支持在“界面展示时”选择展示原文或脱敏文。
- 支持在“导出报告时”生成脱敏版报告，避免内部流转泄露。
- 保留原文与脱敏文的映射关系，便于后续定位风险条款和导出修订结果。
- 保证功能默认安全，但不能破坏当前审查、风险定位、条款替换、DOCX 导出主流程。

非目标：

- 本阶段不追求覆盖所有行业专有字段。
- 本阶段不直接改写原始上传文件内容。
- 本阶段不做基于权限系统的多人隔离访问控制。

## 3. 核心原则

### 3.1 原文永远保留，脱敏结果单独存储

不要用脱敏文本覆盖原始文本。系统至少保留三份内容：

- `raw_text`：提取出的原始合同全文
- `masked_text`：规则替换后的脱敏文本
- `display_text`：当前界面实际展示使用的文本，可指向 `raw_text` 或 `masked_text`

这样可以避免后续 DOCX 导出、原文替换、风险条款应用时丢失真实文本。

### 3.2 脱敏应早于 LLM 调用，但晚于 OCR/文本提取

推荐接入顺序：

1. 文件解析/OCR
2. 得到 `raw_text`
3. 执行脱敏，生成 `masked_text`
4. 根据用户配置决定：
   - 审查走 `masked_text`
   - 对话走 `masked_text`
   - 页面展示走 `raw_text` 或 `masked_text`

这样可以最大化降低外发风险，同时不影响 OCR 识别质量。

### 3.3 风险定位必须显式区分“审查文本”和“原始文本”

如果模型看到的是脱敏文本，那么返回的：

- `original`
- `suggestion`
- 风险高亮定位

都将针对脱敏后的文本，而不是原始文本。

因此系统必须明确区分：

- `review_text`：送给模型的文本
- `source_text`：系统真实保存的原始文本

否则后续高亮定位和原文替换会错位。

### 3.4 审查结果与展示结果要分通道

即使 AI 审查使用的是脱敏文本，系统右侧概览面板仍然可能需要展示真实信息，例如：

- 甲方/乙方真实名称
- 真实签署主体
- 真实联系人
- 未脱敏的内部展示字段

因此不能把“AI 返回的 overview”直接作为页面唯一数据源。系统应至少区分：

- `review_overview`：AI 基于脱敏文本返回的概览，可能包含 `[公司名称_1]`
- `display_overview_raw`：供系统右侧面板展示的真实概览
- `display_overview_masked`：供共享屏幕或导出脱敏报告使用的脱敏概览

这意味着脱敏系统不仅要有 `raw_text <-> masked_text` 的映射，还要能把 overview 里的占位符反向还原为真实值，或者直接从原文中补提取真实主体信息。

## 4. 用户场景

### 场景 A：对外部模型审查前先脱敏

用户上传合同后，系统先识别敏感字段并替换为占位符，例如：

- `张三` -> `[姓名_1]`
- `13800138000` -> `[手机号_1]`
- `9144xxxxxxxxxxxxxx` -> `[统一社会信用代码_1]`

然后把脱敏后的合同发送给模型审查。

### 场景 B：内部讨论使用脱敏版界面

法务同事在共享屏幕或演示环境下，希望页面上展示脱敏文本和脱敏风险片段，而不是客户真实名称与账号。

### 场景 C：导出脱敏版审查报告

管理层只需要看问题摘要，不需要看到真实主体信息，导出时可以选择脱敏版 HTML/TXT/DOCX 报告。

### 场景 D：最终修订导出仍基于原文

即使审查环节用了脱敏文本，最终一键应用修改和 DOCX 导出仍要回到原始合同文本，保证对外交付文件可直接使用。

### 场景 E：AI 脱敏审查，但右侧概览显示真实甲乙方

例如：

- 送给 AI 的文本：`甲方：[公司名称_1]；乙方：[公司名称_2]`
- 右侧面板显示：`甲方：北京某某科技有限公司；乙方：上海某某贸易有限公司`

这是可实现的，但前提是概览信息不能只依赖 AI 的原始返回值，而要经过一层“展示侧回填/还原”。

## 5. 总体方案

推荐将脱敏能力拆成两层：

### 5.1 第一层：规则脱敏引擎

新增模块建议：

- `legal_review/masking.py`

提供以下职责：

- 定义敏感信息类别
- 维护正则规则和替换器
- 执行脱敏并返回映射结果
- 支持对文本片段做二次脱敏

核心接口建议：

```python
from dataclasses import dataclass

@dataclass
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

def mask_contract_text(text: str, *, enabled_categories: set[str] | None = None) -> MaskResult:
    ...

def mask_fragment(text: str, matches: list[MaskMatch]) -> str:
    ...
```

### 5.2 第二层：链路接入与展示策略

在 `app.py` 中新增脱敏配置与状态：

- 是否启用发送前脱敏
- 页面默认展示原文还是脱敏文
- 导出时是否生成脱敏版
- 当前脱敏统计信息

同时在 `review_snapshot` 中保存：

- `raw_text`
- `review_text`
- `display_mode`
- `masking_result`

### 5.3 第三层：概览信息双通道组装

为了满足“AI 看脱敏文，但右侧显示真实甲乙方”的需求，建议再补一层 overview 组装器：

- `legal_review/overview_resolver.py`

职责：

- 接收 AI 基于脱敏文本返回的 `review_overview`
- 根据脱敏映射把占位符还原成真实值
- 必要时从原文中直接提取甲方/乙方等主体信息
- 生成供 UI 使用的 `display_overview_raw`

推荐策略：

1. AI 审查仍然只看 `review_text`
2. AI 返回 `review_overview`
3. 系统执行 `resolve_display_overview(review_overview, raw_text, mask_result)`
4. 右侧面板默认展示 `display_overview_raw`

这样既能避免敏感信息发送给模型，也不影响系统自己展示真实主体名称。

## 6. 脱敏规则设计

### 6.1 第一阶段建议支持的类别

优先做高收益、低歧义类别：

- 手机号
- 邮箱
- 身份证号
- 银行卡号/账号
- 统一社会信用代码
- 纳税人识别号
- 公司名称后缀明显的主体名称
- 个人姓名（有限规则）
- 详细地址（有限规则）

### 6.2 占位符策略

建议采用“类别 + 序号”的稳定占位符：

- `[公司名称_1]`
- `[公司名称_2]`
- `[姓名_1]`
- `[手机号_1]`
- `[银行账号_1]`

优点：

- 模型仍能理解“这是不同主体/字段”
- 同一值在全文多次出现时可复用同一个占位符
- 便于后续审查结论还原或人工核对

### 6.3 替换规则要求

替换规则需要满足：

- 相同原文值始终替换为相同占位符
- 长文本优先，避免短文本截断长文本
- 先替换强规则字段，再替换弱规则字段
- 避免对法律条文编号、金额、比例、日期等审查关键字段误伤

建议优先级：

1. 身份证号/统一社会信用代码/邮箱/手机号/银行卡号
2. 公司全称
3. 地址
4. 姓名

## 7. 数据结构设计

### 7.1 Session State

建议新增以下状态：

```python
st.session_state.masking_enabled = True
st.session_state.masking_display_mode = "脱敏显示"
st.session_state.masking_export_enabled = True
st.session_state.masking_result = None
st.session_state.contract_raw_text = ""
st.session_state.contract_review_text = ""
```

说明：

- `contract_raw_text`：原始解析文本
- `contract_review_text`：实际送审文本，可能是脱敏后的
- `masking_result`：完整脱敏结果对象或序列化字典

### 7.2 Review Snapshot

当前 `_build_review_snapshot()` 只有一个 `text` 字段，不足以支持脱敏。建议扩展为：

```python
{
    "text": review_text,
    "raw_text": raw_text,
    "review_text": review_text,
    "display_text": display_text,
    "contract_type": contract_type,
    "overview": review_overview,
    "review_overview": review_overview,
    "display_overview_raw": display_overview_raw,
    "display_overview_masked": display_overview_masked,
    "risks": risks,
    "masking": {
        "enabled": True,
        "display_mode": "masked",
        "stats": {"手机号": 3, "姓名": 5},
        "matches": [...]
    },
}
```

兼容策略：

- 保留原有 `text` 字段，短期内让它等于 `review_text`
- 保留原有 `overview` 字段，短期内让它等于 `review_overview`
- 新逻辑逐步改成显式读取 `raw_text` / `review_text` / `display_overview_raw`

### 7.3 Overview 对象建议

建议 overview 统一保留同样的结构，避免 UI 多套分支：

```python
{
    "parties": ["甲方：北京某某科技有限公司", "乙方：上海某某贸易有限公司"],
    "amount": "100万元",
    "duration": "2026年1月1日至2026年12月31日",
    "sign_date": "2026年1月1日",
    "governing_law": "中华人民共和国法律，争议解决地为上海",
    "summary": "..."
}
```

区别只在于值来自哪条通道：

- `review_overview`：可能含占位符
- `display_overview_raw`：已还原真实值
- `display_overview_masked`：明确用于脱敏展示

## 8. 关键接入点设计

### 8.1 文件解析后立即脱敏

接入位置：

- `app.py` 中点击“开始审查”后的 `final_text` 生成位置

当前逻辑：

```python
final_text = extract_text(uploaded_file, file_bytes=file_bytes)
```

建议改为：

```python
raw_text = extract_text(uploaded_file, file_bytes=file_bytes)
mask_result = mask_contract_text(raw_text) if masking_enabled else None
review_text = mask_result.masked_text if mask_result else raw_text
```

### 8.2 审查提示词与对话统一使用 `review_text`

当前直接发送 `final_text[:4000]` 给模型。建议替换为：

- 审查：`review_text`
- 上下文问答：`contract_text_for_chat = review_text`

这样可保证二次追问不会意外泄露原文。

### 8.3 右侧概览面板使用 `display_overview_raw`

当前 `_render_overview_panel()` 和 `build_export_report_html()` 都直接读取 `snap["overview"]`。

为了满足真实甲乙方展示需求，建议改成：

- 右侧系统面板默认读取 `snap["display_overview_raw"]`
- 如果用户主动切换“脱敏展示”，则读取 `snap["display_overview_masked"]`
- `snap["overview"]` 保留给兼容逻辑，默认等于 `review_overview`

推荐优先级：

1. `display_overview_raw`
2. `display_overview_masked`
3. `review_overview`

这样即使 AI 返回的是 `[公司名称_1]`，页面依然可以显示真实甲乙方名称。

### 8.4 风险展示按显示模式渲染

如果模型审查基于脱敏文本，则：

- 风险 `original` 本身就是脱敏片段
- 高亮文本也应基于 `review_text`

因此工作区展示建议这样处理：

- “脱敏显示”模式：合同画布、高亮、风险卡片统一使用 `review_text`
- “原文显示”模式：第一期不自动把风险片段反查回原文，只展示原文全文 + 风险卡片中的脱敏片段

原因：

从脱敏片段精确反向映射回原文位置，复杂度高且容易错配，建议放到第二阶段。

### 8.5 导出分两条链路

#### 导出审查报告

HTML 报告可直接支持：

- 脱敏版报告
- 原文版报告

这里成本较低，因为本质是重新拼装页面。

补充建议：

- 原文版报告使用 `display_overview_raw`
- 脱敏版报告使用 `display_overview_masked`
- 不要直接用 `review_overview` 作为展示稿，因为其中可能残留占位符，不适合最终阅读

#### 导出修订后的合同

DOCX/TXT 修订导出必须基于原文，不能直接用脱敏文本替换。否则会出现：

- `[公司名称_1]` 被写回正式合同
- 风险修改无法匹配原始条款

因此第一阶段建议限制：

- 如果本次审查启用了脱敏，则“正式修订导出”先提示暂不支持自动回写原文
- 仅支持导出脱敏版审查报告

或者采用更稳妥的兼容策略：

- 审查脱敏仅用于“概览和问题识别”
- 自动替换导出功能在脱敏开启时默认禁用

这是一期最安全的做法。

## 9. 推荐的产品策略

### 9.1 一期目标

一期只做“安全可用”，不追求全链路无损回写：

- 支持上传后自动脱敏
- 支持脱敏文本送审
- 支持脱敏版风险展示
- 支持脱敏版 HTML 报告导出
- 明确禁用或弱化“基于脱敏审查结果的一键回写导出”

### 9.2 二期目标

二期再做“脱敏审查结果映射回原文”：

- 记录更稳定的 span 映射
- 风险片段可从脱敏文本回查原文
- 建立 `masked original -> raw original` 的定位表
- 支持在原始 DOCX 上自动回写修订

### 9.3 三期目标

三期可补：

- 手动勾选脱敏类别
- 白名单主体不过滤
- 支持自定义占位符格式
- 支持表格字段级脱敏统计

## 10. 方案取舍

### 方案 A：只在展示层脱敏

优点：

- 不影响现有审查和导出逻辑

缺点：

- 文本仍会原样发给模型，安全收益有限

结论：

不建议作为主方案。

### 方案 B：在送模前脱敏，但不做原文回写

优点：

- 安全收益最大
- 实现复杂度可控
- 适合快速上线验证

缺点：

- 脱敏审查结果暂时不能直接用于原文合同自动替换

结论：

建议作为一期主方案。

### 方案 C：送模前脱敏，同时建立精准原文映射并支持自动回写

优点：

- 体验最完整

缺点：

- 需要维护复杂的 span 映射、重复值区分、段落级还原逻辑
- 对当前 `original -> suggestion` 的替换机制侵入较大

结论：

适合作为二期升级目标，不建议一步到位。

## 11. 详细开发建议

### 11.1 新增模块

建议新增：

- `legal_review/masking.py`
- `legal_review/masking_types.py`，可选
- `legal_review/overview_resolver.py`

`masking.py` 职责：

- 定义规则
- 执行脱敏
- 生成统计
- 对零散片段再次脱敏

`overview_resolver.py` 职责：

- 把 AI 返回的 `review_overview` 还原成展示版 overview
- 对 `parties`、`summary`、`governing_law` 中的占位符做反查替换
- 必要时从 `raw_text` 中直接抽取“甲方/乙方/丙方”字段补全真实主体名

推荐接口：

```python
def resolve_display_overview(
    review_overview: dict,
    raw_text: str,
    mask_result: MaskResult | None,
) -> tuple[dict, dict]:
    """
    返回:
    - display_overview_raw
    - display_overview_masked
    """
```

### 11.2 修改 `app.py`

建议改动点：

1. 初始化新的 session state
2. 在 `extract_text()` 后接入 `mask_contract_text()`
3. 审查时使用 `review_text`
4. 对话时使用 `review_text`
5. `review_snapshot` 扩展脱敏字段
6. 组装 `review_overview -> display_overview_raw/display_overview_masked`
6. UI 增加：
   - `发送给模型前脱敏`
   - `结果区显示脱敏文本`
   - `导出脱敏版报告`
7. `_render_overview_panel()` 默认优先展示真实 overview
8. 如果当前为脱敏审查，则在导出面板提示：
   - 正式合同回写功能已受限

### 11.3 修改 `build_chat_system_prompt()`

当前参数名是 `contract`，建议调用端统一传 `review_text`，并在函数注释里明确：

- 这里传入的是“可发送给模型的安全文本”

### 11.4 修改导出逻辑

`legal_review/document_editor.py` 建议新增：

- `build_masked_export_payload(snapshot)`
- `render_masking_notice(snapshot)`

一期先支持：

- HTML 脱敏报告下载
- TXT 脱敏文本下载

对于 DOCX 原格式替换：

- 若 `snapshot["masking"]["enabled"] == True`，则提示暂不支持自动回写原文

## 12. 风险与难点

### 12.1 人名识别误伤

个人姓名最容易误判，尤其是：

- 合同项目名称中带人名
- 法律条文案例名
- “甲方代表：李某”与普通词语混淆

建议：

- 一期只做较保守的人名规则
- 宁可少脱，不要大面积误伤

### 12.2 公司名称切分不稳定

“北京某某科技有限公司”在全文中可能存在：

- 全称
- 简称
- 含括号版本

建议：

- 一期优先匹配全称
- 简称留给二期做别名归并

### 12.3 原文导出回写困难

当前导出逻辑依赖风险里的 `original` 直接在原文里查找替换。如果 `original` 来自脱敏文本，就无法直接回写原文。

这是本设计中最关键的约束，也是建议分阶段推进的原因。

### 12.4 真实概览回填不应再次调用外部 AI

为了避免“显示真实甲乙方”这个动作再次把原文发给模型，overview 回填应优先使用：

- 脱敏映射反查
- 本地规则抽取

不建议做法：

- 先把原文再发一次给 AI，只为了拿真实 `overview`

因为这会绕开送模前脱敏的安全目标。

## 13. 测试建议

至少准备以下样例：

1. 含公司全称、联系人、手机号、邮箱的服务合同
2. 含身份证号、开户地址、银行卡号的劳务/保密协议
3. 含 OCR 扫描文本的 PDF 合同
4. 含重复主体名称、多次出现同一手机号的合同
5. 含金额、日期、法条编号，验证不会误脱敏的合同

重点验证：

- 同一字段是否稳定映射到同一占位符
- 模型是否能在脱敏文本上仍输出可读风险
- `review_overview` 是否能正确还原成真实 `display_overview_raw`
- 右侧面板是否能显示真实甲方/乙方，而 AI 输入仍保持脱敏
- 页面高亮是否与脱敏文本一致
- 导出报告是否不会泄露原始信息
- 开启脱敏后是否正确限制正式回写导出

## 14. 实施顺序

建议按以下顺序推进：

1. 新增 `legal_review/masking.py`，完成规则引擎和测试样例
2. 新增 `legal_review/overview_resolver.py`，先打通真实主体名回填
3. 在 `app.py` 接入 `raw_text -> masked_text -> review_text`
4. 增加页面配置和脱敏统计提示
5. 调整 `review_snapshot` 结构
6. 右侧概览面板切到 `display_overview_raw`
7. 支持脱敏版报告导出
8. 在脱敏开启时限制 DOCX 自动回写
9. 二期再做原文映射和完整修订导出

## 15. 建议结论

建议采用“送模前脱敏 + 展示侧支持脱敏 + 导出报告支持脱敏 + 正式回写延后”的分阶段方案。

这个方案最适合当前仓库的原因有三点：

- 对现有 `app.py` 和 `document_editor.py` 的侵入可控
- 安全收益立即可见
- 避免为了兼容 DOCX 回写一步引入过高复杂度

如果后续确认要继续实现，下一步可以直接进入一期开发，优先落地：

- `legal_review/masking.py`
- `legal_review/overview_resolver.py`
- `app.py` 接入脱敏状态与送模文本切换
- 右侧真实概览回填
- 脱敏版 HTML 报告导出
