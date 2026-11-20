> 本变更先停在 spec，审过之后再开始写代码。

## 1. 规格和文档

- [ ] 1.1 `docs/spec.md` 新增「控制台接入」一节（引用本变更的两份 spec）；README 加「只有执行过 `ticket enroll` 才启用」
- [ ] 1.2 `ticket doctor` 每一项加稳定的英文 id（心跳里用）

## 2. 连接和本机文件（`ticketd/console.py`）

- [ ] 2.1 `request()`：只收 `https://`、系统 CA + 主机名校验（不可关闭）、超时、响应体上限、可注入的假连接层
- [ ] 2.2 `console.json` / `device-token` 的读写（700 / 600、临时文件 + rename）、`--leave` 删除
- [ ] 2.3 单测：证书校验失败时没有写出任何请求字节、不重试不降级；`http://` 被拒；令牌不出现在任何输出里；`ticket doctor` 在没有 CA 证书时给出提示

## 3. 入网（`ticket enroll`）

- [ ] 3.1 参数检查、worker 环境拒绝、标准输入必须是终端、已入网拒绝
- [ ] 3.2 采集清单 `c1` 打印和 `yes` 确认、清单 SHA-256
- [ ] 3.3 取公钥 → 指纹比对（复用 `updsig`）→ 本机已信任不同公钥时失败
- [ ] 3.4 `enroll/start`、确认码输入和重试、`enroll/confirm`、各错误码的中文提示
- [ ] 3.5 成功后写公钥（复用 `ticket update trust` 的写入）和控制台文件；写入失败的提示
- [ ] 3.6 `ticket enroll --leave`（控制台不可达时询问）
- [ ] 3.7 单测：spec `console-enrollment` 每个场景（用假控制台）

## 4. 心跳

- [ ] 4.1 `build_heartbeat()`（白名单字段，和 `ticket console show` 共用）
- [ ] 4.2 守护进程主循环：300 秒 + 0～30 秒抖动、`next_in` 范围、后台线程、`kv` 和 `events`
- [ ] 4.3 401 三种错误码停止上报；`ticket console status`
- [ ] 4.4 `ticket doctor` 控制台一项；状态卡 / 结果卡详情的内部版标识
- [ ] 4.5 自动更新：入网且未配 `TICKETD_UPDATE_URL` 时以控制台为更新源，经 `request()` 带设备令牌
- [ ] 4.6 单测：spec `console-heartbeat` 每个场景；未入网时 24 小时模拟内零请求；心跳里搜不到工单标题和原话

## 5. 验收（和控制台一起）

- [ ] 5.1 维护者机器入网 → 控制台出现 → `ticket console show` 和控制台详情一致
- [ ] 5.2 用发给别人的入网码入网失败；指纹不符时本机没有写入公钥；控制台证书自动续期后心跳不中断
- [ ] 5.3 控制台停 24 小时：派单正常，恢复后只补一次心跳
- [ ] 5.4 吊销后 10 分钟内心跳被拒、派单照常
