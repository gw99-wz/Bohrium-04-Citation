# Bohrium-04-Citation

玻尔分站赛「写作04｜参考文献与引用管理」参赛仓库。

| 项 | 值 |
|-|-|
| 赛题 | 写作04｜参考文献与引用管理 |
| Challenge ID | `product-arena-03-429cc14d` |
| 赛题页 | https://play.bohrium.com/challenge/product-arena-03-429cc14d |
| 提交形态 | Skill 本体 `SKILL.md` + 可复用资源；平台在 3 个固定案例上运行 |
| 状态 | **准备阶段**（环境、赛题、仓库已就绪；方案尚未落地为代码） |

## 本题的执行契约（来自 task.md）

- 评测时安装到 `/skills/weekly-skill`，输入为 `/input/case.json` + `task.md` + 随题材料。
- 运行在 `/app/work`，最终产物与 `result.json` 落在 `/app/output`；`result.json` 须包含
  **案例原始 `output_contract` 的全部字段 + `case_id`**。
- `output_contract` 的字段名**运行时才下发**，不得硬编码。
- 预算：64 次模型请求 / 900 秒 / 单次回答 16384 tokens（含推理）。
- Skill 资源上限：≤ 50 个 UTF-8 文本文件，单文件 ≤ 500KB，合计 ≤ 2MB，无符号链接。

## 目录约定

```
Bohrium-04-Citation/
├─ README.md            # 本文件
├─ .gitignore           # 仅 git 用，不进入提交树
├─ submission/          # ← 提交根：packaging 时作为 --outputs 传入
│  └─ SKILL.md          #    （待建）Skill 入口，YAML frontmatter: name/description
├─ docs/                # （待建）方法、用法、限制说明
└─ evidence/            # （待建）自测运行记录，标明为示例而非官方案例答案
```

设计约束：**`submission/` 内不出现无扩展名文件**，避免打包合规问题。

## 四个考察重点

| 题面 | 落点 |
|-|-|
| 挂得对 | 引用落在真正需要来源支撑的句子/位置 |
| 编得顺 | 增删改序后正文编号与文末列表同步、连续、样式统一 |
| 收得净 | 中英文题名 / 预印本-正式版 / 多库导出去重合并，合并依据可解释 |
| 对得上 | 条目真实存在、元数据准确、正文论断与所引文献主题匹配 |

## 说明

本仓库不含预制答案。正式案例、白板与测评由平台提供。
