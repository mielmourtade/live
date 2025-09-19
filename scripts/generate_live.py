#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Génère le live Chamsin à partir de scripts/news_cache.json et templates/live_template.html

Améliorations clés vs. version initiale :
- Prompt strict avec délimiteurs <!--ITEMS--> / <!--ANALYSIS--> pour un parsing fiable.
- Nettoyage/sanitation HTML (suppression de Markdown, balises dangereuses, styles inline).
- Compression intelligente côté client + regroupement par pays pour réduire les tokens.
- Tolérance aux changements de schéma (country_guess vs country, presence/absence de champs).
- Retries exponentiels et gestion propre des erreurs API.
- Fallback minimal si le modèle renvoie un HTML inattendu (tout passe en “items”).
- Sécurité sur le remplacement dans live.html (marqueurs obligatoires).
"""

import os, json, time, re, html
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

# -------------------- Chemins --------------------
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'scripts' / 'news_cache.json'
TEMPLATE = ROOT / 'templates' / 'live_template.html'
LIVE_HTML = ROOT / 'live.html'

# -------------------- OpenAI API --------------------
OPENAI_BASE = os.getenv('OPENAI_BASE', 'https://api.openai.com/v1')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
OPENAI_KEY = os.getenv('OPENAI_API_KEY', '').strip()

if not OPENAI_KEY:
    raise RuntimeError("OPENAI_API_KEY manquant dans l'environnement.")

# -------------------- Balises & constantes --------------------
REPLACER_START = "<!-- LIVE:START -->"
REPLACER_END   = "<!-- LIVE:END -->"
PARIS_TZ = ZoneInfo('Europe/Paris')

# -------------------- Prompts --------------------
SYSTEM_PROMPT = """Tu es la plume de Chamsin (observatoire indépendant). Style : factuel, concis, rigoureux.

Produit DEUX BLOCS UNIQUEMENT, en HTML pur, strictement encadrés par des délimiteurs :

<!--ITEMS-->
<p><strong>{Pays}</strong> : <em>{JJ/MM}</em> — {1–2 phrases factuelles}</p>
... (autres lignes)
<!--/ITEMS-->

<!--ANALYSIS-->
<h2>Ce que disent les journaux / Notre analyse</h2>
<p>{analyse 1}</p>
<p>{analyse 2}</p>
[optionnel 2 paragraphes supplémentaires]
<!--/ANALYSIS-->

Contraintes :
- Pas d’emoji, pas d’adjectifs forts, pas de liens, pas de noms de médias.
- Conserver les noms propres en français (ex. « Israël/Palestine »).
- Pas de Markdown, pas de ```html ni ``` ; HTML simple seulement.
"""

USER_TEMPLATE = """Date (Europe/Paris) : {date}
Articles (compressés) : {items_json}

