# 使用说明（USAGE）

## 1. 依赖

| 项 | 要求 |
|-|-|
| Python | 3.8+，**仅标准库**（无第三方依赖，无需联网安装） |
| 可选 | `pdftotext`（PDF 取文本）或 `PyMuPDF`；都没有时 PDF 案例降级为 `partial` |
| 网络 | 可选。仅用于 DOI 解析；不可用自动降级，不影响产物 |

## 2. 命令

```bash
python scripts/run.py --input <case.json 或目录> --out <产物目录> --work <工作目录> [选项]
```

| 选项 | 说明 |
|-|-|
| `--input` | `case.json` 路径，或包含它的目录（目录内也可只有稿件+文献库） |
| `--out` | 产物目录，默认 `/app/output` |
| `--work` | 工作目录，默认 `/app/work`（记录中间态，可留痕） |
| `--no-network` | 完全离线；`verify.network.mode = offline_degraded` |
| `--online` | 强制尝试 DOI 解析（默认 `auto`） |
| `--style gb7714\|apa` | 强制引用样式；缺省按 `task.md` 关键词 + 稿件中英文比例判定 |
| `--case-id` | 覆盖 `case_id` |
| `--strict` | 非 `success` 时返回退出码 1 |

退出码：`0` 成功 / `1` partial（仅 `--strict`）/ `2` unable。

## 3. 输入的最小形态

`case.json` 最少只需指向稿件与文献库；键名不敏感：

```json
{
  "case_id": "demo",
  "manuscript": "manuscript.md",
  "library": "library.json",
  "style": "GB/T 7714-2015",
  "output_contract": {"case_id": "string", "status": "string", "manuscript": "markdown"}
}
```

也可把内容内联（`"manuscript": "……全文……"`），或干脆只放 `manuscript.md` + `library.json` 两个文件让脚本自动发现。

文献库支持：`.json`（列表 / `{"records":[...]}` / 键值映射）、`.bib`（BibTeX）、`.ris`、`.csv`/`.tsv`、以及一行一条的 `.txt`。中英文字段名均可（`题名`/`刊名`/`作者`/`年份` 等）。

## 4. 产物怎么读

| 想确认的事 | 看哪里 |
|-|-|
| 交付是否合格 | `result.json` → `status` / `status_reason` |
| 编号是否真的同步且稳定 | `envelope.metrics.numbering_idempotent` / `numbering_continuous` |
| 合并了哪些、凭什么 | `merge_decisions.json` → 每对的 `decision` + `basis` + `veto` |
| 有没有被误合并 | `merge_decisions.json` 里 `decision: distinct` 的条目（含否决理由） |
| 哪条引用有问题 | `citation_ledger.csv` → `verdict` / `doi_network` / 问题码 |
| 哪些要人工确认 | `result.json` → `envelope.uncertainty` |
| 改了什么 | `changes.json` |
| 契约字段解析情况 | `result.json` → `envelope.contract`（`resolved` / `unresolved`） |

## 5. 常见问题

**Q：`status=partial`，但稿子看着没问题？**
看 `status_reason`。常见原因：文献库外条目（`REF_NOT_IN_LIBRARY`）、DOI 未注册、`needs_review` 合并、无正文标记。这些是**如实标注**，不是脚本故障。

**Q：`envelope.contract.unresolved` 非空？**
说明案例声明的字段名无法映射到内部值。`result.json` 里该字段为 `null` 且被记录——这是预期行为（不臆造）。扩别名请改 `scripts/pk/contract.py` 的 `SYNONYMS`。

**Q：为什么有条目没被合并？**
`merge_decisions.json` 找到该对：若 `decision=distinct` 看 `veto`；若没出现在决策里，说明连比较门槛都没到（题名相似 <0.30、作者无重合、年+卷不同）。

**Q：能处理 Word 域代码（`{ REF }`）吗？**
不能。先把域转为纯文本标记再运行。

**Q：会改我的原稿吗？**
不会。原文只读；成品写到 `manuscript_fixed.md`。

## 6. 自测

```bash
python tests/run_tests.py          # 20 项：幂等、连续性、去重精度、离线降级、契约、合规
```

`tests/fixtures/` 下两个合成案例：`clean/`（应 `success`）、`broken/`（含 3 组重复 + 1 条编造 + 1 组同作者陷阱，应 `partial` 并精确识别）。**DOI 为合成值**，联网运行时会如实报 `DOI_NOT_REGISTERED`，这正是核验功能在工作。
