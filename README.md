# Amazon Gmail Notifier for Discord (Raspberry Pi 5)

This script monitors Gmail via IMAP and sends notifications to Discord  
**only for Amazon.co.jp delivery related emails** (Shipping / Out for Delivery / Delivered).

## Features
- Extracts real sender email strictly using regex
- Filters only *@amazon.co.jp*
- Avoids spam, マイナビ転職, リクナビ, Temu, etc
- Runs via systemd as a resident service
- 1-minute interval Gmail check
- Emoji-based status detection:
  - 📦 発送
  - 🚚 配達中
  - 📬 配達済み

## How to Install
(in Japanese or English…書きたい形式で書けばOK)