Consignes :
- Regrouper par pays selon la liste : Iran, Israël/Palestine, Liban, Syrie, Irak, Jordanie, Yémen, Arabie saoudite, Émirats arabes unis, Qatar, Bahreïn, Koweït, Oman, Égypte, Libye, Tunisie, Algérie, Maroc, Mauritanie, Soudan, Arménie, Azerbaïdjan, Géorgie, Afghanistan, Turquie, Mer Rouge / Maritime, Autres.
- Éviter les doublons ; privilégier les faits nouveaux et datés (JJ/MM).
- 1–2 phrases factuelles par ligne pays ; pas de spéculation ; pas de chiffres non sourcés dans les items.
- L’analyse (2–4 <p>) doit dégager les tendances communes (sécurité, diplomatie, humanitaire, sanctions, énergie)."""

# -------------------- Utilitaires --------------------
def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))

def iso_today_paris(fmt='%d/%m/%Y'):
    return datetime.now(PARIS_TZ).strftime(fmt)

def html_strip_dangerous(s: str) -> str:
    """
    Retire Markdown, balises script/style, éventuels styles/onclick.
    Laisse un HTML très basique.
    """
    if not s:
        return ""
    # Enlever fences Markdown
    s = s.replace("```html", "").replace("```", "")
    # Supprimer balises script/style
    s = re.sub(r"(?is)<\s*(script|style)[^>]*>.*?<\s*/\s*\1\s*>", "", s)
    # Supprimer attributs on* et style
    s = re.sub(r'(?i)\s(on\w+|style)\s*=\s*"[^"]*"', "", s)
    s = re.sub(r"(?i)\s(on\w+|style)\s*=\s*'[^']*'", "", s)
    # Nettoyage espaces
    s = re.sub(r"\s+", " ", s).strip()
    return s

def openai_chat(messages, temperature=0.2, max_retries=6, timeout=60):
    headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
    payload = {"model": OPENAI_MODEL, "messages": messages, "temperature": temperature}
    delay = 2
    for attempt in range(max_retries):
        r = requests.post(f"{OPENAI_BASE}/chat/completions", headers=headers, json=payload, timeout=timeout)
        if r.status_code == 200:
            j = r.json()
            content = j['choices'][0]['message']['content']
            return content
        if r.status_code in (429, 500, 502, 503, 504):
            retry_after = r.headers.get("Retry-After")
            sleep_s = int(retry_after) if retry_after and retry_after.isdigit() else delay
            print(f"[openai_chat] HTTP {r.status_code}, retry in {sleep_s}s… (attempt {attempt+1}/{max_retries})")
            time.sleep(sleep_s)
            delay = min(delay * 2, 60)
            continue
        # autres erreurs : remonter
        try:
            r.raise_for_status()
        finally:
            pass
    r.raise_for_status()

# -------------------- Compression & regroupement --------------------
CANON_COUNTRIES = [
    "Iran", "Israël/Palestine", "Liban", "Syrie", "Irak", "Jordanie", "Yémen",
    "Arabie saoudite", "Émirats arabes unis", "Qatar", "Bahreïn", "Koweït", "Oman",
    "Égypte", "Libye", "Tunisie", "Algérie", "Maroc", "Mauritanie", "Soudan",
    "Arménie", "Azerbaïdjan", "Géorgie", "Afghanistan", "Turquie",
    "Mer Rouge / Maritime", "Autres"
]

def normalize_country(name: str) -> str:
    if not name:
        return "Autres"
    n = name.strip()
    # mappages souples
    aliases = {
        "Israel": "Israël/Palestine",
        "Israël": "Israël/Palestine",
        "Palestine": "Israël/Palestine",
        "West Bank": "Israël/Palestine",
        "Mer Rouge": "Mer Rouge / Maritime",
        "Red Sea": "Mer Rouge / Maritime",
        "Gulf": "Arabie saoudite",  # on évite "Golfe" générique ici ; mieux vaut pays
        "Golfe": "Arabie saoudite",
        "UAE": "Émirats arabes unis",
    }
    n = aliases.get(n, n)
    return n if n in CANON_COUNTRIES else n

def compress_items(data, max_items=40, max_sum=220):
    """
    Réduit la charge token et stabilise le schéma envoyé au LLM.
    Attend des items avec au minimum title/summary et, si possible,
    country_guess (préféré) ou country.
    """
    out = []
    for x in data[:max_items]:
        title = (x.get('title') or '').strip()
        summary = (x.get('summary') or '').strip()
        country = (x.get('country_guess') or x.get('country') or 'Autres').strip()
        theme = (x.get('theme') or '').strip()
        ents = x.get('entities') or []
        date_iso = (x.get('published_at') or '').strip()
        try:
            date_dt = datetime.fromisoformat(date_iso.replace('Z', '+00:00')) if date_iso else None
        except Exception:
            date_dt = None
        date_short = date_dt.astimezone(PARIS_TZ).strftime('%d/%m') if date_dt else ''
        if len(summary) > max_sum:
            summary = summary[:max_sum].rsplit(' ', 1)[0] + '…'
        out.append({
            "title": title,
            "summary": summary,
            "country": normalize_country(country) or "Autres",
            "theme": theme,
            "entities": ents,
            "date": date_short
        })
    return out

def group_by_country(items):
    by_c = {}
    for it in items:
        c = it.get("country") or "Autres"
        by_c.setdefault(c, []).append(it)
    # tri pays selon l’ordre canonique, puis alpha
    ordered = []
    remaining = sorted([c for c in by_c.keys() if c not in CANON_COUNTRIES])
    for c in CANON_COUNTRIES + remaining:
        if c in by_c:
            ordered.append((c, by_c[c]))
    return ordered

# -------------------- Template & injection --------------------
def ensure_template():
    """Crée un template minimal si absent, pour éviter un crash."""
    if TEMPLATE.exists():
        return
    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE.write_text("""<section class="live">
