#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, time, requests
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'scripts' / 'news_cache.json'
TEMPLATE = ROOT / 'templates' / 'live_template.html'
LIVE_HTML = ROOT / 'live.html'

OPENAI_BASE = os.getenv('OPENAI_BASE', 'https://api.openai.com/v1')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_KEY = os.environ['OPENAI_API_KEY']

# --- Nettoyage de la sortie du LLM (supprime les blocs Markdown) ---
def clean_output(s: str) -> str:
    """Supprime les blocs markdown ```html ou ``` éventuels"""
    return s.replace("```html", "").replace("```", "").strip()

SYSTEM_PROMPT = """
Tu es la plume de Chamsin (observatoire indépendant). Style : factuel, concis, rigoureux.
Produit deux blocs :
1) LIGNES PAR PAYS (HTML) — Chaque ligne est un <p> au format :
   <p><strong>{Pays}</strong> : <em>{JJ/MM}</em> — {1–2 phrases factuelles}
2) ANALYSE (HTML) — 2–4 <p> sous le titre "Ce que disent les journaux / Notre analyse".
Contraintes :
- Pas d’emoji, pas d’adjectifs forts, pas de source nommée ni de liens.
- Conserve les noms propres FR usuels.
- Réponds uniquement en HTML pur, sans blocs Markdown, sans ```html ni ```.
"""

USER_TEMPLATE = """
Date (Europe/Paris) : {date}
Articles (JSON, déjà compressés) : {items}
Consignes :
- Regrouper par pays (Iran, Israël/Palestine, Liban, Syrie, Irak, Jordanie, Yémen, Arabie saoudite, Émirats arabes unis, Qatar, Bahreïn, Koweït, Oman, Égypte, Libye, Tunisie, Algérie, Maroc, Mauritanie, Soudan, Arménie, Azerbaïdjan, Géorgie, Afghanistan, Turquie).
- Évite les doublons, privilégie le neuf.
"""

REPLACER_START = "<!-- LIVE:START -->"
REPLACER_END   = "<!-- LIVE:END -->"

def openai_chat(messages, temperature=0.2, max_retries=6):
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": temperature}
    delay = 2
    for attempt in range(max_retries):
        r = requests.post(f"{OPENAI_BASE}/chat/completions", headers=headers, json=payload, timeout=60)
        if r.status_code == 200:
            j = r.json()
            return j['choices'][0]['message']['content']
        # 429/5xx → retry exponentiel
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            sleep_s = int(retry_after) if retry_after and retry_after.isdigit() else delay
            print(f"[openai_chat] HTTP {r.status_code}, retry in {sleep_s}s… (attempt {attempt+1}/{max_retries})")
            time.sleep(sleep_s)
            delay = min(delay * 2, 60)
            continue
        r.raise_for_status()
    r.raise_for_status()

def build_html(items_html, analysis_html):
    paris = ZoneInfo('Europe/Paris')
    today = datetime.now(paris).strftime('%d/%m/%Y')
    tpl = TEMPLATE.read_text(encoding='utf-8')
    return tpl.replace('{{date}}', today) \
              .replace('{{items}}', items_html.strip()) \
              .replace('{{analysis}}', analysis_html.strip())

def inject_into_live(full_block):
    content = LIVE_HTML.read_text(encoding='utf-8')
    start = content.find(REPLACER_START)
    end = content.find(REPLACER_END)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Marqueurs LIVE:START/END introuvables")
    new = content[: start + len(REPLACER_START)] + "\n" + full_block + "\n" + content[end:]
    LIVE_HTML.write_text(new, encoding='utf-8')

def compress_items(data, max_items=40, max_sum=220):
    """Réduit la charge token : garde max 40 items, ne conserve que le minimum utile."""
    out = []
    for x in data[:max_items]:
        title = (x.get('title') or '').strip()
        summary = (x.get('summary') or '').strip()
        country = (x.get('country') or 'Autres').strip()
        if len(summary) > max_sum:
            summary = summary[:max_sum].rsplit(' ', 1)[0] + '…'
        out.append({"title": title, "summary": summary, "country": country})
    return out

if __name__ == '__main__':
    # Charge et compresse
    raw = json.loads(CACHE.read_text(encoding='utf-8'))
    items = compress_items(raw, max_items=int(os.getenv('MAX_ITEMS', '40')), max_sum=220)

    paris = ZoneInfo('Europe/Paris')
    date_str = datetime.now(paris).strftime('%d/%m')

    user = USER_TEMPLATE.format(date=date_str, items=json.dumps(items, ensure_ascii=False))
    content = openai_chat(
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user}],
        temperature=0.2,
        max_retries=6
    )
    # --- Nettoyage ICI ---
    content = clean_output(content)

    # Séparer items et analyse
    if "<h2" in content:
        items_html = content.split("<h2")[0].strip()
        analysis_html = content.split("</h2>")[-1].strip()
    else:
        items_html, analysis_html = content.strip(), ""

    full_html = build_html(items_html, analysis_html)
    inject_into_live(full_html)
    print("live.html mis à jour.")
