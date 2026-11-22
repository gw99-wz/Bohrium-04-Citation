# Bohrium-04-Citation

玻尔分站赛「写作04｜参考文献与引用管理」参赛作品。

| 项 | 值 |
|-|-|
| 赛题 | 写作04｜参考文献与引用管理 |
| Challenge ID | `product-arena-03-429cc14d` |
| 赛题页 | https://play.bohrium.com/challenge/product-arena-03-429cc14d |
| 提交物 | Skill 本体 `submission/SKILL.md` + 脚本 + 文档 + 自测题集 |
| 聚焦 | **编得顺**（编号幂等连锁更新）+ **收得净**（去重可解释）+ **对得上**（逐条核验，可离线降级） |
| 状态 | 已自测 20/20，已提交 |

## 一句话

把一份稿件和一份文献库变成**编号连续、去重可解释、逐条可核验**的成品；机械动作全部由确定性脚本完成，模型只读报告并处理「需人工确认」项。

## 目录

```
Bohrium-04-Citation/
├─ README.md
├─ .gitignore                 # 仅 git 用，不进入提交树
└─ submission/                # ← 提交根（打包时作为 --outputs）
   ├─ SKILL.md                # 入口：用途/调用/契约/异常/边界
   ├─ GITHUB_URL.txt          # 平台要求的作品链接
   ├─ scripts/
   │  ├─ run.py               # CLI 入口
   │  └─ pk/                  # common / contract / ingest / identity / numbering / styles / verify / render
   ├─ docs/                   # METHOD（算法与阈值）/ USAGE / LIMITS
   ├─ examples/mini/          # 最小调用示例（真实可跑）
   └─ tests/                  # 20 项自测 + 两个合成案例（clean / broken）
```

## 怎么跑

```bash
python submission/scripts/run.py \
  --input submission/examples/mini/case.json \
  --out .build/mini --work .build/mini-work --no-network
```

## 自测

```bash
python submission/tests/run_tests.py
```

覆盖：编号连续性与**逐字节幂等**、去重精度（3 组合并 / **0 误合并**）、同作者同年的防误合并负例、编造条目检出、**离线降级**（断网不崩且不改判）、契约字段解析与未知字段回退、缺库/缺稿/坏 JSON 的降级、资源合规（≤50 文件 / ≤500KB / ≤2MB / 无符号链接 / 无无后缀名文件）。

## 关键设计

1. **编号是派生量**，不是存储状态：以实体为单位按首现顺序重算，一遍改写正文、文末列表、图表注、附录四个面。
2. **去重先否决后合并**：卷不同、年差 ≥2、题名低于下限等直接判不同；跨语言无桥梁时只给 `needs_review`，绝不误并。
3. **闭合世界核验**：引用必须落到提供的文献库记录；DOI 解析仅作增强，任何网络异常整轮降级为 `offline_degraded` 并标注 `network_unavailable`。
4. **契约运行时解析**：`output_contract` 字段名不公开，按 精确→别名→深搜 解析，缺失写 `null` 并如实记录，绝不臆造。

详见 `submission/SKILL.md` 与 `submission/docs/METHOD.md`。
