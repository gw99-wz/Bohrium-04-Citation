# 最小调用示例（示例，不是官方案例结果）

一个 4 条文献、3 处引用的小案例，用来演示完整接口：**去重合并 → 编号重排 → 逐条核验 → 契约输出**。

## 输入

| 文件 | 作用 |
|-|-|
| `case.json` | 平台输入形态：`case_id` / `manuscript` / `library` / `style` / `output_contract` |
| `manuscript.md` | 待整理稿件：文末列表里 `[2]` 与 `[4]` 是同一篇文献的两种导出 |
| `library.json` | 文献库（3 条记录） |

## 调用

```bash
python scripts/run.py \
  --input examples/mini/case.json \
  --out /tmp/mini_out --work /tmp/mini_work --no-network
```

## 示意输出（示例）

```json
{
  "status": "success",
  "case_id": "example-mini",
  "metrics": {
    "citations": 4,
    "references": 3,
    "entries": 4,
    "duplicates_collapsed": 1,
    "idempotent": true,
    "continuous": true,
    "network_mode": "offline_degraded"
  }
}
```

`manuscript_fixed.md` 里：

- 正文 `[1] [2] [3] [4]` → `[1] [2] [3] [2]`（`[4]` 与 `[2]` 合并后指向同一编号）
- 文末列表由 4 条变 3 条，编号连续
- `merge_decisions.json` 记录 `[2] ↔ [4]` 的判据（题名/作者/年/卷/页一致）
- `citation_ledger.csv` 每个引用点一行，含 `verdict`

> 本示例的文献与 DOI 均为演示用合成数据；联网运行会如实报 `DOI_NOT_REGISTERED`，属预期行为。
