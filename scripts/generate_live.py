#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fetch/score/compress news for Chamsin (niveau "doctorant-proof")

- Lit config/feeds.yml (voir structure proposée précédemment)
- Récupère les flux en parallèle
- Nettoie/normalise (titre, résumé, URL canonique)
- Filtre par langue et block_keywords
- Tag pays (heuristique élargie ME/CAU)
- Score (fraîcheur * priorité source + boosts sémantiques)
- Déduplique (title+url+canonical) dans une fenêtre de 48h par défaut
- Compresse vers un target N en maximisant la diversité (pays/thème/source)

Dépendances:
  pip install feedparser python-dateutil langdetect html5lib
"""
from __future__ import annotations
import json, re, hashlib, html, logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple
import feedparser
from dateutil import parser as dparser
from langdetect import detect, DetectorFactory

# -------------------- Chemins / Constantes --------------------
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'feeds.yml'
OUT_JSON = ROOT / 'scripts' / 'news_cache.json'

# Langdetect rendu déterministe
DetectorFactory.seed = 42

# -------------------- Logging propre --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("chamsin.fetch")

# -------------------- Utilitaires --------------------
def norm_text(s: str | None) -> str:
    s = (s or "").strip()
    s = html.unescape(s)
    # Supprimer balises <.*?> grossières si un résumé HTML arrive
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s

def safe_detect(text: str, default: str = "en") -> str:
    try:
        return detect(text) if text else default
    except Exception:
        return default

def parse_date(entry: Any) -> datetime | None:
    # 1) published
    for fld in ("published", "updated", "created"):
        val = getattr(entry, fld, None) or entry.get(fld) if isinstance(entry, dict) else None
        if val:
            try:
                dt = dparser.parse(str(val))
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    # 2) *_parsed fournis par feedparser
    for fld in ("published_parsed", "updated_parsed"):
        val = getattr(entry, fld, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def canonical_url(link: str) -> str:
    # Normalise quelques querystrings fréquents de tracking
    if not link:
        return link
    # enlever UTM/tracking
    link = re.sub(r"([?&])utm_[^=&]+=[^&]+", r"\1", link, flags=re.I)
    link = re.sub(r"([?&])(fbclid|gclid|mc_cid|mc_eid|oref|guce_referrer|guce_referrer_sig)=[^&]+", r"\1", link, flags=re.I)
    link = re.sub(r"[?&]$", "", link)
    return link

def hash_key(url: str, title: str) -> str:
    return hashlib.md5(f"{url}|{title}".encode("utf-8")).hexdigest()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

# -------------------- Pays & Thèmes --------------------
COUNTRY_TAGS: Dict[str, List[str]] = {
    'Iran': ['iran', 'tehran', 'isfahan', 'qom', 'iri', 'irgc', 'pasdaran'],
    'Israël/Palestine': ['israel', 'israeli', 'idf', 'gaza', 'rafah', 'hamas', 'west bank', 'cisjord', 'jerusalem', 'idf'],
    'Liban': ['lebanon', 'lebanese', 'hezbollah', 'beirut', 'south lebanon', 'margaliot'],
    'Syrie': ['syria', 'syrian', 'damascus', 'aleppo', 'idlib', 'daraa', 'deir ez-zor'],
    'Irak': ['iraq', 'iraqi', 'baghdad', 'erbil', 'kurdistan', 'pmf', 'hashd'],
    'Yémen': ['yemen', 'houthi', 'ansar allah', 'sanaa', 'hudaydah', 'aden'],
    'Golfe': ['saudi', 'ksa', 'riyadh', 'emirati', 'uae', 'abudhabi', 'qatar', 'doha', 'bahrain', 'oman', 'muscat', 'kuwait'],
    'Caucase': ['armenia', 'armenian', 'yerevan', 'azerbaijan', 'azerbaijani', 'baku', 'nagorno', 'karabakh', 'artsakh', 'nakhchivan', 'zangezur', 'georgia', 'tbilisi', 'abkhazia', 'south ossetia', 'ossetia'],
    'Égypte/Jordanie': ['egypt', 'cairo', 'sinai', 'jordan', 'amman', 'aqaba'],
    'Turquie': ['turkey', 'türkiye', 'ankara', 'istanbul', 'pkk', 'sdf'],
    'Mer Rouge / Maritime': ['red sea', 'mer rouge', 'bab al-mandeb', 'tanker', 'suez', 'hijack', 'ais']
}

# Thèmes heuristiques simples (armement, diplomatie, humanitaire, économie)
THEME_TAGS: Dict[str, List[str]] = {
    'Nucléaire': ['iaea', 'aiea', 'jcpoa', 'centrifuge', 'enrichment', 'ir-'],
    'Sécurité/Conflit': ['strike', 'airstrike', 'rocket', 'missile', 'drone', 'uav', 'shell', 'incursion', 'clash'],
    'Diplomatie/Sanctions': ['sanction', 'designation', 'talks', 'negotiation', 'normalis', 'e3', 'eu', 'ofac', 'ofsi', 'eeas'],
    'Humanitaire': ['ocha', 'relief', 'displaced', 'casualties', 'aid', 'famine', 'hostage', 'prisoner exchange'],
    'Maritime/Énergie': ['tanker', 'pipeline', 'opec', 'gas field', 'lng', 'ais', 'strait', 'shipping'],
    'Politique intérieure': ['cabinet', 'coalition', 'knesset', 'election', 'parliament', 'minister', 'dissolution']
}

def tag_country(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    for country, keys in COUNTRY_TAGS.items():
        if any(k in text for k in keys):
            return country
    return 'Autres'

def tag_theme(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    for theme, keys in THEME_TAGS.items():
        if any(k in text for k in keys):
            return theme
    return 'Général'

# -------------------- Config --------------------
def load_cfg() -> Dict[str, Any]:
    import yaml
    with open(CONFIG, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}
    # Valeurs par défaut robustes
    cfg.setdefault('feeds', [])
    cfg.setdefault('per_feed_max', 20)
    cfg.setdefault('overall_max', 120)
    cfg.setdefault('language_allowlist', ['fr', 'en'])
    cfg.setdefault('block_keywords', [])
    cfg.setdefault('boost_keywords', [])
    cfg.setdefault('source_priorities', {})
    cfg.setdefault('deduplicate', {'mode': 'title+url+canonical', 'window_hours': 48})
    cfg.setdefault('compress', {'target': 40, 'method': 'salience+diversity', 'diversity_axes': ['country','theme','source']})
    cfg.setdefault('scoring', {'freshness_half_life_hours': 18})
    cfg.setdefault('output', {'include_fields': ["title","summary","source","published_at","byline","country_guess","theme","entities","url"]})
    return cfg

# -------------------- Récupération flux --------------------
def fetch_one_feed(url: str, per_feed_max: int) -> Tuple[str, List[Dict[str, Any]]]:
    try:
        d = feedparser.parse(url)
    except Exception as e:
        log.warning("Parse error on %s: %s", url, e)
        return url, []
    entries = []
    for e in d.entries[: per_feed_max]:
        title = norm_text(getattr(e, 'title', '') or e.get('title') if isinstance(e, dict) else '')
        link  = canonical_url(getattr(e, 'link', '') or e.get('link') if isinstance(e, dict) else '')
        if not title or not link:
            continue
        summary = norm_text(getattr(e, 'summary', '') or getattr(e, 'description', '') or '')
        published_dt = parse_date(e)
        published_iso = published_dt.isoformat() if published_dt else ""
        byline = norm_text(getattr(e, 'author', '') or getattr(e, 'creator', '') or '')
        source = d.feed.get('link') or url
        # Certaines plateformes exposent "source.title"
        source_title = norm_text(d.feed.get('title', '')) or re.sub(r'^https?://(www\.)?', '', source).split('/')[0]
        language_hint = d.feed.get('language') or d.feed.get('dc_language') or ''
        entries.append({
            'title': title,
            'url': link,
            'summary': summary,
            'published_at': published_iso,
            'byline': byline,
            'source': source_title,
            'source_url': source,
            'language_hint': language_hint
        })
    return url, entries

def fetch_all(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    per_feed_max = int(cfg.get('per_feed_max', 20))
    feeds = cfg['feeds']
    out: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(16, max(4, len(feeds)))) as ex:
        futs = {ex.submit(fetch_one_feed, u, per_feed_max): u for u in feeds}
        for fut in as_completed(futs):
            _, entries = fut.result()
            out.extend(entries)
    log.info("Fetched %d raw entries from %d feeds", len(out), len(feeds))
    return out

# -------------------- Filtres & enrichissements --------------------
def language_filter(items: List[Dict[str, Any]], allow: List[str]) -> List[Dict[str, Any]]:
    allow = [a.lower() for a in allow]
    kept = []
    for it in items:
        text = f"{it['title']} {it['summary']}"
        lang = safe_detect(text, default=(it.get('language_hint') or 'en').split('-')[0].lower())
        it['lang'] = lang
        if lang in allow:
            kept.append(it)
    log.info("Language filter: %d -> %d (allow=%s)", len(items), len(kept), allow)
    return kept

def blocks_filter(items: List[Dict[str, Any]], block_words: List[str]) -> List[Dict[str, Any]]:
    if not block_words:
        return items
    pat = re.compile("|".join([re.escape(w) for w in block_words]), flags=re.I)
    kept = [it for it in items if not pat.search(f"{it['title']} {it['summary']}")]
    log.info("Block filter: %d -> %d (blocked by keywords)", len(items), len(kept))
    return kept

def extract_entities(txt: str) -> List[str]:
    # extraction légère basée sur mots-clés importants
    ents = []
    patterns = [
        r"\bIAEA\b|\bAIEA\b", r"\bE3\b", r"\bIRGC\b|\bPasdaran\b",
        r"\bHezbollah\b", r"\bHouthi\b", r"\bOFAC\b|\bOFSI\b|\bEU\b",
        r"\bIDF\b", r"\bPKK\b|\bSDF\b", r"\bRSF\b"
    ]
    for p in patterns:
        if re.search(p, txt, flags=re.I):
            ents.append(p.strip("\\b").upper())
    return sorted(list(set(ents)))

def enrich(items: List[Dict[str, Any]]) -> None:
    for it in items:
        it['country_guess'] = tag_country(it['title'], it['summary'])
        it['theme'] = tag_theme(it['title'], it['summary'])
        it['entities'] = extract_entities(f"{it['title']} {it['summary']}")

# -------------------- Scoring --------------------
def domain_from_source_url(src_url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", src_url)
    return (m.group(1).lower() if m else src_url).replace("www.", "")

def recency_score(published_at: str, half_life_h: float) -> float:
    if not published_at:
        return 0.3  # légèrement pénalisé mais non nul
    try:
        dt = dparser.parse(published_at)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        age_h = (now_utc() - dt.astimezone(timezone.utc)).total_seconds()/3600.0
        if age_h < 0:
            age_h = 0.0
        # Exponentiel: score = 0.5^(age/half-life)
        return pow(0.5, age_h / max(1e-6, half_life_h))
    except Exception:
        return 0.3

def keyword_boost(text: str, boosts: List[str]) -> float:
    if not boosts:
        return 0.0
    score = 0.0
    for term in boosts:
        # autoriser OR / wildcard simple
        if " OR " in term:
            if any(re.search(t.strip(), text, flags=re.I) for t in term.split(" OR ")):
                score += 0.3
        elif "*" in term:
            pat = re.compile(re.escape(term).replace("\\*", ".*"), re.I)
            if pat.search(text):
                score += 0.3
        else:
            if re.search(term, text, flags=re.I):
                score += 0.3
    return min(score, 1.5)

def rule_boosts(text: str, country: str, theme: str) -> float:
    plus = 0.0
    # Ajustements simples utiles à Chamsin
    if theme in ("Nucléaire", "Diplomatie/Sanctions"):
        plus += 0.3
    if country in ("Iran", "Israël/Palestine", "Liban", "Yémen"):
        plus += 0.2
    return plus

def score_items(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> None:
    half = float(cfg['scoring'].get('freshness_half_life_hours', 18))
    priorities = {k.lower(): float(v) for k, v in cfg.get('source_priorities', {}).items()}
    boosts = cfg.get('boost_keywords', [])
    for it in items:
        text = f"{it['title']} {it['summary']}"
        rec = recency_score(it.get('published_at',''), half)
        dom = domain_from_source_url(it.get('source_url',''))
        base = priorities.get(dom, 0.6)  # défaut neutre
        kwb = keyword_boost(text, boosts)
        rb = rule_boosts(text, it.get('country_guess','Autres'), it.get('theme','Général'))
        it['score'] = max(0.0, rec * base) + kwb + rb

# -------------------- Déduplication --------------------
def dedupe(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = cfg['deduplicate'].get('mode', 'title+url+canonical')
    # fenêtre temporelle (ne conserve que items dans window_hours si date dispo)
    window_h = float(cfg['deduplicate'].get('window_hours', 48))
    ref_time = now_utc()
    uniq: Dict[str, Dict[str, Any]] = {}
    for it in items:
        # filtre fenêtre
        ts_ok = True
        if it.get('published_at'):
            try:
                dt = dparser.parse(it['published_at'])
                if not dt.tzinfo:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_h = (ref_time - dt.astimezone(timezone.utc)).total_seconds()/3600.0
                ts_ok = (age_h <= window_h)
            except Exception:
                pass
        # On garde quand même si pas de date (sources officielles sans timestamp)
        if not ts_ok and it.get('published_at'):
            continue

        key_parts = []
        if 'title' in mode:
            key_parts.append(re.sub(r"\W+", "", it['title']).lower())
        if 'url' in mode:
            key_parts.append(canonical_url(it['url']).lower())
        if 'canonical' in mode:
            key_parts.append(domain_from_source_url(it.get('source_url','')))
        k = hashlib.md5("|".join(key_parts).encode('utf-8')).hexdigest()
        if k not in uniq or it['score'] > uniq[k]['score']:
            uniq[k] = it
    out = list(uniq.values())
    log.info("Dedupe: %d -> %d (mode=%s, window=%sh)", len(items), len(out), mode, window_h)
    return out

# -------------------- Compression (diversité) --------------------
def compress_diverse(items: List[Dict[str, Any]], target: int, axes: List[str]) -> List[Dict[str, Any]]:
    if len(items) <= target:
        return items
    # Tri initial par score desc
    items = sorted(items, key=lambda x: x['score'], reverse=True)
    chosen: List[Dict[str, Any]] = []
    seen: Dict[Tuple[str, ...], int] = {}
    # Greedy: tente de maximiser la couverture des combinaisons sur les axes
    for it in items:
        key = tuple((it.get(ax) or '').lower() for ax in axes)
        cnt = seen.get(key, 0)
        # tolérance: 1er passage favorise nouvelles combinaisons
        if cnt == 0 or len(chosen) < target // 2:
            chosen.append(it)
            seen[key] = cnt + 1
        if len(chosen) >= target:
            break
    # Si pas atteint, complète simplement par score
    if len(chosen) < target:
        pool = [it for it in items if it not in chosen]
        chosen.extend(pool[: target - len(chosen)])
    return chosen

def compress(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    target = int(cfg['compress'].get('target', 40))
    method = cfg['compress'].get('method', 'salience+diversity')
    axes = cfg['compress'].get('diversity_axes', ['country','theme','source'])
    if method == 'salience+diversity':
        return compress_diverse(items, target, axes)
    # fallback: simple top-k
    return sorted(items, key=lambda x: x['score'], reverse=True)[:target]

# -------------------- Projection champs de sortie --------------------
def project(items: List[Dict[str, Any]], cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    fields = cfg['output'].get('include_fields', [])
    out = []
    for it in items:
        row = {}
        for f in fields:
            if f == "url":
                row[f] = it.get('url')
            elif f == "title":
                row[f] = it.get('title')
            elif f == "summary":
                row[f] = it.get('summary')
            elif f == "source":
                row[f] = it.get('source')
            elif f == "published_at":
                row[f] = it.get('published_at')
            elif f == "byline":
                row[f] = it.get('byline')
            elif f == "country_guess":
                row[f] = it.get('country_guess')
            elif f == "theme":
                row[f] = it.get('theme')
            elif f == "entities":
                row[f] = it.get('entities', [])
            else:
                # Laisse passer champs non standards si ajoutés
                row[f] = it.get(f)
        # Ajouts utiles en interne
        row['score'] = round(float(it.get('score', 0.0)), 4)
        row['source_url'] = it.get('source_url')
        out.append(row)
    return out

# -------------------- Pipeline principal --------------------
def collect() -> List[Dict[str, Any]]:
    cfg = load_cfg()

    # 1) Fetch
    raw = fetch_all(cfg)

    # 2) Langues & blocks
    items = language_filter(raw, cfg.get('language_allowlist', ['fr','en']))
    items = blocks_filter(items, cfg.get('block_keywords', []))

    # 3) Enrichissements (pays, thème, entités)
    enrich(items)

    # 4) Scoring
    score_items(items, cfg)

    # 5) Déduplication
    items = dedupe(items, cfg)

    # 6) Cap "overall_max" avant compression
    overall_max = int(cfg.get('overall_max', 120))
    items = sorted(items, key=lambda x: x['score'], reverse=True)[:overall_max]

    # 7) Compression (diversité)
    items = compress(items, cfg)

    return items

# -------------------- Main --------------------
if __name__ == '__main__':
    data = collect()
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Collected {len(data)} items → {OUT_JSON}")
