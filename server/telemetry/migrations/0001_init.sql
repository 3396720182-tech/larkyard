-- Larkyard 匿名统计：原始报告（只有维护者能查）和每周公开汇总。不存 IP、User-Agent。
CREATE TABLE IF NOT EXISTS reports (
  install_id   TEXT    NOT NULL,
  day          TEXT    NOT NULL,           -- 报告统计的是哪一天（客户端本机时间）
  schema       INTEGER NOT NULL,
  received_day TEXT    NOT NULL,           -- 收到的日期（UTC，只到天，不存时刻）
  body         TEXT    NOT NULL,           -- 校验通过的报告 JSON
  PRIMARY KEY (install_id, day)
);
CREATE INDEX IF NOT EXISTS reports_day ON reports(day);

CREATE TABLE IF NOT EXISTS summaries (
  week TEXT PRIMARY KEY,                   -- 汇总窗口的最后一天
  body TEXT NOT NULL
);
