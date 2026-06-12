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

# Swedish Oddset name → English worldcup26.ir name
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
    'bosnien och hercegovina': 'bosnia and herzegovina',
    'sydkorea': 'south korea', 'japan': 'japan', 'iran': 'iran',
    'saudiarabien': 'saudi arabia', 'qatar': 'qatar', 'irak': 'iraq',
    'australien': 'australia', 'nya zeeland': 'new zealand',
    'indonesien': 'indonesia', 'indonesia': 'indonesia',
    'kina': 'china', 'indien': 'india', 'uzbekistan': 'uzbekistan',
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

def _search_odds_in_json(data, odds_map, depth=0):
    """Recursively scan an unknown JSON structure for football match odds."""
    if depth > 10 or not isinstance(data, (dict, list)):
        return
    if isinstance(data, list):
        for item in data:
            _search_odds_in_json(item, odds_map, depth + 1)
        return

    # Try to extract home/away team names from various field naming conventions
    home = (data.get('homeName') or data.get('homeTeam') or data.get('home_team') or
            data.get('homeTeamName') or data.get('home') or '')
    away = (data.get('awayName') or data.get('awayTeam') or data.get('away_team') or
            data.get('awayTeamName') or data.get('away') or '')
    if isinstance(home, dict):
        home = home.get('name') or home.get('displayName') or ''
    if isinstance(away, dict):
        away = away.get('name') or away.get('displayName') or ''

    # Try participants/teams array
    if not home or not away:
        parts = data.get('participants') or data.get('teams') or data.get('competitors') or []
        if isinstance(parts, list) and len(parts) >= 2:
            p0, p1 = parts[0], parts[1]
            home = (p0.get('name') or p0.get('displayName') or '') if isinstance(p0, dict) else str(p0)
            away = (p1.get('name') or p1.get('displayName') or '') if isinstance(p1, dict) else str(p1)

    if home and away and len(str(home)) > 2 and len(str(away)) > 2:
        o1 = ox = o2 = None

        # Try outcomes list [home_win, draw, away_win]
        outcomes = (data.get('outcomes') or data.get('betOutcomes') or
                    data.get('prices') or [])
        if isinstance(outcomes, list) and len(outcomes) >= 3:
            def get_val(o):
                if isinstance(o, dict):
                    return (o.get('odds') or o.get('price') or o.get('value') or
                            o.get('decimalOdds') or o.get('decimal'))
                return o
            o1 = parse_odds(get_val(outcomes[0]))
            ox = parse_odds(get_val(outcomes[1]))
            o2 = parse_odds(get_val(outcomes[2]))

        # Try named odds dict
        if not o1:
            odds_obj = data.get('odds') or data.get('prices') or {}
            if isinstance(odds_obj, dict):
                o1 = parse_odds(odds_obj.get('1') or odds_obj.get('home') or odds_obj.get('one'))
                ox = parse_odds(odds_obj.get('X') or odds_obj.get('x') or odds_obj.get('draw'))
                o2 = parse_odds(odds_obj.get('2') or odds_obj.get('away') or odds_obj.get('two'))

        if o1 and o2:
            h_en = sv_to_en(str(home).strip())
            a_en = sv_to_en(str(away).strip())
            odds_map[(h_en, a_en)] = (o1, ox, o2)
            print(f'Oddset: {home} vs {away} → {h_en}/{a_en} | {o1}/{ox}/{o2}')

    for v in data.values():
        _search_odds_in_json(v, odds_map, depth + 1)


def scrape_oddset():
    """Load spela.svenskaspel.se/odds with Playwright, intercept JSON API calls, extract 1X2 odds."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('Playwright not installed — no Oddset odds')
        return {}

    odds_map = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            ctx = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                locale='sv-SE'
            )
            page = ctx.new_page()

            captured = []

            def on_response(response):
                if response.status != 200:
                    return
                ct = response.headers.get('content-type', '')
                if 'json' not in ct:
                    return
                try:
                    data = response.json()
                    captured.append({'url': response.url, 'data': data})
                except Exception:
                    pass

            page.on('response', on_response)
            page.goto('https://spela.svenskaspel.se/odds', timeout=30000, wait_until='networkidle')
            page.wait_for_timeout(3000)
            browser.close()

        print(f'Oddset: {len(captured)} JSON responses captured')
        for item in captured:
            print(f'  {item["url"]}')
            _search_odds_in_json(item['data'], odds_map)

    except Exception as e:
        print(f'Oddset Playwright error: {e}')

    print(f'Oddset: found odds for {len(odds_map)} matches')
    return odds_map


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
                wc_matches.append({
                    'hemma': g.get('home_team_name_en', ''),
                    'borta': g.get('away_team_name_en', ''),
                    'start': date_str,
                    'start_ts': dt.timestamp(),
                    'group': g.get('group', ''),
                    'match_type': g.get('type', 'group'),
                    'odds_1': None, 'odds_x': None, 'odds_2': None,
                })
    except Exception as e:
        print(f'worldcup26.ir error: {e}')

    # Secondary: Oddset — enrich with odds via Playwright page scrape
    try:
        odds_map = scrape_oddset()
        for wm in wc_matches:
            wm_h = name_key(wm['hemma'])
            wm_a = name_key(wm['borta'])
            if (wm_h, wm_a) in odds_map:
                o1, ox, o2 = odds_map[(wm_h, wm_a)]
                wm['odds_1'] = o1
                wm['odds_x'] = ox
                wm['odds_2'] = o2
    except Exception as e:
        print(f'Oddset enrichment error: {e}')

    wc_matches.sort(key=lambda x: x['start_ts'])
    _matches_cache = wc_matches[:10]
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
    except Exception as e:
        result['wc_api_error'] = str(e)
    return jsonify(result)

@app.route('/api/debug-odds')
def debug_odds():
    odds_map = scrape_oddset()
    return jsonify({
        'matches_with_odds': len(odds_map),
        'odds': {f'{h} vs {a}': {'1': o1, 'x': ox, '2': o2} for (h, a), (o1, ox, o2) in odds_map.items()}
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
