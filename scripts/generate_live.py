#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, json, html, textwrap
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'scripts' / 'news_cache.json'
TEMPLATE = ROOT / 'templates' / 'live_template.html'
LIVE_HTML = ROOT / 'live.html'

OPENAI_BASE = os.getenv('OPENAI_BASE', 'https://api.openai.com/v1')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_KEY = os.environ['OPENAI_API_KEY']

SYSTEM_PROMPT = """
Tu es la plume de Chamsin (observatoire indépendant). Style : factuel, concis, sans sensationnalisme, rigoureux.
Produit deux blocs :
1) LIGNES PAR PAYS (HTML) — Chaque ligne est un paragraphe <p> au format :
<p><strong>{Pays}</strong> : <em>{JJ/MM}</em> — {1–2 phrases factuelles.}
Inclure seulement les pays/sujets pertinents. Évite répétitions. 1–3 lignes max par pays.
2) ANALYSE (HTML) — Sous un <h2> déjà présent « Ce que disent les journaux / Notre analyse », écris 2–4 <p> courts :
- Ce que disent les journaux : consensus/nouveautés/sources dominantes
- Notre analyse : mise en perspective régionale, impacts, signaux faibles
Contraintes :
- Pas d’emoji, pas d’adjectifs forts. Cite aucun média nommément. Pas de lien.
- Conserve les noms propres et orthonymes FR usuels.
"""

USER_TEMPLATE = """
Date (Europe/Paris) : {date}
Articles (JSON) : {items}
Consignes additionnelles :
- Regrouper par : Iran; Israël/Palestine; Liban; Syrie; Irak; Yémen; Golfe; Caucase; Autres (si nécessaire)
- Évite de dupliquer des infos proches. Privilégie ce qui est nouveau et vérifiable.
- Chaque paragraphe <p> doit être sur une seule ligne.
"""

REPLACER_START = "<!-- LIVE:START -->"
REPLACER_END = "<!-- LIVE:END -->"

def openai_chat(messages, temperature=0.2):
headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
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
print('live.html mis à jour.')
