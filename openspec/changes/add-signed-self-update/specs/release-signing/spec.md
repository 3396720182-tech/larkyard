## Purpose

让发布者在自己的电脑上，从一个 git tag 打出可验证的版本包和签名清单，使客户端只凭本机信任的公钥就能判断一个版本是不是发布者本人发的、内容有没有被改过。

## ADDED Requirements

### Requirement: 从 git tag 打出可复现的版本包
发版工具 SHALL 只从一个已存在的 git tag 打包，包内只含该 tag 下被 git 跟踪的文件，文件名为 `larkyard-<版本>.tar.gz`。工作树有未提交改动、tag 不存在、或 tag 指向的代码里 `ticketd/__init__.py` 的版本号与 tag 不一致时，发版工具 MUST 拒绝打包并说明原因。

#### Scenario: 正常打包
- **WHEN** 发布者对已存在的 tag `v0.3.0` 执行打包，且该 tag 里的 `__version__` 为 `0.3.0`
- **THEN** 生成 `larkyard-0.3.0.tar.gz`，包内只有该 tag 下被跟踪的文件，不含 `.git/`、未跟踪文件和本机配置

#### Scenario: 版本号不一致
- **WHEN** tag 为 `v0.3.0`，但该 tag 里的 `__version__` 为 `0.2.0`
- **THEN** 发版工具退出码非 0，提示版本号与 tag 不一致，不生成任何文件

#### Scenario: 同一个 tag 打两次
- **WHEN** 对同一个 tag 在同一台机器上打包两次
- **THEN** 两次得到的包 SHA-256 相同

### Requirement: 版本清单
每个版本包 SHALL 配一份 `manifest.json`，至少包含：格式版本、产品名、版本号、包文件名、包的 SHA-256、包大小、发布通道、最低可升级起点版本、对应的 git 提交号、签名时间、更新说明，以及可选的回滚标记和新公钥。清单 MUST 是确定的 UTF-8 JSON（键排序、无多余空白），签名针对的就是这份文件的原始字节。

#### Scenario: 清单字段完整
- **WHEN** 发布者为 `0.3.0` 生成通道为 `canary` 的清单
- **THEN** 清单包含上面列出的全部必需字段，`sha256` 与包文件实际的 SHA-256 一致，`commit` 与 tag 指向的提交一致

### Requirement: 用发布者本机的 Ed25519 私钥签名
发版工具 SHALL 用 `ssh-keygen -Y sign` 和发布者指定的 Ed25519 私钥对清单签名，签名命名空间固定为 `larkyard-release`，输出 `manifest.json.sig`。发版工具 MUST NOT 把私钥写入仓库、版本包、清单或任何日志，也 MUST NOT 提供在服务器上签名的模式。非 Ed25519 的密钥 MUST 被拒绝。

#### Scenario: 签名成功
- **WHEN** 发布者用自己的 Ed25519 私钥签 `0.3.0` 的清单
- **THEN** 生成 `manifest.json.sig`，用对应公钥和命名空间 `larkyard-release` 执行 `ssh-keygen -Y verify` 通过

#### Scenario: 用错误的密钥类型
- **WHEN** 发布者指定一把 RSA 私钥签名
- **THEN** 发版工具退出码非 0，提示只接受 Ed25519，不生成签名

### Requirement: 回滚清单
发版工具 SHALL 能为一个已发布的旧版本生成「回滚清单」：内容是那个旧版本的包信息，加上 `rollback_to` 字段（等于该旧版本号）和本次签名时间，由发布者重新签名。只有带签名回滚标记的清单才允许客户端装回更低的版本。

#### Scenario: 生成回滚清单
- **WHEN** 当前已发布 `0.3.0`，发布者为旧版本 `0.2.1` 生成通道 `stable` 的回滚清单
- **THEN** 得到一份 `version` 为 `0.2.1`、`rollback_to` 为 `0.2.1`、通道为 `stable` 的新清单和它的新签名，旧包文件不需要重新打

### Requirement: 换钥
清单 SHALL 可以带一个 `next_public_key` 字段（Ed25519 公钥）。这样的清单 MUST 由当前受信任的私钥签名；客户端装上这个版本并通过自检后，改为只信任新公钥。

#### Scenario: 换钥版本
- **WHEN** 发布者用旧私钥签发一个带 `next_public_key` 的 `0.4.0`
- **THEN** 清单的签名用旧公钥能验过，清单里的新公钥是合法的 Ed25519 公钥
