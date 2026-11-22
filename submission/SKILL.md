---
name: citation-toolkit
description: 参考文献与引用管理：把一份稿件和一份文献库变成编号连续、去重可解释、逐条可核验的成品（重排编号、合并重复条目、核验引用真伪、输出 result.json 契约字段）。适用于需要交付引用整理结果、或需要核对引用是否真实存在的学术写作任务。
---

# citation-toolkit

**一句话**：引用插入/删除/调序之后，让正文编号、文末列表、图表注与附录**自动同步**；把同一篇文献的中英文条目、预印本与正式版、不同数据库导出**合并成一条并给出依据**；对每条引用给出**可回溯的真伪与匹配判定**（含离线降级）。

本 Skill 是**确定性代码 + 契约适配**，不依赖模型逐条改写稿件：模型只需调用脚本、读懂报告、处理「需人工确认」项。

---

## 1. 什么时候用

**用**：
- 任务要求整理稿件引用、重排参考文献编号、合并重复文献、核验引用真实性；
- 输入形态是「一份稿件 + 一份文献库（BibTeX/RIS/JSON/CSV/EndNote/CNKI 导出）」；
- 交付物要求「可继续编辑的稿件 + 同步的文献表 + 可核验记录」。

**不用**：
- 只需要润色语言、不需要动引用；
- 只要求生成一段文字，没有稿件与文献库；
- 纯问答、不需要产物文件。

## 2. 输入

| 来源 | 说明 |
|-|-|
| `/input/case.json` | 平台案例。本 Skill **不假设字段名**：稿件与文献库的指针可能是 `manuscript` / `draft` / `text` / `materials.manuscript` 等，也可能是同目录下的独立文件；`output_contract` 字段名运行时解析。 |
| 随题材料 | 稿件（`.md` `.txt` `.docx` `.pdf` `.json`）与文献库（`.json` `.bib` `.ris` `.csv` `.tsv`）；同目录文件会被自动发现。 |
| 任务说明 | `task.md`。用于判定目标引用样式（GB/T 7714 顺序编码制 / APA 著者—出版年制）。 |

## 3. 输出（写入 `/app/output`）

| 文件 | 内容 |
|-|-|
| `result.json` | **契约字段全部平铺在根**（`output_contract` 声明的每个名字都在），另有 `case_id`、`status`、`status_reason`，全部细节在 `envelope` |
| `manuscript_fixed.md` | 可继续编辑的成品稿：正文标记已同步、文末列表已按新编号重排 |
| `references.md` / `references.json` | 文献表（渲染文本 + 结构化字段、来源记录、去重成员） |
| `merge_decisions.json` | 去重账本：每对比较的决策（merged / distinct / needs_review）+ **判据** + 置信度 + 否决理由 |
| `citation_ledger.jsonl` / `.csv` | 逐条引用核验账本：所在句、区域、文献号、DOI 网络状态、论断—文献匹配、问题码、判定 |
| `changes.json` | 每处编号变化与每条合并/存疑的变更记录 |
| `verify_report.md` | 人读报告：编号一致性、去重明细、真实性核验、建议 |
| `run_manifest.json` | 运行信息：样式及来源、输入指纹、产物清单、稿件 sha256、是否幂等 |

## 4. 调用（三步）

```bash
# 1) 主流程：整理引用并产出全部文件
python /skills/weekly-skill/scripts/run.py \
  --input /input/case.json --out /app/output --work /app/work

# 2) 需要强制样式时（默认按 task.md 与稿件语言自动判定）
python /skills/weekly-skill/scripts/run.py \
  --input /input/case.json --out /app/output --work /app/work --style gb7714

# 3) 完全离线（禁止一切网络请求）
python /skills/weekly-skill/scripts/run.py \
  --input /input/case.json --out /app/output --work /app/work --no-network
```

脚本打印一段 JSON 摘要（`status` / `metrics` / `errors`），退出码：`0` 成功、`1` 部分成功（带 `--strict`）、`2` 无法处理。

