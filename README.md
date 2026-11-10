# Instagram → Telegram Notifier (v2 - safety-focused rewrite)

Naye posts, reels aur stories (media ke saath) seedha Telegram par — 24/7, VPS par,
account lock ke risk ko kam karne ke liye redesign kiya gaya.

## Kyun rewrite hua

Pehla version 29h chalne ke baad Instagram ne lock kar diya ("Try another device to
continue"). Do saaf wajah mili:

1. **Fresh login har restart pe ek NAYE random device fingerprint se** (jab
   `IG_SESSIONID` use hota tha, `session.json` load hi nahi hota tha) - session cookie
   ek device ka, requests kisi aur (random) device ke naam se — yeh mismatch
   Instagram ki abuse-detection ko seedha flag karta hai.
2. **Continuous, back-to-back scanning, koi rest nahi** - `FULL_SCAN_SECONDS=60` tha,
   lekin 76 accounts ko scan karne mein khud hi 50-60s lag jaate the, to practically
   bot 24/7 non-stop requests bhej raha tha, kabhi rukta nahi tha.

Dono fix ho gaye hain is version mein — niche "Kya badla" section mein detail hai.

## Setup (VPS)

```bash
git clone <your-repo> /opt/insta-tg-bot && cd /opt/insta-tg-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
```

### Pehle session.json apne LAPTOP/HOME se banao - VPS par NAHI

Yeh sabse important step hai. VPS (datacenter IP) se *fresh login* karna hi woh cheez
hai jo 429 / `ClientNeedsUpgradeError` degi. Apne laptop/phone ke network se:

```bash
cp .env.example .env && nano .env     # IG_USERNAME/IG_PASSWORD ya IG_SESSIONID bharo
python bot.py --login
```

2FA code maangega to terminal mein daal do (ya `IG_TOTP_SECRET` set karo for
auto-code). Done hone par `session.json` current directory mein ban jayega:

```bash
scp session.json user@your-vps:/opt/insta-tg-bot/session.json
```

VPS par bot ab se **hamesha yeh session.json resume karega**, dobara login nahi
karega khud se — agar session.json missing/dead mile to bot CRASH ho jayega with a
clear error, VPS se khud login try nahi karega (jaanbhujkar, taaki wahi purani
galti na dohraye).

```bash
cp igbot.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now igbot
journalctl -u igbot -f
```

### Agar session expire/lock ho jaye dobara

1. Apne USI purane device/network se Instagram app/website khol kar checkpoint
   complete karo (bot khud kuch retry nahi karega jab `challenge_required` aaye -
   woh owner ko Telegram par alert bhejega aur khud ko PAUSE kar dega).
2. Laptop/home se dobara `python bot.py --login` chalao.
3. Naya `session.json` VPS par copy karo, `/resume` bhejo ya service restart karo.

## Safety knobs (.env)

| Var | Kya karta hai |
|---|---|
| `FULL_SCAN_SECONDS` | Saare (non-priority) accounts ek baar cycle karne ka target time - 76 accounts ke liye 300s (~5 min) default hai, jo brief ke "5-6 min delay" ke andar hai. |
| `SCAN_GAP_MIN`/`MAX` | Har account ke baad random itna second rukta hai - continuous scanning rok kar human-jaisa pattern banata hai. |
| `STORY_EVERY` | Stories sirf har Nth round check hoti hain (posts har round check hoti hain) - request volume lagbhag half/third ho jaata hai. |
| `QUIET_HOURS` | Optional daily rest window ("02:00-07:00") - is window mein koi Instagram call nahi jaati, bilkul 24/7 pattern se bachne ke liye. |
| `FAST_PATH` | OFF by default - home-feed + reels-tray (2 calls, sab accounts cover) - yeh session kill kar chuka hai pehle, isliye disabled rakha hai jab tak carefully kisi throwaway account par test na ho. |

## Proxy / VPS IP badalne par

`IG_PROXY=http://user:pass@host:port` set karo to Instagram-bound traffic us proxy
se jayega (residential/static proxy recommended - startup log batayega proxy ON hai
ya nahi). Abhi budget nahi hai to blank chhod do.

VPS seller har mahine NAYA VPS (nayi IP) deta hai. IP change hone par:

- `session.json` ko DOBARA mat banao - usi purane ko reuse karo (device fingerprint
  wahi rehna chahiye, sirf network IP badla hai, jo normal hai - log in/out har
  mahine Instagram ko zyada suspicious lagega).
- Naye VPS par pehla kaam halka rakho (bot start karo, 1-2 accounts check hone do),
  phir normal scanning chalne do - ekdum se bursty traffic IP-change ke saath milke
  zyada flag-worthy lagta hai.
- Agar proxy use karte ho, to proxy ka IP VPS badalne se nahi badalta - IG_PROXY
  same rakho, kuch karne ki zaroorat nahi.

## Remote control from mhajk_.py

Yeh bot do chhoti shared JSON files ke through doosre bot (jaise mhajk_.py) se
control ho sakta hai - koi live API/socket nahi, sirf files (dono bots same VPS
ya same shared directory dekh sakte hain to kaam karega; alag machines par ho to
beech mein rsync/cron jaisa kuch sync chahiye hoga):

- **`ig_commands.json`** (mhajk_.py likhta hai, yeh bot padhta hai): list of
  `{"id", "cmd", "args", "ts"}` - `cmd` in `add/remove/priority/unpriority/pause/
  resume/import` mein se ek. Yeh bot har `IG_CMD_POLL_SECONDS` (default 5s) mein
  file check karta hai, command apply karta hai (wahi functions jo /add /remove
  Telegram commands khud use karte hain), phir queue clear kar deta hai.
- **`ig_data.json`** (yeh bot likhta hai, mhajk_.py padh sakta hai): read-only
  mirror - `paused`, `accounts`, `ready`, `priority`, `users` ka summary, `updated_at`.

## Telegram commands (sirf OWNER_ID)

- `/add user1 user2` – pages add karo (username ya link)
- `/remove user` – hatao
- `/list` – watch list
- `/import` – tumhare IG account ke sabhi followed accounts add
- `/pause`, `/resume`, `/status`
- `/priority user` / `/unpriority user` – zyada baar check (har `PRIO_SECONDS`)

## Notes

- Naya account add hone par pehle round me purana content sirf "seen" mark hota hai,
  spam nahi aata.
- Instagram unofficial API use hota hai - humesha ek risk rehta hai. Main account ki
  jagah ek separate, aged, 2FA-wala secondary account use karo - MAIN account kabhi
  nahi.
- Caption format aur video-cover-as-first-frame trick bilkul pehle jaisa hai -
  kuch nahi badla us part mein.
