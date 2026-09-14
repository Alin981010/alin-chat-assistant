---
name: simple-pptx-generator
description: 生成「轻量化汇报」风格的极简 PPTX（16:9、纯白底、单一强调色、大标题大留白、每页最多 5 条）。当用户明确要求做 PPT / 演示文稿 / 幻灯片 / 汇报材料 / 导出 pptx 时使用。支持命令行一键生成，也提供 generate_light_pptx() 函数供自定义版式，可选带一页原生柱状图。
license: MIT
metadata:
  version: "1.0.0"
  entrypoint: /skills/simple-pptx-generator/generate_pptx.py
  dependency: python-pptx
---

# 极简 PPTX 生成器

面向**轻量化汇报**：一页一个论点，30 秒能读完。不做复杂版式、不堆装饰。

## 何时使用

- 用户明确说「做 PPT / 生成 PPT / 做个演示文稿 / 做幻灯片 / 极简 PPT / 轻量化汇报 PPT / 导出 pptx 简报」
- 用户给了一段材料，要求整理成可以讲的页面

**绝不主动提起。** 用户只是在聊天、提问、分析文件时，不要提 PPT，也不要给 PPT 建议。

## 硬性约束

| 项 | 约束 |
|---|---|
| 尺寸 | 16:9（13.333 × 7.5 英寸），已内置 |
| 底色 | 纯白，不加背景图、渐变、纹理 |
| 颜色 | 只有 4 个：正文 `#1F2933`、次级 `#7B8794`、强调 `#C63A2B`、分隔线 `#E4E7EB` |
| 每页条目 | ≤ 5 条，超出自动截断 |
| 字号 | 封面 40pt / 页标题 30pt / 正文 18pt / 脚注 12pt |
| 字体 | 中文微软雅黑、西文 Segoe UI（已写入 `<a:ea>`，中文不会走主题默认字体） |
| 页数 | `--page-num` 是**目标上限**，实际页数由内容决定，**不空凑页数** |

## 页面结构

固定骨架，按顺序生成：

1. **封面** — 强调色短横线 + 主题 + 副标题（默认当天日期）
2. **目录** — 仅当论点 ≥ 4 且 `--page-num ≥ 5`
3. **内容页 ×N** — 小标签「要点 01」+ 页标题 + 分隔线 + 条目
4. **数据页** — 仅在传了 `--data` 时出现，原生柱状图（带数据标签、无图例）
5. **收尾页** — 大字「谢谢」+ 主题

要点比页码配额多时，会把多条合并到同一页；反之不补页。

## 用法一：命令行（推荐）

```bash
python3 /skills/simple-pptx-generator/generate_pptx.py \
  --topic "2026 Q1 业务复盘" \
  --points "市场概况,增长驱动：用户增长；渠道扩张,风险：供应链；合规" \
  --page-num 8 \
  --data "12,25,31,44" \
  --output /workspace/q1_review.pptx \
  --json
```

参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `--topic` | 是 | 封面主题 |
| `--points` | 建议 | 论点，**逗号/换行**分隔；**分号只用于页内条目**，见下方写法 |
| `--page-num` | 否 | 目标总页数，默认 8 |
| `--output` | 否 | 输出路径，默认 `/workspace/light_presentation.pptx` |
| `--data` | 否 | 柱状图数值，如 `"12,25,31,44"`；传了就多一页数据页 |
| `--subtitle` | 否 | 封面副标题，默认当天日期 |
| `--footer` | 否 | 封面页脚（如汇报人） |
| `--closing` | 否 | 收尾页大字，默认「谢谢」 |
| `--json` | 否 | 以 JSON 输出结果，**stdout 只有 JSON**，日志走 stderr |

### `--points` 的写法

**两级分隔符，各司其职**：

- **论点之间**：中英文逗号 `,` `，` 或换行
- **页内条目之间**：中英文分号 `;` `；`

每条论点有两种形态：

- `标题` —— 一页一个论点，无子条目
- `标题：要点1；要点2` —— 冒号前做页标题，分号拆成条目

```bash
# 4 个论点，其中 2 个带子条目
--points "市场概况,增长驱动：用户增长；渠道扩张,风险：供应链；合规"
```

> 分号不会拆论点。上例是 **4 页**，不是 6 页——把分号也当论点分隔符会让
> `增长驱动：用户增长；渠道扩张` 被误拆成两页。

### 输出（`--json`）

```json
{"ok": true, "output": "/workspace/q1_review.pptx", "pages": 6, "bytes": 38912,
 "slides": [{"index": 1, "title": "2026 Q1 业务复盘"}, {"index": 2, "title": "目录"}]}
```

失败时：`{"ok": false, "error": "..."}`，退出码非 0。

## 用法二：当函数调用（要改版式时）

文件可以直接 import，或在沙箱里复制后改：

```python
import sys
sys.path.insert(0, "/skills/simple-pptx-generator")
from generate_pptx import generate_light_pptx

path = generate_light_pptx(
    topic="2026 Q1 业务复盘",
    points=["市场概况", "增长驱动：用户增长；渠道扩张", "风险：供应链；合规"],
    page_num=8,
    data=[12, 25, 31, 44],
    output="/workspace/q1_review.pptx",
    subtitle="汇报人：阿林",
    closing="谢谢",
)
print(path)
```

想改配色/字号就先 `read_file` 本文件确认约束，再用 `write_file` 写一份改过的脚本到 `/workspace/`，
不要直接改 `/skills/` 下的原件（那是只读的技能目录，改了下个沙箱就没了）。

## 交付给用户

生成后，把沙箱里的绝对路径拼成可点击的下载链接返回：

```
/api/sandbox/download?org_id=<消息末尾【运行环境】里的 org_id>&path=/workspace/q1_review.pptx
```

## 故障排查

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: No module named 'pptx'` | 脚本会**自己装**（沙箱无 pip 时会先拉 get-pip.py，并用 `--break-system-packages` 绕过 PEP 668）。仍失败就手动：`curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && python3 /tmp/get-pip.py --quiet --break-system-packages && python3 -m pip install --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple python-pptx` |
| 提示安装中很慢 | 首次装依赖 20–60 秒，属正常，别重试太频繁；`execute` 的 timeout 给到 300 秒以上 |
| 刚创建沙箱就执行失败 | 沙箱的 provisioning 还在后台跑，等几秒再试一次即可 |
| 中文显示成方块 | 目标机器缺「微软雅黑」，换成本机有的字体（改脚本里的 `FONT_CN`） |
| 页数比 `--page-num` 少 | 预期行为：页数由论点数量决定，不空凑 |

## 不要做的事

- 不要把整段原文直接塞进页面（一页超过 5 条会被截断，等于白写）
- 不要为了凑数把一句话拆成三页
- 不要在页面里放长段落、代码块、表格截图；要点化
- 不要动 `/skills/` 下的文件，要改版式请在 `/workspace/` 里另存一份
