# 问问镇涛

基于郑镇涛实操知识的 AI 电商落地顾问与创作助手。安装后输入 `/zt`，直接说问题或发素材。

[![版本](https://img.shields.io/github/v/release/zhengzhentao86/wenwen-zhentao)](https://github.com/zhengzhentao86/wenwen-zhentao/releases/latest)
[![许可：CC BY-NC 4.0](https://img.shields.io/badge/License-CC_BY--NC_4.0-lightgrey.svg)](LICENSE)

**允许个人非商业使用、学习与研究；商业用途需另行授权。** 本项目采用与 [DBS](https://github.com/dontbesilent2025/dbskill) 相同的 CC BY-NC 4.0 许可公开源码。署名、非商业限制及原有 MIT 组件的适用范围见下方许可说明。

## 能帮你做什么

| 你遇到的问题 | 得到的帮助 |
|---|---|
| 学完课程后，实际操作卡住 | 从 191 条答疑与方法中按需找答案，给结论、步骤和检查办法 |
| 想写或修改商品视频提示词 | 完整中文提示词，或对现有提示词的具体诊断与最小修改 |
| 想分析、复刻参考视频 | 根据可读取的画面与声音拆解，保留结构并适配自己的商品 |
| 有一条好母版，想裂变 10 条 | 保留商品事实，生成实质不同、各自完整的提示词 |
| 想做人物三视图、故事板 | 可复制的提示词；宿主具备相应工具时可进一步生成图片 |
| 想去水印、转高清、局部改视频或处理声音 | 按素材和工具能力给处理要求、操作路径与验收方法 |
| 多维表格、素材整理、团队落地有问题 | 字段排错、数据口径、素材去重、流程与交付建议 |

## 安装

把下面这段口令发给支持安装 Skill 的 AI 助手：

```text
请帮我安装“问问镇涛”技能：https://github.com/zhengzhentao86/wenwen-zhentao
同时安装 zt 入口和 wenwen-zhentao 主技能，并验证安装成功。
安装完成后，请读取已安装主技能的 references/welcome.md，在当前对话主动向我介绍用途、能帮我做什么、使用示例，以及作者微信和领取 AI 电商知识库的方式。
```

欢迎介绍也可在首次输入 `/zt` 时查看。手动解压或 CLI 安装不会自行发送聊天消息，需由 AI 安装助手展示，或加载 Skill 后展示。

**添加郑镇涛微信：7838053，可以领取 AI 电商知识库。**

### 方式一：使用 Skills CLI

在终端运行：

```sh
npx -y skills add zhengzhentao86/wenwen-zhentao -g --skill '*' --copy
```

该命令由第三方 [Skills CLI](https://github.com/vercel-labs/skills) 提供，需要 Node.js 和网络；它会根据所选宿主安装本仓库的两个 Skill。没有 Node.js 时可用下面的 ZIP 方式。

### 方式二：下载完整安装包

打开 [最新 Release](https://github.com/zhengzhentao86/wenwen-zhentao/releases/latest)，下载 `zt-0.5.2.zip`，解压得到：

```text
zt/
wenwen-zhentao/
```

把两个目录并排放入宿主支持的 Skill 目录。Codex 常用 `~/.codex/skills/`；Claude Code 常用 `~/.claude/skills/`。保留包内相对位置，不要把整个 GitHub 仓库根目录作为单个 Skill 导入。

安装后重新加载技能或新开会话。Codex 桌面输入 `/zt` 并选中入口；Codex CLI 可使用 `$zt`。其他工具的斜杠菜单由宿主决定，支持导入 Skill 不等于支持同样的命令注册方式。

[完整安装与更新说明](docs/INSTALLATION.md)

## 直接这样问

```text
/zt 我上传了商品图，但生成的视频里瓶口结构总变。先帮我排查。

/zt 给这个随行杯写一条15秒中文视频提示词，卖点是单手开盖。

/zt 只诊断这条提示词，先别整段重写：……

/zt 参考这个视频的结构，换成我的商品，保留声音与画面的配合。

/zt 保留这个母版的购买理由和证明动作，裂变10条完整提示词。

/zt 多维表格刚建记录就发空报告，改备注又重复通知，怎么改？
```

不需要先选菜单或记住子模块。简单问题直接回答；确实需要提示词、截图、音频或视频时，再补充必要材料。镇涛的方法与建议直接用于帮助你操作，助教保持 AI 身份。

## 怎样工作

`/zt` 统一入口 → 识别当前目标 → 选择方法或创作模块 → 按需读取知识 → 交付答案或成品。

`zt` 只负责入口，`wenwen-zhentao` 保存知识、创作模块、离线检索和版本检查。已有商品提示词、视频反推专用 Skill 时按需联动；没有它们时使用内置模块，不要求额外安装。

日常答疑不需要登录维护者飞书、填写 API 密钥或部署服务。使用时会检查已发布版本；宿主具备 Python、网络和安装目录写权限时尝试自动更新，离线或能力不足时继续使用现版。用户素材与对话不会随版本检查上传。

## 当前版本与验证

v0.5.2 加入安装后与首次使用的欢迎介绍、使用示例和作者联系方式，包含 191 条答疑与方法。知识正文沿用 v0.5.0；许可和更新机制沿用 v0.5.1。

原有知识版的 96 项工程检查通过；58 道检索题原问前五命中 56 道，其余两道用操作关键词定位成功。本机 Codex 的 16 道定向答题计划完成 12 道，独立 AI 评阅为 11 道通过、1 道有小项遗漏；其余 4 道网络超时未评分。公开发行改动的检查见 [版本说明](docs/RELEASE-NOTES-0.5.1.md)。

这些结果不代表真实学员解决率或经营收益。媒体分析、图片和视频生成依赖宿主实际工具；没有把写好提示词当成已生成文件。Claude 等其他宿主尚未完成同等行为验收。

## 贡献与维护

欢迎通过 [Issues](https://github.com/zhengzhentao86/wenwen-zhentao/issues) 提交可复现问题、补充方法或改进建议。请去掉账号、密钥、客户信息和私人课程原文。

维护代码、合成测试及评测问题在本仓库。原始课程资料、私人快照、客户记录与审核证据不公开。维护者操作方式见 [开发与发行](docs/DEVELOPMENT.md)。

## 许可

Copyright © 2026 郑镇涛。

本项目原创内容按 [CC BY-NC 4.0](LICENSE) 许可：

- 允许非商业使用、复制、分享和改编，包括个人学习与研究。
- 分发或公开衍生作品时保留署名与许可链接，并标明修改。
- 商业用途不在该许可授权内，需联系作者取得单独授权；是否商业使用取决于用途，不能仅按使用者是个人还是公司判断。

项目中从既有 `zhentao-product-video-prompt-writer`、`reverse-video-prompt` 引入的原有部分继续适用 MIT，其声明保留在 [原有组件许可](skills/wenwen-zhentao/references/creative/UPSTREAM-LICENSES.txt)。本次 CC BY-NC 许可不会撤销这些部分此前已授予的 MIT 权利；本项目新增知识、编排与修改按 CC BY-NC 4.0 提供。

商业授权可通过 [作者 GitHub 主页](https://github.com/zhengzhentao86) 的公开联系入口咨询。项目采用“公开源码、非商业许可”的发布方式；CC BY-NC 4.0 具有非商业限制，不属于 OSI 定义的开源软件许可证。
