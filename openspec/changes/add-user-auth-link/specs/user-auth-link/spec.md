## ADDED Requirements

### Requirement: 自检查用户身份
`ticket doctor` SHALL 有一项 `user_auth`，按 design D2 的状态显示：没登录、授权失效或缺 design D1 里的权限时以 ❌ 开头并给出下一步；查不到（lark-cli 跑不起来、钥匙串打不开）或还没查过时以 ⚠️ 开头，不算不通过。查不到的结果 MUST NOT 覆盖之前查到的结论。

#### Scenario: 没授权
- **WHEN** `lark-cli auth status --json` 的 user 状态是 missing
- **THEN** `ticket doctor` 有一行 ❌「飞书用户身份没授权…」，心跳的 `doctor.failed` 含 `user_auth`

#### Scenario: 在 worker 里跑
- **WHEN** 之前查到的是 ok，worker 里执行 `ticket doctor` 时 lark-cli 报钥匙串打不开
- **THEN** 这一项仍显示 ✅，kv 里的状态不变

### Requirement: 装好后马上发一次授权链接
守护进程查到用户身份不可用（missing / expired / scope），且本机从来没发过授权链接时，SHALL 马上用 bot 私信主人一张带「去授权」按钮的卡片，链接是 `lark-cli auth login --no-wait --json --scope <D1 的范围>` 返回的 `verification_url`。主人没配好时 MUST NOT 发。

#### Scenario: 新装没授权
- **WHEN** 守护进程启动，状态是 missing，`userauth.first_link_at` 为空
- **THEN** 发件箱多一条给 `@owner` 的卡片，按钮打开 verification_url；`auth login` 的参数里带着全部 D1 范围；之后状态再查多少次都不再发

### Requirement: 出稿失败时再发
文档导入或视频传云空间遇到授权类失败（design D6）时，守护进程 SHALL 在没有未过期链接、且离上次发链接不少于 30 分钟时再发一条。同一次汇报里多份文档失败 MUST 只发一条。

#### Scenario: 授权后来失效
- **WHEN** 发过一次链接、之后授权成功；一周后导入报 `{"type":"authentication","subtype":"refresh_token_expired"}`
- **THEN** 状态记成 expired，守护进程下一拍发一条新链接

#### Scenario: 三份同时失败
- **WHEN** 一份结果里 3 份稿子都报 need_user_authorization
- **THEN** 只发一条授权链接

### Requirement: 授权完成后补发文档
用户在链接里同意后（或在终端授权后被守护进程查到），守护进程 SHALL 把 7 天内因为授权类失败没导入的稿子按发布规则重新核对并导入，每张单一条消息回复在原结果下面，带每份文档的「打开」按钮；已经有链接的不重复导入、不重复发。

#### Scenario: 同意之后
- **WHEN** T0001 的 `结果.md` 因为没授权没导入，随后 `auth login --device-code` 成功
- **THEN** `结果.md` 被导入一次，发件箱多一条回复到 T0001 那条结果的卡片，按钮链接是新文档；主人另收到一句「授权好了」

#### Scenario: 文件被换成符号链接
- **WHEN** 补发前 `结果.md` 被换成指向工单目录外的符号链接
- **THEN** 不导入、不发，记录被删掉

### Requirement: 失败原因说中文、写下一步
结果卡和文字兜底里稿子没能导入的说明 SHALL 按 design D6 归类写中文：授权类写「机器人已私信你授权链接，点开同意后会自动补发这份文档」；不再出现 lark-cli 的英文原文（其他类只在括号里带 ≤60 字的原因）。

#### Scenario: 没授权
- **WHEN** 导入报 `need_user_authorization`
- **THEN** 卡片那一行里有「飞书用户身份没授权」和「点开同意后会自动补发」，没有 `need_user_authorization`

### Requirement: 同一原因只报一次
授权类失败 MUST NOT 产生 `media/doc_fail` 或 `media/video_fail` 出错记录；这个原因只经自检项 `user_auth` 上报。

#### Scenario: 一张单多次失败
- **WHEN** 没授权时同一张单汇报 3 次、每次 2 份稿子失败
- **THEN** 心跳的 problems 里没有 doc_fail，`doctor.failed` 里有 `user_auth`

### Requirement: 诊断包带失败原因
`ticket diag` SHALL 包含用户身份状态、最近的导入失败原因和告警日志里相关的原文（打码后），MUST NOT 包含设备码。

#### Scenario: 生成诊断包
- **WHEN** 有一条导入失败，告警日志里有它的原文，kv 里有一条待用的授权链接
- **THEN** 诊断包里有那行原文和「有一条授权链接还没过期」，没有 device_code
