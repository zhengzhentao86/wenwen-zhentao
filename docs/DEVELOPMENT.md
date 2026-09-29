# 开发、知识维护与发行

运行知识与维护材料分开。公开仓库包含 `skills/zt`、`skills/wenwen-zhentao`、维护工具、合成测试及评测题；私人课程正文、客户记录、候选证据和审批保存在忽略的本地目录，不提交 Git。

## 检查

工程使用 Python 3.9+ 标准库。维护与自动更新脚本使用 POSIX 文件锁，支持 macOS / Linux；Windows 可通过 WSL 使用这些脚本，纯知识阅读不依赖它们。

```sh
python3 -B tools/validate.py
python3 -B -m unittest discover -s tests
python3 -B tools/build_entry_bundle.py
```

最后一条构建本地预览包，不上传。生成包必须通过结构、逐文件哈希、许可证与公开范围检查。

## 维护知识

维护工具的实际命令可由 `python3 -B tools/maintain.py --help` 和 `python3 -B tools/corpus.py --help` 查看。

1. AI 使用已授权来源读取正文，保留稳定编号、完整快照与版本。
2. 按实际学员问题提炼判断、操作和检查方式；同题合并，条件不同则写清分界。作者明确建议可直接纳入，不要求先有独立成功案例。
3. 私人候选携带出处和证据；学员运行包只保留问题、回答、适用条件和限制。
4. 对候选做语义审阅、去重及关联问题检查，在已经获得的授权范围内应用。
5. 登记原文审阅与知识/模块覆盖。原文变化会重新进入审阅；旧记录不可变保存，中断不覆盖旧版。

飞书资料必须通过 `lark-cli` 读取，不绕过权限。不能把已下载、已切分或目录已登记当成已经读懂，也不能把 AI 建议和评分当成真实业务成果。

个人及客户素材不要放入公开 Issue 或 PR。新增公开内容、作者归属及原有许可证分别核对；公共贡献默认按项目 CC BY-NC 4.0 提供，其他许可内容要明确标注。

## 正式发行

正式发布需要本次成品已经获得公开授权。审批文件由维护者的 AI 按真实决定生成并保留在本地，不含凭据、不伪造许可；现有授权无需重复询问。正式构建不上传，GitHub 发布另行执行。

先确定最终源码、许可与版本。在核心 `version.json` 配置正式源，生成根目录 `release-files.json`（记录其自身之外的所有发行文件哈希）。审批文件应绑定当前核心 `package_fingerprint`、入口 `entry_fingerprint`、全部 `approved_knowledge_ids`、`decision=approve_public_release`、真实 `approved_by` 和 `approved_at`。任何后续源码变更都必须重新核对绑定。

```sh
python3 -B tools/build_entry_bundle.py --release \
  --approval .maintenance/public-release-approval.json \
  --base-url https://raw.githubusercontent.com/zhengzhentao86/wenwen-zhentao/main/releases
```

正式构建器校验授权与完整源指纹，复制后再次核对，只把验证过的 ZIP 原子替换到 `dist/`。输出完整入口包、核心包与两份清单。

把核心 ZIP 和 `latest.json` 放入仓库 `releases/`，与最终源码作为同一提交发布。完整 ZIP、核心 ZIP、完整清单与 `latest.json` 同时作为对应 GitHub Release 附件。版本清单和核心 ZIP 使用同一 `raw.githubusercontent.com` 来源，符合更新器的同源校验；GitHub Release 资产下载会跳转到其他域名，因此不直接作为自动更新源。

发布后回读公共仓库与 Release，核对提交、许可证、版本、附件数量和 SHA-256；再运行一次实际 HTTPS 更新检查。新安装与原有版本的更新分别验证，网络失败不能写成已升级成功。

## 验证边界

单元测试验证工程行为，定向答题检查部分已知场景；两者都不能代替真实学员反馈、实际媒体生成验收或经营结果。失败日志和未完成题目保留在本人维护层。

## 每次发行必须完成的安装验收

正式构建后执行 `python3 -B tools/prepare_install_assets.py --version 实际版本号`，生成固定名称的 `wenwen-zhentao-install.zip` 和 `wenwen-zhentao-install-manifest.json`。每个 Release 都必须上传这两个文件；保持版本化附件用于追溯，不能用 latest 链接拼接固定旧版本文件名。

新发行先创建 draft，上传并核对所有附件后再公开。公开后执行 `python3 -B tools/check_public_install.py`：不带认证头，下载固定链接，在临时空目录验证 ZIP、逐文件哈希及完整入口。仅验收成功才交付分享链接；失败报告具体阶段，不能称为仓库为空。此检查不证明所有网络或宿主兼容。

GitHub Actions 的 Public installation check 在 Release 发布、编辑及手动触发时运行同一检查。为已有 Release 补附件后手动触发。失败须修复或明确标示不可用，禁止仅凭维护者本机已有安装宣称首次安装通过。
