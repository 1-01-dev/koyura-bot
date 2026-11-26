# Native deployment assets

The step-by-step native install guide now lives in
[`docs/deployment.md`](../docs/deployment.md): layout, `/etc/koyura/koyura.env`,
the units, building `telegram-bot-api` from source and HTTPS in front of the
dashboard.

This directory only holds the units themselves:

| File | Service |
| --- | --- |
| [`systemd/koyura-engine.service`](systemd/koyura-engine.service) | Media engine (Flask, `127.0.0.1:8899`) |
| [`systemd/koyura-dashboard.service`](systemd/koyura-dashboard.service) | Admin dashboard (uvicorn, `127.0.0.1:8080`) |
| [`systemd/koyura-bot.service`](systemd/koyura-bot.service) | Telegram bot |
| [`systemd/koyura-telegram-bot-api.service`](systemd/koyura-telegram-bot-api.service) | Self-hosted Bot API server (`127.0.0.1:8081`), optional |

Install them as described in
[`docs/deployment.md`](../docs/deployment.md#24-units):

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemd-analyze verify koyura-engine.service koyura-dashboard.service \
  koyura-bot.service koyura-telegram-bot-api.service
sudo systemctl daemon-reload
sudo systemctl enable --now koyura-engine koyura-dashboard koyura-bot
```
