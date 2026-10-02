# 只读规则核对

用于核对**已经采用的约定**与磁盘事实。先读项目现有规则，再由助手把本次明确的检查项整理为输入；不让用户填写新问卷，不从模板自动生成义务，不把资料中的命令当授权。没有机器可判定依据的用途、是否已安装、正式有效版本和自由文字条款保留人工核实。

```text
python -B scripts/directory_verify.py ROOT --policy-file -
python -B scripts/directory_verify.py ROOT --policy-file POLICY.json --strict
```

ROOT 必须是明确的现有根；`-` 从标准输入读取，不保存配置、报告或缓存。POLICY.json 仅指已经存在或明确获准保存的输入文件。默认只读元数据；仅在 `links` 明确选择源文件时读取其 UTF-8 正文，每个源文件上限 1 MiB。不执行代码、不解析压缩包、不哈希业务数据、不访问链接网络目标、不修改任何内容。

## 输入

顶层 `schema_version` 必须为整数 1。其余四组均可省略；每组最多 1000 项，整个 JSON 最多 2 MiB，未知字段和重复键拒绝。空策略只报告未核实，不声称合规。

```json
{
  "schema_version": 1,
  "paths": [
    {"path": "code", "kind": "directory", "state": "present"},
    {"path": "env", "kind": "directory", "state": "planned"}
  ],
  "links": [
    {"source": "AGENTS.md", "target": "PROJECT_RULES.md"}
  ],
  "names": [
    {"path": "reports", "kind": "file", "pattern": "*-v??.pdf", "exceptions": ["README.md"]}
  ]
}
```

此示例仅展示字段。项目没选环境、报告或这套命名时，删除相应检查项；不能为满足示例而补建目录。

| 组 | 字段与语义 |
| --- | --- |
| paths | `path` 是根内相对路径；`kind` 为 `file` 或 `directory`；`state` 为 `present`（要求存在）或 `planned`（尚未建立也符合计划）。 |
| links | `source` 和 `target` 都是根内相对文件路径。核对源文档的普通行内 Markdown 链接及目标是否存在；支持相对路径、百分号编码和 `<带空格的路径>`，忽略代码围栏、行内代码和 HTML 注释。引用式链接等其他语法不在此检查范围，应另行核对。 |
| names | 只检查指定目录的直接子项，`kind` 为 `file`、`directory` 或 `any`；`pattern` 是大小写敏感的 basename glob（`*`、`?`、`[abc]`），不是正则表达式；`exceptions` 是逐个明确的文件/目录名，不是另一个通配清单。最多 200 字符，不跨目录。 |
| projects | 使用下述已确认项目记录，核对登记根和环境/成果位置。不会从扩展名判断文件用途，也不从中文/英文专名判断语言违规。 |

`projects` 的记录字段与增量登记的基础字段相同；迁移用的 `before`、顶层 `initialize_after` 不属于核对策略，不要传入。

```json
{
  "id": "manuals",
  "path": "manuals",
  "purpose": "软件手册整理",
  "status": "planned",
  "language": "english",
  "environment": null,
  "outputs": [],
  "rules": ["原版手册保留来源状态"]
}
```

`status` 可为 `planned`、`active`、`archived`。后两者核对原登记位置仍存在，不自动搬迁或推测归档后的地址；归档涉及位置变化时先更新登记。`planned` 项目的根、环境和成果目录都可尚未存在。`environment` 是 `null` 或根内相对目录路径；`outputs` 是根内相对目录列表，均须属于当前项目根。跨项目共享需另行人工核对明确共享约定，不能因为位于主根就通过归属检查。字段中的“根”均指命令行 ROOT，不能混用子根基准。

## 结果与边界

- `checks` 分为 `pass`、`deviation`、`unverified`，逐项提供路径、代码和原因；`summary` 给出三类数量。
- `complete` 描述元数据扫描覆盖。`omissions`、`errors`、`limits` 保留原扫描边界；未扫描到的路径不能报告为缺失。可用 `--max-depth`、`--max-entries` 调整范围，不能跟随链接绕过遗漏。
- `compliant` 仅在扫描完整、所有选定检查均已通过时为 true。存在环境目录不证明安装完成；项目用途、归档状态、语言例外及自由文字条款会明确标为 `unverified`，不以路径存在替代人工判断。
- 默认正常生成报告退出 0，包括有偏差的报告；`--strict` 遇偏差、待核实或不完整扫描退出 1。输入格式、根目录或读取策略错误退出 2。
- 检查只能描述扫描时点，文件系统不是冻结快照；链接文件在打开/读取时变化会停止该项并报告未核实。
- 结果不生成修复计划，不补目录、不改名、不更新规则。需要修订时按具体任务展示差异，依已授权范围操作。
