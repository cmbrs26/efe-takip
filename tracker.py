"""WinningCircle Canlı Cüzdan takipçisi: Efe Bulduk'un işlem açılış/kapanışlarını Telegram'a bildirir."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import requests

URL = "https://www.winningcircle.io/panel/canlicuzdan"
ACCOUNT = "Efe Bulduk"
STATE_FILE = Path("state.json")
MAX_PENDING = 6      # açık listeden kayboldu ama geçmişe düşmediyse kaç çalıştırma beklenir
MAX_FAILS = 3        # art arda kaç hatadan sonra Telegram'dan uyarı gelir
KEEP_HIST = 500

TICKER = r"[A-Z0-9]+-[A-Z0-9]+"
MONEY = r"\$[\d.,]+"
SIGNED = r"[+-]?\$[\d.,]+"
PCT = r"[+-]?[\d.,]+%"

OPEN_RE = re.compile(
    rf"TICKER ({TICKER}) \S+ Yön (Long|Short) Giriş ({MONEY}) Çıkış \S+ "
    rf"Bakiye ({MONEY}) PnL ({SIGNED}) ({PCT})"
)
HIST_RE = re.compile(
    rf"({TICKER}) \S+ (Long|Short) {re.escape(ACCOUNT)} ({MONEY}) ({MONEY}) ({MONEY}) "
    rf"({SIGNED}) ({PCT}) (\d{{2}}\.\d{{2}}\.\d{{4}} \d{{2}}:\d{{2}})"
)


def norm(text):
    return re.sub(r"\s+", " ", text).strip()


def parse(text):
    """Dönen: (açık_işlemler, geçmiş_satırları) ya da sayfa okunamadıysa None."""
    t = norm(text)
    start = t.find(f"{ACCOUNT} Toplam değer")
    hist_at = t.find("Geçmiş işlemler", start if start >= 0 else 0)
    if start < 0 or hist_at < 0:
        return None
    seg = t[start:hist_at]
    nxt = seg.find("Toplam değer", len(ACCOUNT) + 14)  # sonraki hesap (ör. Oslo Bot) başlıyorsa kes
    if nxt > 0:
        seg = seg[:nxt]

    opens = {}
    for tk, side, entry, bal, pnl, pct in OPEN_RE.findall(seg):
        opens[f"{tk}|{side}|{entry}"] = dict(ticker=tk, side=side, entry=entry, balance=bal, pnl=pnl, pct=pct)
    if not opens and "açık işlem yok" not in seg:
        return None  # ne işlem ne "açık işlem yok" yazısı var: sayfa beklenmedik, güvenli tarafta kal

    hist = []
    for tk, side, entry, exit_, bal, pnl, pct, date in HIST_RE.findall(t[hist_at:]):
        hist.append(dict(id=f"{tk}|{side}|{entry}|{date}", key=f"{tk}|{side}|{entry}", ticker=tk,
                         side=side, entry=entry, exit=exit_, balance=bal, pnl=pnl, pct=pct, date=date))
    return opens, hist


def html_to_text(html):
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"<[^>]+>", " ", html)


def render():
    from playwright.sync_api import sync_playwright

    def run():
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(URL, wait_until="networkidle", timeout=60000)
            page.wait_for_selector("text=Geçmiş işlemler", timeout=30000)
            txt = page.inner_text("body")
            browser.close()
            return txt

    try:
        return run()
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
        subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=True)
        return run()


def fetch():
    try:
        r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        if r.ok:
            parsed = parse(html_to_text(r.text))
            if parsed is not None:
                return parsed
    except requests.RequestException:
        pass
    return parse(render())  # sayfa JS ile yükleniyorsa gerçek tarayıcı


def send(msg):
    token, chat = os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": msg}, timeout=30)
    r.raise_for_status()


def fmt_open(o):
    return (f"🟢 Yeni işlem açıldı — {ACCOUNT}\n{o['ticker']} · {o['side']}\n"
            f"Giriş: {o['entry']}\nBakiye: {o['balance']}")


def fmt_closed(h):
    return (f"🔴 İşlem kapandı — {ACCOUNT}\n{h['ticker']} · {h['side']}\n"
            f"Giriş: {h['entry']} → Çıkış: {h['exit']}\nPnL: {h['pnl']} ({h['pct']})\nTarih: {h['date']}")


def step(state, opens, hist, notify):
    """Mantığın tamamı burada; state'i günceller. notify(msg) bildirim gönderir."""
    if state is None:  # ilk çalıştırma: sadece mevcut durumu kaydet
        lines = [f"• {o['ticker']} {o['side']} (giriş {o['entry']}, PnL {o['pnl']})" for o in opens.values()]
        notify(f"✅ Takip başladı. Şu an {len(opens)} açık işlem var.\n" + ("\n".join(lines) if lines else ""))
        return dict(open=opens, pending={}, hist=[h["id"] for h in hist][:KEEP_HIST], fails=0)

    seen = list(state.get("hist", []))
    pending = dict(state.get("pending", {}))
    prev_open = state.get("open", {})
    hist_by_key = {}
    for h in hist:
        hist_by_key.setdefault(h["key"], h)

    # 1) Yeni açılanlar
    for key, o in opens.items():
        if key not in prev_open and key not in pending:
            notify(fmt_open(o))
        pending.pop(key, None)  # geri döndüyse bekleyenlerden çıkar

    # 2) Geçmişte yeni görünen satırlar = kapanış
    for h in hist:
        if h["id"] not in seen:
            notify(fmt_closed(h))
            seen.append(h["id"])

    # 3) Açık listeden kaybolanlar: geçmişte karşılığı varsa zaten bildirildi, yoksa bekle
    for key, o in prev_open.items():
        if key in opens:
            continue
        if key in hist_by_key:
            pending.pop(key, None)
            continue
        n = pending.get(key, {}).get("missing", 0) + 1
        if n >= MAX_PENDING:
            notify(f"🔴 İşlem açık listeden kalktı (geçmişte henüz görünmüyor) — {o['ticker']} · {o['side']}, giriş {o['entry']}")
            pending.pop(key, None)
        else:
            pending[key] = dict(o, missing=n)

    # bekleyen işlemleri bir sonraki tur için "açık" gibi hatırla
    carry = {k: {kk: vv for kk, vv in v.items() if kk != "missing"} for k, v in pending.items()}
    return dict(open={**opens, **carry}, pending=pending, hist=seen[-KEEP_HIST:], fails=0)


def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else None
    try:
        parsed = fetch()
        if parsed is None:
            raise RuntimeError("Sayfa beklenen biçimde okunamadı")
    except Exception as e:
        fails = (state or {}).get("fails", 0) + 1
        print("HATA:", e)
        if state is not None:
            state["fails"] = fails
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            if fails == MAX_FAILS:
                send(f"⚠️ Takipçi {MAX_FAILS} kez üst üste sayfayı okuyamadı: {e}")
        sys.exit(0)  # bildirimi biz yönetiyoruz, GitHub'dan hata maili yağmasın
    opens, hist = parsed
    new_state = step(state, opens, hist, send)
    STATE_FILE.write_text(json.dumps(new_state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
