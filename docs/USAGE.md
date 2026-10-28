# 使用

下列命令均从项目根目录执行。Linux 运行方式：

```bash
bash tools/with-linux-tools.sh bash tools/linux-env.sh \
  .runtime/linux-python/bin/tracewaltz --help
```

Windows 的 `tracewaltz.cmd` 包装同一 Linux 入口，默认使用 WSL 的 `Ubuntu-24.04`。

## 从编码任务开始

把 `examples/flag-parser` 复制到工作目录，先建立 Git 起点，再提交需求。该例子故意包含字符串布尔值解析错误。

```bash
cp -r examples/flag-parser /path/to/my-flag-task
git -C /path/to/my-flag-task init
git -C /path/to/my-flag-task add .
git -C /path/to/my-flag-task commit -m "task starting point"

bash tools/with-linux-tools.sh bash tools/linux-env.sh \
  .runtime/linux-python/bin/tracewaltz produce /path/to/my-flag-task \
  --task-id flag-parser --run-id first-attempt --family parsing --split seed \
  --instruction "Read REQUEST.md, fix settings.py, keep tests unchanged, and run the tests." \
  --verify "python -m unittest discover -s tests -v"
```

`produce` 创建代码副本，启动 DSH，保存记录，再调用 内部任务审核流程。是否构建、加难、补齐或拒绝由审核和实际材料决定；不会仅凭模型说“完成”就加入正式评测集。

本次修复交付代码；后续 Gear 改进交付 Agent 工作方式。这是两种不同结果。

## 任务、经验与评测

| 入口 | 输入与输出 |
| --- | --- |
| `run` | 编码需求、Git 仓库、测试命令 → 代码修改、执行记录和验证结果 |
| `produce` | 上述编码任务 → 任务审核与可用任务资产 |
| `asset validate/calibrate/harden/evolve_experience/products` | 任务包 → 验证、标定、派生任务或经验结果 |
| `experience-run` | 已有编码记录 → 原始记录绑定的压缩经验 |
| `export-native` | 可复现任务包 → Harbor 题目、起始环境、私有验证与参考实现 |
| `freeze` | 选定任务资产 → 固定本轮题集与划分 |
| `evolve` | 冻结题集、现有配置 → 配置搜索、评测、采用判断和通过后的发布 |
| `cycle` | 明确的阶段计划 → 同一轮编码、任务处理、优化及后续编码状态 |

查看准确参数：

```bash
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz export-native --help
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz freeze --help
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz evolve --help
bash tools/linux-env.sh .runtime/linux-python/bin/tracewaltz cycle --help
```

优化题和留出题在轮次开始前固定。改进 Agent 能读取授权优化题的完整公开反馈，并修改 `preset/`、`prompts/`、`skills/`、`plugins/` 和 `workflows/`。参考答案、隐藏测试和留出材料分别提供给对应执行与验证组件。

单独新增文件不代表生效：配置入口需要加载插件和提示词，Skill 需被索引或读取，工具／工作流需注册或引用。系统检查并冻结候选，容器运行后再依据原生规则决定是否采用。

## 查看状态与恢复

`.state/` 保存任务、版本、运行与发布状态；`artifacts/` 保存报告及回归输出。`.state/releases/active.json` 指向下一次编码使用的版本，现有会话继续使用原版本。

`evolve --resume` 恢复指定原生运行；`cycle --resume` 核对原轮与已完成回执后继续。响应未知时先检查原工作进程和原生记录，不盲目再次调用模型。`tools/recover-completed-meta.py` 用于核对已完成工具执行后的明确字段别名问题，不修改原生分数或伪造候选。

完整闭环验收需要真实候选比较、独立留出、采用发布及后续会话加载证据；仅运行本地回归或生成经验还不足以说明能力提升。
