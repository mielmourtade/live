#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, requests
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

SYSTEM_PROMPT = """
Tu es la plume de Chamsin (observatoire indépendant). Style : factuel, concis, rigoureux.
Produit deux blocs :
1) LIGNES PAR PAYS (HTML) — Chaque ligne est un <p> au format :
   <p><strong>{Pays}</strong> : <em>{JJ/MM}</em> — {1–2 phrases factuelles}
2) ANALYSE (HTML) — 2–4 <p> sous le titre "Ce que disent les journaux / Notre analyse".
Contraintes :
- Pas d’emoji, pas d’adjectifs forts, pas de source nommée.
- Conserve les noms propres FR usuels.
"""

USER_TEMPLATE = """
Date (Europe/Paris) : {date}
Articles (JSON) : {items}
Consignes :
- Regrouper par pays (Iran, Israël/Palestine, Liban, Syrie, Irak, Yémen, Golfe, Caucase, Autres).
- Évite les doublons.
"""

REPLACER_START = "<!-- LIVE:START -->"
REPLACER_END   = "<!-- LIVE:END -->"

def openai_chat(messages, temperature=0.2):
    headers = {
        "Authorization": f"Bearer {OPENAI_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": OPENAI_MODEL,
        "messages": messages,
        "temperature": temperature
    }
    r = requests.post(f"{OPENAI_BASE}/chat/completions", headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']

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

if __name__ == '__main__':
    data = json.loads(CACHE.read_text(encoding='utf-8'))
    paris = ZoneInfo('Europe/Paris')
    date_str = datetime.now(paris).strftime('%d/%m')

    user = USER_TEMPLATE.format(date=date_str, items=json.dumps(data, ensure_ascii=False))

    content = openai_chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user}
    ])

    # Séparer items et analyse
    if "<h2" in content:
        items_html = content.split("<h2")[0].strip()
        analysis_html = content.split("</h2>")[-1].strip()
    else:
        items_html = content.strip()
        analysis_html = ""

    full_html = build_html(items_html, analysis_html)
    inject_into_live(full_html)
    print("live.html mis à jour.")
