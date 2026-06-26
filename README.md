# IPO Cross Tracker — Backend (scraper API)

Yeh chhota server **Mainboard + SME** recently-listed IPOs ki list Chittorgarh se
scrape karta hai, aur har stock ka **listing / lowest / current price** Yahoo Finance
se nikaal ke saaf JSON deta hai. Website (frontend) isi server se data leti hai.

---

## A. Pehle local pe test (optional, par recommend)

> Sirf pure-Python packages hain (flask, requests, gunicorn) — koi C++ build tool
> ya Visual Studio nahi chahiye. Kisi bhi Python 3.9+ version pe chalega.

```bash
pip install -r requirements.txt
python app.py
```
Browser me kholo:
- http://localhost:5000/health  → `{"ok": true}`
- http://localhost:5000/ipos?type=all  → IPO list (mainboard + sme)
- http://localhost:5000/quote?symbol=RELIANCE → price data

Agar `/ipos` khaali aaye → "Troubleshooting" section dekho.

---

## B. Render pe free deploy (~10 min)

1. **GitHub** pe ek naya repo banao, in 4 files ko usme daal do:
   `app.py`, `requirements.txt`, `render.yaml`, `README.md`.
2. [render.com](https://render.com) pe **GitHub se sign up** karo.
3. Dashboard → **New + → Blueprint** chuno, apna repo select karo.
   - `render.yaml` apne aap detect ho jaayega (web service, free plan, Singapore).
   - Ya manually: **New + → Web Service** → repo → Build: `pip install -r requirements.txt`,
     Start: `gunicorn app:app`, Instance: **Free**.
4. **Create** dabao. 3-5 min me deploy. Upar ek URL milega jaise
   `https://ipo-cross-tracker-api.onrender.com` — **ise copy karo.**
5. Test: `https://aapka-url.onrender.com/ipos?type=all` JSON dikhe → ho gaya.
6. Yahi URL website ke **Settings → API URL** me paste karna hai.

> ⚠️ Free plan: 15 min tak koi request na aaye to server "so" jaata hai; agli baar
> kholne pe pehli request ~1 min slow (cold start). Personal use me theek hai.

---

## C. Endpoints

| Endpoint | Kaam |
|---|---|
| `GET /health` | server zinda hai ya nahi |
| `GET /ipos?type=all` | mainboard + sme list (`mainboard` / `sme` bhi de sakte ho) |
| `GET /quote?symbol=XYZ` | us symbol ka `current`, `lowest`, `listing` price |

`/ipos` har IPO ka `name`, `type`, `issuePrice`, `listingDate` deta hai. NSE **symbol**
reliably nahi milta — website me aap har row ka symbol bhar/correct kar sakte ho, tab
price auto-fetch hoga.

---

## D. Troubleshooting (scraper toot jaaye to)

Chittorgarh page badalne pe scraping fail ho sakti hai. Theek karne ke 3 jagah:

1. **URL galat / page hata** → `app.py` me `SOURCES` ke andar mainboard/sme URL
   update karo. Chittorgarh pe "Mainboard IPO list" aur "SME IPO list" report page
   dhoondo aur naya URL daal do.
2. **Columns match nahi ho rahe** → `scrape_chittorgarh()` me `_pick_col(...)` ke
   needles (`"issue price"`, `"company"`, `"listing"`) ko us page ke actual column
   naamon se milao.
3. **403 / block** → `HEADERS` ka User-Agent badal ke dekho, ya requests ke beech
   thoda gap rakho. Cache (`_TTL`) already har 30 min me ek baar hi scrape karta hai.

Yaad rahe: website **manual mode** me bina backend ke bhi chalti hai — aap khud IPO
add kar sakte ho. Backend sirf auto-fill ke liye hai, to scraper toote tab bhi tool
band nahi padta.

---

## E. Legal / accuracy note

Data publicly accessible sources (Chittorgarh, Yahoo) se aggregate hota hai aur sirf
**informational** hai — koi guarantee nahi ki accurate/complete/real-time ho.
Investment decisions se pehle official NSE/BSE sources se verify karo. Yeh tool koi
investment advice nahi deta.
