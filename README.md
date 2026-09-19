# Telegram Content Lock Bot — Heroku

A Telegram bot that locks content until the user joins all configured channels/groups.

## Features

- Text/photo/video/document/audio/voice and other copyable Telegram messages
- Unlimited required channels/groups
- Join buttons
- Membership verification
- Check & Unlock
- Deep links for each content item
- Admin commands
- PostgreSQL database
- Heroku worker deployment

## 1. Create the Telegram bot

Use @BotFather and create a bot. Copy the bot token.

## 2. Find your Telegram numeric user ID

Use a trusted Telegram ID bot or another method to obtain your numeric Telegram user ID.

Put it into `ADMIN_ID`.

## 3. Add the bot as administrator

For every channel/group that users must join:

1. Add the bot.
2. Give it administrator privileges.
3. Keep the bot in that chat.

This is required for reliable membership checks of other users.

## 4. Heroku database

Create a Heroku Postgres database for the app. Heroku will provide the `DATABASE_URL` config variable.

Do NOT rely on SQLite for production on Heroku because the dyno filesystem is ephemeral.

## 5. Set config variables

Set:

BOT_TOKEN
ADMIN_ID
DATABASE_URL

Do not commit `.env` to Git.

## 6. Deploy

The repository contains:

Procfile:
worker: python bot.py

After deployment, scale the worker to 1.

Then inspect logs:

heroku logs --tail

## 7. Configure required chats

Message the bot as admin:

/admin

For a public channel:

/addchat @YourChannel | https://t.me/YourChannel

For a private channel/group:

/addchat -1001234567890 | https://t.me/+YOUR_INVITE_LINK

Then:

/listchats

## 8. Create locked content

Send:

/content

Then immediately send or forward ONE content message.

The bot returns a link similar to:

https://t.me/YourBot?start=CABC12345

Share that link.

## Important

For V1, each locked content item is one Telegram message. If you want a package containing multiple messages/media items, add a multi-message content wizard in V2.

## Security

Never put BOT_TOKEN directly in source code or GitHub.
