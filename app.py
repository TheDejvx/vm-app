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

# Swedish VM-tipset name → English worldcup26.ir name
SV_TO_EN = {
    'usa': 'united states', 'mexiko': 'mexico', 'kanada': 'canada',
    'brasilien': 'brazil', 'argentina': 'argentina', 'colombia': 'colombia',
    'uruguay': 'uruguay', 'ecuador': 'ecuador', 'peru': 'peru',
    'chile': 'chile', 'venezuela': 'venezuela', 'bolivia': 'bolivia',
    'paraguay': 'paraguay', 'jamaica': 'jamaica', 'kuba': 'cuba',
    'haiti': 'haiti', 'el salvador': 'el salvador', 'honduras': 'honduras',
    'costa rica': 'costa rica', 'guatemala': 'guatemala', 'panama': 'panama',
    'trinidad och tobago': 'trinidad and tobago',
    'england': 'england', 'frankrike': 'france', 'tyskland': 'germany',
    'spanien': 'spain', 'portugal': 'portugal', 'belgien': 'belgium',
    'nederländerna': 'netherlands', 'italien': 'italy', 'kroatien': 'croatia',
    'serbien': 'serbia', 'schweiz': 'switzerland', 'österrike': 'austria',
    'turkiet': 'turkey', 'ukraina': 'ukraine', 'polonien': 'poland',
    'ungern': 'hungary', 'rumänien': 'romania', 'albanien': 'albania',
    'slovakien': 'slovakia', 'slovenien': 'slovenia', 'tjeckien': 'czech republic',
    'skottland': 'scotland', 'wales': 'wales', 'norge': 'norway',
    'sverige': 'sweden', 'danmark': 'denmark', 'finland': 'finland',
    'bosnien': 'bosnia and herzegovina',
    'bosnien & hercegovina': 'bosnia and herzegovina',
    'bosnien-hercegovina': 'bosnia and herzegovina',
    'sydkorea': 'south korea', 'japan': 'japan', 'iran': 'iran',
    'saudiarabien': 'saudi arabia', 'qatar': 'qatar', 'irak': 'iraq',
    'australien': 'australia', 'nya zeeland': 'new zealand',
    'indonesia': 'indonesia', 'kina': 'china', 'indien': 'india',
    'uzbekistan': 'uzbekistan',
    'marocko': 'morocco', 'tunisien': 'tunisia', 'nigeria': 'nigeria',
    'kamerun': 'cameroon', 'ghana': 'ghana', 'senegal': 'senegal',
    'sydafrika': 'south africa', 'egypten': 'egypt', 'mali': 'mali',
    'elfenbenskusten': 'ivory coast', 'dr kongo': 'dr congo',
    'tanzania': 'tanzania', 'zambia': 'zambia', 'angola': 'angola',
    'guinea': 'guinea', 'kap verde': 'cape verde', 'namibia': 'namibia',
    'kenya': 'kenya', 'kongo': 'dr congo',
}

def sv_to_en(name):
    key = name.lower().strip()
    return SV_TO_EN.get(key, key)

def name_key(s):
    return s.lower().strip()

def scrape_matches():
    global _matches_cache, _last_scraped
    import requests as req

    wc_matches = []

    # Primary: worldcup26.ir — all 104 VM matches, free, no key
    try:
        r = req.get('https://worldcup26.ir/get/games',
                    headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.ok:
            for g in r.json().get('games', []):
                if g.get('finished', 'FALSE') == 'TRUE':
                    continue
                date_str = g.get('local_date', '')  # "06/12/2026 15:00"
                if not date_str:
                    continue
                try:
                    dt = datetime.strptime(date_str, '%m/%d/%Y %H:%M')
                except Exception:
                    continue
                parts = date_str.split(' ')[0].split('/')  # ["06","12","2026"]
                iso_date = f"{parts[2]}-{parts[0]}-{parts[1]}" if len(parts) == 3 else ''
                wc_matches.append({
                    'hemma': g.get('home_team_name_en', ''),
                    'borta': g.get('away_team_name_en', ''),
                    'start': date_str,
                    'start_ts': dt.timestamp(),
                    'iso_date': iso_date,
                    'group': g.get('group', ''),
                    'match_type': g.get('type', 'group'),
                    'odds_1': None, 'odds_x': None, 'odds_2': None,
                })
    except Exception as e:
        print(f'worldcup26.ir error: {e}')

    # Secondary: VM-tipset — enrich with odds where teams match
    try:
        r = req.get('https://api.spela.svenskaspel.se/draw/1/europatipset/draws',
                    headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
        if r.ok:
            for draw in r.json().get('draws', []):
                for ev in draw.get('drawEvents', []):
                    match = ev.get('match', {})
                    start_str = match.get('matchStart', '')
                    if not start_str:
                        continue
                    ev_date = start_str[:10]  # "2026-06-17"
                    participants = match.get('participants', [])
                    sv_h = sv_to_en(participants[0].get('name', '')) if participants else ''
                    sv_a = sv_to_en(participants[1].get('name', '')) if len(participants) > 1 else ''
                    odds = ev.get('odds', {})
                    o1 = parse_odds(odds.get('one'))
                    ox = parse_odds(odds.get('x'))
                    o2 = parse_odds(odds.get('two'))
                    for wm in wc_matches:
                        if wm['iso_date'] != ev_date:
                            continue
                        wm_h = name_key(wm['hemma'])
                        wm_a = name_key(wm['borta'])
                        if wm_h == sv_h and wm_a == sv_a:
                            wm['odds_1'] = o1
                            wm['odds_x'] = ox
                            wm['odds_2'] = o2
                            break
    except Exception as e:
        print(f'VM-tipset odds error: {e}')

    wc_matches.sort(key=lambda x: x['start_ts'])
    _matches_cache = [{k: v for k, v in m.items() if k != 'iso_date'} for m in wc_matches[:15]]
    _last_scraped = datetime.now(timezone.utc)
    print(f'Scraped {len(_matches_cache)} VM matches')
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

@app.route('/api/debug')
def debug():
    import requests as req
    result = {'cache_size': len(_matches_cache), 'last_scraped': _last_scraped.isoformat() if _last_scraped else None}
    try:
        r = req.get('https://worldcup26.ir/get/games', headers={'User-Agent': 'Mozilla/5.0'}, timeout=10)
        games = r.json().get('games', [])
        not_finished = [g for g in games if g.get('finished', 'FALSE') != 'TRUE']
        result['wc_api_status'] = r.status_code
        result['wc_total_games'] = len(games)
        result['wc_not_finished'] = len(not_finished)
        result['sample_finished_field'] = repr(games[0].get('finished')) if games else None
    except Exception as e:
        result['wc_api_error'] = str(e)
    return jsonify(result)

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
