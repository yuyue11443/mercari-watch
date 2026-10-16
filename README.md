# Mercari 上新追踪器

监控日本 Mercari（jp.mercari.com）的关键词搜索结果，有新商品上架时推送到 Telegram（带图片、价格、链接）。

运行在 **GitHub Actions** 上，约每 10 分钟检查一次（GitHub 繁忙时会延迟到 10–30 分钟），不需要自己的电脑开着。

## 改关键词

编辑 `config.json` 的 `watches`，提交到仓库即可（也可以直接在 GitHub 网页上点铅笔图标编辑）。每项只有 `keyword` 必填：

| 字段 | 说明 |
|---|---|
| `keyword` | 关键词，空格分隔为 AND |
| `exclude` | 排除词（可选） |
| `price_min` / `price_max` | 价格区间，日元（可选，不写为不限） |

新加的关键词第一次只记录现有商品、不推送，之后才推送新上架的。

## Telegram 配置

token 和 chat ID **不写在文件里**（仓库是公开的），而是放在仓库的
Settings → Secrets and variables → Actions：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## 常用操作

- **手动检查一次**：仓库 Actions 页 → Mercari Watch → Run workflow
- **暂停监控**：Actions 页 → Mercari Watch → 右上角 `...` → Disable workflow
- **看运行记录 / 报错**：Actions 页点开任意一次运行
- `seen.json` 是已推送过的商品记录，由 Actions 自动提交，不用手动改

## 在本地运行（可选）

```bash
pip install -r requirements.txt
```

PowerShell 中先设置环境变量再运行（`--test` 发测试消息，`--once` 检查一次，不加参数则持续运行）：

```bash
$env:TELEGRAM_BOT_TOKEN="你的token"; $env:TELEGRAM_CHAT_ID="你的chatid"; python mercari_watch.py --test
```

注意：本地和 Actions 同时运行会各自推送。

## 注意

- 公开仓库如果 **60 天没有任何提交**，GitHub 会自动停用定时任务（停用前会发邮件）。到 Actions 页点 Enable 即可恢复。
- 这里用的是 Mercari 网页版自己调用的非公开搜索接口。Mercari 改接口后脚本可能失效，报错会显示在 Actions 运行记录里。
