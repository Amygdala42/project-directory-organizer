# Project Directory Organizer

版本：**v1.0.0**

为 Codex 提供项目目录规划、文件风险检查与整理、成果归档、只读盘点及精确删除恢复功能。支持中文或英文业务目录。

## 工作方式

1. 用短问题补齐地址、操作、目录语言及必要的分析/环境需求。
2. 在正式回复中展示完整目录计划和项目规则，等待文字反馈。
3. 收到明确的实际操作指令后，执行对应范围。

已有答案不重复问，未回答不代选；模拟不会自动变成实际操作。整理时只移动检查充分且通过的独立组，有风险或无法确认的内容保留原位并给出建议。

## 项目内安装

在本仓库根目录打开 PowerShell，将下面的项目地址替换成自己的实际项目路径：

```powershell
$projectPath = 'D:\Projects\my-project'
if (-not (Test-Path -LiteralPath $projectPath -PathType Container)) {
    throw '项目目录不存在，请先填写正确地址。'
}
$skillsPath = Join-Path $projectPath '.agents\skills'
$destination = Join-Path $skillsPath 'project-directory-organizer'
if (Test-Path -LiteralPath $destination) {
    throw '该项目已安装此技能，请先核对已有版本，不直接覆盖。'
}
New-Item -ItemType Directory -Path $skillsPath -Force | Out-Null
Copy-Item -LiteralPath '.\skills\project-directory-organizer' -Destination $destination -Recurse
```

复制整个技能目录，包括 scripts、references 和 assets。安装位置是项目下的 `.agents/skills/`。

在该项目中调用：

```text
$project-directory-organizer 主目录是 D:\Projects\Cancer，首先研究胃癌，请先给出目录计划。
```

新建项目会生成 `AGENTS.md` 和 `PROJECT_RULES.md`：前者是读取入口，后者保存实际采用的目录、环境归属、命名、成果批次和文件保护规则。环境目录只预留时，不代表已安装软件。报告与分析成果按日期和用途分批，同次 PPT 和对应 PDF 共址、同版本。

[完整使用说明](docs/usage.md) · [技能入口](skills/project-directory-organizer/SKILL.md) · [目录示例](skills/project-directory-organizer/references/examples.md)

## 运行条件

- 需要支持本地技能的 Codex 环境。弹窗流程依赖当前会话提供可用的提问工具；不可用时会如实说明并停在待答步骤。
- 脚本使用 Python 3.9+，只依赖标准库。
- 移动执行/回退，以及删除执行/恢复/永久清除目前只支持 Windows。其他平台可使用对应的只读计划功能。
- 完整回归测试以 Windows 为验证环境；符号链接测试在缺少所需权限时可能跳过。
- 文件系统检查与日志不替代备份，也不保证发现所有外部引用。

## 仓库结构

```text
.
├─ README.md
├─ VERSION
├─ .gitignore
├─ docs/
│  └─ usage.md
├─ skills/
│  └─ project-directory-organizer/
│     ├─ SKILL.md
│     ├─ agents/
│     ├─ assets/templates/
│     ├─ references/
│     └─ scripts/
└─ tests/
```

`skills/project-directory-organizer/` 是完整可安装技能；`tests/` 是独立开发测试。

## 验证

在仓库根目录运行：

```powershell
python -B -m unittest discover -s tests -p "test_*.py" -v
```

测试夹具写入系统临时目录下的 `project-directory-organizer-tests/`，不写入仓库或实际研究目录。夹具为复核失败保留，不会自动清除；请勿将这些运行产物上传到仓库。
