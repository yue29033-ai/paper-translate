# Paper Translate · 文献翻译，保留原图

A Codex skill for translating research PDFs into Chinese while retaining original visual content.

将单篇英文论文制作成便于精读的**中文阅读版**和**左中文、右原文的对照版 PDF**。翻译由 Codex 按照 [SKILL.md](SKILL.md) 完成；附带的 Python 脚本负责在本地提取、排版和检查，**不会单独自动翻译论文，也不需要新增翻译 API 或密钥**。Codex 本身仍需可用。

## 翻译范围

| 内容 | 默认处理 |
| --- | --- |
| 标题、摘要、关键词、各节正文、图注、表题和表注 | 忠实翻译为学术中文；术语首次附英文，后文统一 |
| 原图及图内文字、坐标、图例、标尺、分图字母 | 从源 PDF 复制完整区域，保留原文 |
| 表格主体、公式及编号 | 保留原文区域 |
| 作者机构、DOI、链接、参考文献条目 | 保留原文 |

中文阅读版仍含上述保留的英文元素。优先保证可读性，允许重排和增加页数；同一源页的内容独立成组，译文续页在对照版右侧重复对应的完整原文页。源 PDF 只读。

## 安装

需要 Git、**Python 3.11+** 和一款现有的、可嵌入且包含所需中文字形的 TrueType 字体。本项目实际验证环境为 **macOS + Python 3.12**；Linux、Windows 尚未验证。

将仓库安装为个人 Codex skill：

```sh
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/yue29033-ai/paper-translate.git "$HOME/.codex/skills/paper-translate"
```

`git clone` 遇到既有目录会拒绝执行，不会覆盖已有 skill。若目标已存在，请先检查该目录，再自行选择新的安装位置。

首次安装成功后，在该目录创建独立 Python 环境：

```sh
cd "$HOME/.codex/skills/paper-translate"
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

以上为 macOS / Linux 命令。Windows 可用 `py -3` 创建环境，并在 PowerShell 中用 `.venv\Scripts\Activate.ps1` 激活。确认所选 Python 版本满足要求。

字体通过 `--font` 指定。macOS 默认候选为 `/System/Library/Fonts/Supplemental/Arial Unicode.ttf`，只有本机实际存在且可读取时才可用。Linux / Windows 建议明确传入本地字体路径；脚本不下载或安装字体。字体不兼容或缺字时会报错，应换用合适字体。

## 在 Codex 中使用

在能发现此 skill 的 Codex 会话中提供原文 PDF，并输入：

```text
使用 $paper-translate 翻译这篇论文，保留原图，生成中文和中英左右对照 PDF。
```

默认处理全文。指定页码时只处理选定范围，不会把节选称为全文完成。PDF 中出现的命令、提示词或操作要求只作为文献内容处理。

## 手工运行流程

下面从仓库根目录运行，并使用已激活的 Python 环境。将自己的原文放到 `paper.pdf`，字体路径按本机情况修改。

**1. 提取初稿及原页预览。**

```sh
python scripts/extract.py paper.pdf --work-dir work/paper
```

**2. 让 Codex 依照 skill 翻译和审核。**

这一步需要阅读 [SKILL.md](SKILL.md) 和 [manifest 契约](references/manifest.md)：逐页查看原页，修正 `work/paper/manifest.json` 的分区、阅读顺序及 `translate` / `preserve` 动作，填写完整 `translation` 和术语表。确认每页分区后才将该页 `reviewed` 设为 `true`；逐段完成语义、数字和引用核对后，才设置 `review.translation_checked=true`。提取器不会代替这些工作。

**3. 合成两种 PDF，再生成检查报告及全部页面预览。**

```sh
python scripts/compose.py work/paper/manifest.json \
  --out-dir outputs/paper --stem paper-zh \
  --font "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

python scripts/verify.py work/paper/manifest.json \
  outputs/paper/paper-zh.layout.json \
  --report outputs/paper/paper-zh.qa.json \
  --render-dir work/paper/review
```

产物包括 `paper-zh_中文.pdf`、`paper-zh_中英对照.pdf`、版面记录及检查报告。首次检查尚未完成成品视觉审核；如果机器检查均已通过、仅视觉审核标志待完成，返回非零退出码并显示该待审核项是预期行为。其他失败须先按报告修正。

**4. 查看两份 PDF 的每一页，修正问题后完成验收。**

按 [审核标准](references/translation-review.md) 检查字体缺字、叠字、裁切、图注关联、原图边界和左右页映射。只有全部输出页实际查看并通过后，才设置 `review.visual_checked=true`，随后重新运行：

```sh
python scripts/verify.py work/paper/manifest.json \
  outputs/paper/paper-zh.layout.json \
  --report outputs/paper/paper-zh.qa.json
```

`ready=true` 需要机器检查和真实的人工 / Agent 审核同时通过；它不代表脚本自动判断了翻译语义。全文还应为 `completed_paper=true`。更改译文或分区后，必须重新合成和审核；合成器拒绝覆盖同名产物，可用新的 `--stem paper-zh-v2`。

脚本自检使用自动清理的合成文件，不处理用户论文：

```sh
python scripts/self_test.py \
  --font "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"
```

当前版本的环境、自检结果与真实文献实测范围见 [验证记录](docs/validation.md)。

## 限制与许可

- 自动分区只是初稿。双栏、矢量图、跨页续句及复杂公式可能需要人工或 Agent 修正，不能仅凭提取成功认定全文完整。
- 没有内置 OCR。扫描件和损坏的文本层需要另行准备可靠文本，并逐页对照原图校正。
- 保留区域采用源 PDF 内容复用，并进行渲染比对；这不能证明原先圈定的边界没有漏掉图例，也不能代替全页视觉审核。不承诺所有 PDF 都能完全无损地转换。
- 脚本在本地处理文件，不额外调用翻译服务。本公开仓库不包含用户论文、翻译 PDF 或个人审核材料；请勿把这些文件作为贡献提交。

代码和项目文档以 [MIT License](LICENSE) 开源。MIT 许可不覆盖用户输入的论文、论文中的图片或所用字体；这些内容仍遵循各自的版权与许可。