<h1>Chamsin — Live du {{date}}</h1>
<div class="items">
{{items}}
</div>
<div class="analysis">
{{analysis}}
</div>
</section>
""", encoding='utf-8')

def build_html(items_html: str, analysis_html: str) -> str:
    ensure_template()
    today = iso_today_paris('%d/%m/%Y')
    tpl = TEMPLATE.read_text(encoding='utf-8')
    return (tpl.replace('{{date}}', today)
               .replace('{{items}}', items_html.strip())
               .replace('{{analysis}}', analysis_html.strip()))

def inject_into_live(full_block: str):
    if not LIVE_HTML.exists():
        raise RuntimeError(f"{LIVE_HTML} introuvable.")
    content = LIVE_HTML.read_text(encoding='utf-8')
    start = content.find(REPLACER_START)
    end = content.find(REPLACER_END)
    if start == -1 or end == -1 or end < start:
        raise RuntimeError("Marqueurs LIVE:START/END introuvables dans live.html")
    new = content[: start + len(REPLACER_START)] + "\n" + full_block + "\n" + content[end:]
    LIVE_HTML.write_text(new, encoding='utf-8')

# -------------------- Parsing réponse modèle --------------------
def split_items_analysis(html_text: str):
    """
    Sépare proprement via les délimiteurs requis.
    Fallback : tout bascule dans items si les balises manquent.
    """
    txt = html_strip_dangerous(html_text)
    # tolérer petites variations d'espaces/casse
    def _find(tag):
        m = re.search(rf"<!--\s*{tag}\s*-->", txt, flags=re.I)
        return m.start() if m else -1

    s_items = _find("ITEMS")
    e_items = _find("/ITEMS")
    s_ana   = _find("ANALYSIS")
    e_ana   = _find("/ANALYSIS")

    if s_items != -1 and e_items != -1:
        items_html = txt[s_items:e_items]
        # enlever les commentaires eux-mêmes
        items_html = re.sub(r"<!--\s*ITEMS\s*-->\s*", "", items_html, flags=re.I).strip()
    else:
        items_html = txt

    if s_ana != -1 and e_ana != -1:
        analysis_html = txt[s_ana:e_ana]
        analysis_html = re.sub(r"<!--\s*ANALYSIS\s*-->\s*", "", analysis_html, flags=re.I).strip()
    else:
        # si pas d'analyse, chaîne vide
        analysis_html = ""

    # seconde sanitation (au cas où)
    return html_strip_dangerous(items_html), html_strip_dangerous(analysis_html)

# -------------------- Programme principal --------------------
def main():
    if not CACHE.exists():
        raise RuntimeError(f"{CACHE} introuvable. Lance d'abord fetch_news.py")

    raw = read_json(CACHE)

    # compression + regroupement
    max_items = int(os.getenv('MAX_ITEMS', '40'))
    max_sum = int(os.getenv('MAX_SUM', '220'))
    compact = compress_items(raw, max_items=max_items, max_sum=max_sum)

    # Option : regrouper par pays côté user prompt pour donner un signal fort
    grouped = group_by_country(compact)
    # On renvoie une structure compacte au modèle (moins de tokens que du vrac)
    items_for_llm = [{"country": c, "items": v} for c, v in grouped]

    date_str = datetime.now(PARIS_TZ).strftime('%d/%m')
    user = USER_TEMPLATE.format(date=date_str, items_json=json.dumps(items_for_llm, ensure_ascii=False))

    content = openai_chat(
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": user}],
        temperature=0.2,
        max_retries=6,
        timeout=90
    )

    items_html, analysis_html = split_items_analysis(content)
    full_html = build_html(items_html, analysis_html)
    inject_into_live(full_html)
    print("live.html mis à jour.")

if __name__ == '__main__':
    main()
