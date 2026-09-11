# UR House Watcher

每天自动检查 UR 大島四丁目 / 大島六丁目空房。

## 已部署位置

本项目已放在当前仓库的 `ur-house-watcher/` 目录，并通过 GitHub Actions 定时运行。

工作流：`.github/workflows/ur-house-watcher.yml`

## 监控条件

- 大島四丁目：2LDK / 3DK，面积 >= 50㎡
- 大島六丁目：2LDK / 3DK，面积 >= 50㎡
- 每天日本时间 09:00 自动检查
- 也可以在 Actions 页面手动点 `Run workflow`

## 通知方式

默认：发现新房源时自动创建 GitHub Issue。

可选：Telegram 推送。需要在仓库 Secrets 中添加：

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

没有 Telegram Secret 也能正常运行，只是不会发 Telegram。

## 重要提醒

UR 空房一般是先着顺。脚本只能帮你尽快发现网页变化，发现后仍然要尽快打开 UR 官网并联系 UR。
