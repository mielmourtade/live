#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import feedparser, yaml, re, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'feeds.yml'
OUT_JSON = ROOT / 'scripts' / 'news_cache.json'

COUNTRY_TAGS = {
    'Iran': ['iran', 'tehran'],
    'Israël/Palestine': ['israel', 'gaza', 'west bank', 'hamas', 'idf', 'palestin'],
    'Liban': ['lebanon', 'hezbollah', 'beirut', 'south lebanon'],
    'Syrie': ['syria', 'damascus', 'idlib'],
    'Irak': ['iraq', 'baghdad', 'kurdistan'],
    'Yémen': ['yemen', 'houthi'],
    'Golfe': ['saudi', 'emirati', 'uae', 'qatar', 'bahrain', 'oman', 'kuwait'],
    'Caucase': ['armenia', 'azerbaijan', 'nagorno', 'karabakh', 'nakhchivan', 'yerevan', 'baku']
}

def load_cfg():
    with open(CONFIG, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)

def norm(s):
    return re.sub(r'\s+', ' ', (s or '').strip())

def tag_country(title, summary):
    text = f"{title} {summary}".lower()
    for country, keys in COUNTRY_TAGS.items():
        if any(k in text for k in keys):
            return country
    return 'Autres'

def hash_key(url, title):
    return hashlib.md5(f"{url}|{title}".encode()).hexdigest()

def collect():
    cfg = load_cfg()
    items = []
    for feed_url in cfg['feeds']:
        d = feedparser.parse(feed_url)
        for e in d.entries[: cfg.get('per_feed_max', 20)]:
            title = norm(getattr(e, 'title', ''))
            link = getattr(e, 'link', '')
            summary = norm(getattr(e, 'summary', ''))
            if not title or not link:
                continue
            items.append({
                'title': title,
                'link': link,
                'summary': summary,
                'published': getattr(e, 'published', ''),
                'country': tag_country(title, summary),
                'key': hash_key(link, title)
            })
    uniq = {}
    for it in items:
        uniq[it['key']] = it
    kept = list(uniq.values())[: cfg.get('overall_max', 120)]
    return kept

if __name__ == '__main__':
    import json
    data = collect()
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Collected {len(data)} items → {OUT_JSON}")