**自测**（不读平台案例，验证 Skill 本身可用）：

```bash
python /skills/weekly-skill/scripts/run.py \
  --input /skills/weekly-skill/examples/mini/case.json \
  --out /app/work/example_out --work /app/work/example_work --no-network
```

## 5. 契约如何处理（关键）

平台**不公布** `output_contract` 的字段名，它随案例在运行时下发。本 Skill 的处理：

1. 从 `case.json` 的 `output_contract` / `required_outputs` / `expected_outputs` / `required_fields` / `outputs` 中取契约（dict / list / 字符串三种形态都支持）；
2. 每个声明字段按 **精确名 → 中英文别名表 → 限量深搜** 的顺序解析到内部值；
3. 解析不到就写 `null`，并在 `envelope.contract.unresolved` 里**如实记录**，绝不臆造值；
4. `result.json` 根对象始终包含全部声明字段 + `case_id` + `status`。

自查：

```bash
python -c "import json;d=json.load(open('/app/output/result.json'));print(d['status']);print(d['envelope']['contract'])"
```

## 6. 失败与异常处置

| 情况 | 行为 |
|-|-|
| 读不到稿件 | `status: unable`，`result.json` 仍写出并说明原因；退出码 2 |
| 无文末列表 / 无正文标记 | `status: partial`，照常产出报告并列出缺口 |
| 条目在文献库中找不到 | 标 `REF_NOT_IN_LIBRARY`（高强度），进入「疑似不可核验」，**原条目按原文保留**，不删不改 |
| DOI 解析超时 / 断网 | 整轮降级为 `offline_degraded`，记 `network_unavailable: true`，改用本地正则 + 元数据交叉比对；**不崩、不改判为编造** |
| DOI 未注册（`not_found`） | 记 `DOI_NOT_REGISTERED`（中强度）→ `needs_review`；**不等于编造** |
| 正文字号标记找不到对应条目 | 原样保留，记 `dangling_marker`，不静默丢弃 |
| 中英题名 / 预印本合并证据不足 | 不合并，标 `needs_review` 并请求人工确认（宁可标疑，不可误并） |
| 文献库字段缺失 | 从条目原文补齐并记 `field_provenance`；补不上则列出缺失字段 |

## 7. 预算与省时策略（64 请求 / 900 秒）

本 Skill 的机械动作全部由脚本完成，**正常一次案例只需 1–3 次工具调用**：
1. 直接跑主流程（通常 1–5 秒，无模型调用）；
2. 读 `verify_report.md` 或 JSON 摘要；
3. 只对 `needs_review` 项做人工判断并写进说明。

**不要**让模型逐条重排编号或逐条抄写文献表——那是本 Skill 要消除的失败模式。

## 8. 自检（交付前）

```bash
python /skills/weekly-skill/tests/run_tests.py      # 若随 Skill 一起分发
```

或人工核对三件事：`result.json` 里 `envelope.metrics.numbering_idempotent == true`、`numbering_continuous == true`、`fabrication_suspected` 与 `needs_review_pairs` 的每一条都能在报告里找到理由。

## 9. 已知边界

- **不做**跨库联网检索补全文献（防止引入材料外事实）；只在材料内做核验。
- 论断—文献匹配依赖材料提供的摘要/全文；没有就如实标 `evidence_insufficient`，不给假结论。
- 顺序编码制与 APA 已内建；其他规范（Vancouver、Chicago）需扩展 `scripts/pk/styles.py`。
- 扫描版 PDF 需镜像内 `pdftotext`/OCR 可用；不可用时降级为 `partial` 并说明。
- 只处理文本层引用标记（`[1]`、`[1-3]`、上标数字、`(Author, Year)`）；域代码型（Word 域的 `REF`）需先转为文本。

详见 `docs/METHOD.md`（算法与阈值）、`docs/LIMITS.md`（边界与不支持清单）。
