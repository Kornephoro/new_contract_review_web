# 测试合同说明

本目录中的合同文件均为测试样本，所有公司名称、地址、账号、联系人、金额、日期等信息均为虚构，仅用于功能测试与界面演示。

已生成文件：

- `01_construction_contract_test.docx`
  场景：建设工程施工合同
  特点：工期、进度款、质保金、验收备案、安全责任较完整

- `02_software_service_contract_test.docx`
  场景：软件开发与运维服务合同
  特点：知识产权、数据安全、验收标准、运维期较完整

- `03_equipment_purchase_contract_test.docx`
  场景：设备采购合同
  特点：采购标的、交货、安装调试、质保、售后条款较完整

- `04_office_lease_contract_test.docx`
  场景：办公场地租赁合同
  特点：租期、租金递增、保证金、装修管理、违约解除较完整

- `05_labor_contract_ocr_scan_test.pdf`
  场景：劳动合同
  特点：固定期限、试用期、工资、加班、保密义务
  说明：扫描件风格 PDF，适合测试 OCR

- `06_loan_contract_ocr_scan_test.pdf`
  场景：借款合同
  特点：借款金额、利率、担保、提前到期、违约责任
  说明：扫描件风格 PDF，适合测试 OCR

- `07_construction_subcontract_ocr_scan_test.pdf`
  场景：建设工程专业分包合同
  特点：分包范围、工期、产值支付、安全管理、节点违约
  说明：扫描件风格 PDF，适合测试 OCR

- `08_confidentiality_agreement_ocr_scan_test.pdf`
  场景：保密协议
  特点：保密范围、返还义务、违约金、诉讼管辖
  说明：扫描件风格 PDF，适合测试 OCR

- `09_goods_purchase_contract_ocr_scan_test.pdf`
  场景：货物采购合同
  特点：采购标的、交货、质量标准、质保金、违约责任
  说明：扫描件风格 PDF，适合测试 OCR

若你看到旧版 PDF 仍有乱码，请优先使用以下修复版：

- `05_labor_contract_ocr_scan_test_fixed.pdf`
- `06_loan_contract_ocr_scan_test_fixed.pdf`
- `07_construction_subcontract_ocr_scan_test_fixed.pdf`
- `08_confidentiality_agreement_ocr_scan_test_fixed.pdf`
- `09_goods_purchase_contract_ocr_scan_test_fixed.pdf`

这些 `*_fixed.pdf` 由本地 UTF-8 脚本重新生成，适合作为 OCR 测试输入。

建议测试方式：

- 用不同合同类型分别测试合同识别、风险分类和摘要抽取
- 用上传方式测试 `.docx` 解析流程
- 用这些扫描风格 `.pdf` 测试 OCR 识别、字段抽取和版面稳定性
- 用这些完整合同验证金额、日期、主体、争议解决条款等字段抽取是否正常
