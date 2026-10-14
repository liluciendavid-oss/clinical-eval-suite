# 工程验证记录

日期：2026-10-14（Asia/Shanghai）。对象：本地发布候选版的代码和合成测试流程。

## 实测结果

- `python -B src/verify_all.py`：13/13道验收门通过，退出码0。
- `python -B -m unittest discover -s tests -p test_verify_isolation.py -v`：2项回归测试通过，包含fast/full子场景和失败退出场景。
- 题库严格校验：ERROR 0、WARN 0。
- 缺陷注入器：66个经最终文本核实的注入缺陷实例。
- 规则层阴性对照：confirmed数量0。
- 评分审计器：19个内置自检用例按预期检出。
- DEMO报告渲染门通过；临时结果执行结束后清理，不作为真实模型结果发布。

完整输出：[验收日志](verification_output.txt)、[隔离回归测试日志](isolation_test_output.txt)。日志保留本机Python启动器输出的路径提示；两条命令实际均以0退出。

## 验证了什么

验收脚本仅将代码、文档、提示词、测试、工具、题库及依赖清单复制到一次性临时目录。真实回答、key、manifest、score_panel、人工评分、统计和结果文件不进入验收副本。

回归测试用合成哨兵文件覆盖这类路径：检查未被复制、原字节和修改时间不变；子进程写入留在临时目录，成功和失败均清理。此测试与原13门分开，未降低原验收阈值。

第11门实际只生成pointwise输入，名称已据实修正。pairwise代码存在，但本次13门不能单独证明真实成对judge评测已经完成。

## 环境与复现

实测环境：Windows，Python 3.13.12，pandas 3.0.2，numpy 2.4.3，matplotlib 3.10.8。代码目标Python 3.10+；本次未实测全部平台或所有兼容版本组合。

常规命令：

```bash
python -m pip install -r requirements.txt
python -B -m unittest discover -s tests -p test_verify_isolation.py -v
python -B src/verify_all.py
```

本机执行沙箱限制了默认临时目录，因此运行时仅为本次进程将TEMP/TMP指向可写的工作区临时目录。一般环境使用系统临时目录；如遇权限问题，可将这两个变量设到自己的可写临时目录。没有修改全局Python安装或安装额外依赖。

## 不能由本次验证推出的结论

工程测试通过不等于题库医学事实全部复核，不等于已得到可靠的人际一致性、LLM-judge表现或模型排名。真实评分证据、R2/judge范围及结果状态见[发布状态](../docs/release_status.md)。
