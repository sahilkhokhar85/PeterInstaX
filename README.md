# Instagram → Telegram Notifier

Naye posts, reels aur stories (media ke saath) seedha Telegram par.

## Setup (VPS)

```bash
git clone <your-repo> /opt/insta-tg-bot && cd /opt/insta-tg-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
```

**Pehli baar foreground me chalao** (agar Instagram email/SMS code maange to terminal me daal sakoge):

```bash
./venv/bin/python bot.py
```

Login ho jaye (session.json ban jayega) to Ctrl+C karke service chalu karo:

```bash
cp igbot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now igbot
journalctl -u igbot -f
```

## Telegram commands (sirf OWNER_ID)

- `/add user1 user2` – pages add karo (username ya link)
- `/remove user` – hatao
- `/list` – watch list
- `/import` – tumhare IG account ke sabhi followed accounts add
- `/pause`, `/resume`, `/status`

## Notes

- Naya account add hone par pehle round me purana content sirf "seen" mark hota hai, spam nahi aata.
- Instagram unofficial API use hota hai. Bohot zyada accounts + kam POLL_SECONDS = ban/rate-limit ka risk. 100+ accounts ho to POLL_SECONDS 90-120 rakho, aur main account ki jagah secondary account se login karna safe rehta hai.
- VPS (datacenter) IP par Instagram jaldi challenge maangta hai; residential proxy `IG_PROXY` me lagao to behtar.
