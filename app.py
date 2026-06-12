from flask import Flask, render_template, jsonify, request
import json, os, threading, time
from datetime import datetime, timezone

app = Flask(__name__)
DATA_FILE = os.path.join(os.path.dirname(__file__), 'vm_data.json')

_db = None

def get_db():
    global _db
    if _db is not None:
        return _db
    uri = os.environ.get('MONGODB_URI')
    if not uri:
        return None
    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command('ping')
        _db = client.get_default_database()
        print('MongoDB connected')
    except Exception as e:
        print(f'MongoDB error: {e}')
    return _db

def load_data():
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        fallback = json.load(f)
    db = get_db()
    if db is not None:
        doc = db.vm_state.find_one({'_id': 'current'})
        if doc:
            doc.pop('_id')
            return doc
        db.vm_state.insert_one({'_id': 'current', **fallback})
    return fallback

def save_data(data):
    db = get_db()
    if db is not None:
        db.vm_state.replace_one({'_id': 'current'}, {'_id': 'current', **data}, upsert=True)
    else:
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

_matches_cache = []
_last_scraped = None

def parse_odds(val):
    if not val:
        return None
    try:
        return float(str(val).replace(',', '.'))
    except Exception:
        return None

def scrape_matches():
    global _matches_cache, _last_scraped
    import requests as req
    all_matches = []
    now = datetime.now(timezone.utc)

    for product in ['europatipset', 'stryktipset']:
        try:
            r = req.get(
                f'https://api.spela.svenskaspel.se/draw/1/{product}/draws',
                headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                timeout=15
            )
            if not r.ok:
                continue
            data = r.json()
            for draw in data.get('draws', []):
                product_name = draw.get('productName', product)
                for ev in draw.get('drawEvents', []):
                    match = ev.get('match', {})
                    start_str = match.get('matchStart', '')
                    if not start_str:
                        continue
                    try:
                        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if start_dt <= now:
                        continue
                    participants = match.get('participants', [])
                    hemma = participants[0].get('name', '') if len(participants) > 0 else ''
                    borta = participants[1].get('name', '') if len(participants) > 1 else ''
                    if not hemma and not borta:
                        desc = ev.get('eventDescription', '')
                        parts = desc.split(' - ', 1)
                        hemma = parts[0].strip() if parts else ''
                        borta = parts[1].strip() if len(parts) > 1 else ''
                    odds = ev.get('odds', {})
                    all_matches.append({
                        'product': product_name,
                        'hemma': hemma,
                        'borta': borta,
                        'start': start_dt.isoformat(),
                        'start_ts': start_dt.timestamp(),
                        'odds_1': parse_odds(odds.get('one')),
                        'odds_x': parse_odds(odds.get('x')),
                        'odds_2': parse_odds(odds.get('two')),
                    })
        except Exception as e:
            print(f'Scrape {product} error: {e}')

    seen = set()
    unique = []
    for m in all_matches:
        key = (m['hemma'], m['borta'], m['start'][:13])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    unique.sort(key=lambda x: x['start_ts'])
    _matches_cache = unique[:10]
    _last_scraped = datetime.now(timezone.utc)
    print(f'Scraped {len(_matches_cache)} matches')
    return _matches_cache

def background_scraper():
    time.sleep(5)
    while True:
        try:
            scrape_matches()
        except Exception as e:
            print(f'Background scraper error: {e}')
        time.sleep(3600)

threading.Thread(target=background_scraper, daemon=True).start()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/data')
def get_data():
    return jsonify(load_data())

@app.route('/api/save', methods=['POST'])
def save():
    try:
        data = request.json
        save_data(data)
        return jsonify({'status': 'ok'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/matches')
def get_matches():
    if not _matches_cache:
        scrape_matches()
    return jsonify({
        'matches': _matches_cache,
        'last_updated': _last_scraped.isoformat() if _last_scraped else None
    })

@app.route('/api/refresh-matches', methods=['POST'])
def refresh_matches():
    scrape_matches()
    return jsonify({
        'matches': _matches_cache,
        'last_updated': _last_scraped.isoformat() if _last_scraped else None
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    app.run(debug=True, port=port)
