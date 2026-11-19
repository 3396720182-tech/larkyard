# Larkyard 匿名统计后端（Cloudflare Worker + D1）

客户端说明和字段表见 [`docs/telemetry.md`](../../docs/telemetry.md)。这里是维护者部署和运维用的说明。

- `POST /v1/report`：收一份每日报告。严格按 `ticketd/telemetry_schema.json` 校验（多一个键、类型不对、取值不在枚举里，整份 400 拒收），超过 8KB 返回 413，按 `(install_id, day)` 去重写入（重复的返回 200）。日期只收最近 8 天内的。不读、不存 IP 和 User-Agent；按 IP 限流用 Cloudflare 的 Rate Limiting 绑定（只在边缘计数）。
- `GET /v1/summary`：最近一份全体汇总（公开，可跨域读）。活跃机器少于 20 台时只有总量；取值少于 5 台机器的并进 `other`。不含 install_id。
- 定时任务（每天 03:17 UTC）：删掉 365 天前的报告；每周一生成上一周（7 天）的汇总。
- 不需要任何密钥：上报接口本来就是公开的，靠严格校验、去重和限流防垃圾数据。

## 本地测试

不需要 wrangler 和 Cloudflare 账号，Node 22.5+ 自带的 `node:sqlite` 模拟 D1：

```
cd server/telemetry && npm test
```

仓库根目录的 `tests/test_telemetry.py` 里另有一条交叉校验：Python 客户端生成的报告，后端的校验函数也认。

## 部署清单（维护者本人的 Cloudflare 账号）

权限集中在维护者本人：不加其他成员，不和公司账号共用，账号开两步验证。部署用的 API Token 不进仓库、不进 `config.env`。

1. **账号**：登录自己的 Cloudflare 账号，确认已开两步验证；在 Workers 页面确认 `workers.dev` 子域名。子域名如果带个人用户名、又不想出现在公开仓库里，先改成中性的名字（如 `larkyard`）。
2. **API Token**（如果不用 `wrangler login` 的浏览器授权）：My Profile → API Tokens → Create Token，权限只给 `Account · Workers Scripts · Edit` 和 `Account · D1 · Edit`，账户范围只选这一个账号。存进本机钥匙串，例如：
   `security add-generic-password -a "$USER" -s larkyard-cloudflare -w`（会提示输入 token）。
3. **建库**：
   ```
   cd server/telemetry
   npx wrangler@4 d1 create larkyard-telemetry
   ```
   把输出里的 `database_id` 填进 `wrangler.toml`（替换 `REPLACE_WITH_D1_DATABASE_ID`）。database_id 不是密钥，但它属于你的账号，确认后再提交。
4. **建表**：`npx wrangler@4 d1 migrations apply larkyard-telemetry --remote`
5. **部署**：`npx wrangler@4 deploy`。输出里的地址形如 `https://larkyard-telemetry.<子域名>.workers.dev`。
6. **冒烟**：
   ```
   curl -s https://larkyard-telemetry.<子域名>.workers.dev/                       # 说明文字
   curl -s -X POST -H 'content-type: application/json' --data '{"x":1}' \
        https://larkyard-telemetry.<子域名>.workers.dev/v1/report                 # 400 invalid
   ```
   不要用 `docs/telemetry-example.json` 原样冒烟：它的 install_id 是示例值，会作为一条真数据留在库里（要试就试完按第 9 条删掉）。
7. **自己先开一周**：在自己的 `~/.ticketd/config.env` 写 `TICKETD_TELEMETRY_URL=https://larkyard-telemetry.<子域名>.workers.dev/v1/report`，重启服务，`ticket telemetry show` 看内容，`ticket telemetry on` 开启。一周后对照 `ticket stats` 核对数字，用下面的查询看原始数据。
8. **写进代码和文档**：核对无误后，把地址写进 `ticketd/telemetry.py` 的 `DEFAULT_URL`，并填上 `docs/telemetry.md` 里的 `<后端域名>`、`<公开数据链接>`（`…/v1/summary`）、`<联系方式>`，记 CHANGELOG。
9. **运维查询**（只有你能查）：
   ```
   npx wrangler@4 d1 execute larkyard-telemetry --remote --command "SELECT day, COUNT(*) FROM reports GROUP BY day ORDER BY day DESC LIMIT 14"
   npx wrangler@4 d1 execute larkyard-telemetry --remote --command "DELETE FROM reports WHERE install_id='<用户提交的 ID>'"   # 删除申请
   ```

## 回滚

- 停收：Cloudflare 后台把这个 Worker 的 `workers.dev` 路由关掉（或 `npx wrangler@4 delete`）。客户端发送失败只会在本机留 7 天再删，不告警、不影响派单。
- 删数据：`npx wrangler@4 d1 delete larkyard-telemetry`。
