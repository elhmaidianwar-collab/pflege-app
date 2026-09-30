import sys
import os
import re
import json
import asyncio
import urllib.parse
from urllib.parse import urljoin
from io import BytesIO

from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
import uvicorn
import requests
import pandas as pd
from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding='utf-8')

IGNORE_DOMAINS = [
    'google.com', 'gstatic.com', 'fontawesome.com', 'defiant.com', 
    'sentry.io', 'wixpress.com', 'wordpress.org', 'schema.org', 
    'cloudflare.com', 'w3.org', 'doubleclick.net'
]

def clean_email(email_str):
    if not email_str:
        return None
    email = email_str.strip().strip('.').lower()
    if any(email.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.svg', '.webp', '.gif', '.css', '.js']):
        return None
    for domain in IGNORE_DOMAINS:
        if domain in email:
            return None
    return email

# Fallback search using OpenStreetMap Overpass (No browser required, 100% reliable anywhere)
async def search_osm_fallback(query, city, country="Deutschland"):
    print(f"[*] Executing fast search fallback for '{query}' in '{city}'...")
    try:
        lat, lon = None, None
        try:
            geo_res = requests.get(
                f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=de&format=json",
                timeout=4
            ).json()
            if geo_res.get('results'):
                lat = geo_res['results'][0]['latitude']
                lon = geo_res['results'][0]['longitude']
                print(f"[+] Geocoded {city} -> ({lat}, {lon})")
        except Exception:
            pass

        if lat and lon:
            overpass_query = f"""
            [out:json][timeout:20];
            (
              node["amenity"~"nursing_home|hospital|social_facility"](around:25000, {lat}, {lon});
              way["amenity"~"nursing_home|hospital|social_facility"](around:25000, {lat}, {lon});
              node["healthcare"~"nurse|hospital|clinic|centre"](around:25000, {lat}, {lon});
              way["healthcare"~"nurse|hospital|clinic|centre"](around:25000, {lat}, {lon});
            );
            out tags 60;
            """
        else:
            overpass_query = f"""
            [out:json][timeout:20];
            area["name"="{city}"]->.searchArea;
            (
              node["amenity"~"nursing_home|hospital|social_facility"](area.searchArea);
              way["amenity"~"nursing_home|hospital|social_facility"](area.searchArea);
            );
            out tags 60;
            """

        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, lambda: requests.post(
            "https://overpass-api.de/api/interpreter",
            data={'data': overpass_query},
            headers={'User-Agent': 'PflegeApp/1.0'},
            timeout=20
        ))
        
        results = []
        if res.status_code == 200:
            data = res.json()
            elements = data.get('elements', [])
            for idx, el in enumerate(elements):
                t = el.get('tags', {})
                name = t.get('name') or t.get('operator') or f"{query} {idx+1}"
                street = t.get('addr:street', '')
                house = t.get('addr:housenumber', '')
                postcode = t.get('addr:postcode', '')
                full_addr = f"{street} {house}, {postcode} {city}".strip(', ')
                phone = t.get('phone') or t.get('contact:phone') or 'غير متوفر'
                website = t.get('website') or t.get('contact:website') or ''
                email = t.get('email') or t.get('contact:email') or 'قيد الاستخراج...'
                
                results.append({
                    'id': idx + 1,
                    'name': name,
                    'website': website,
                    'phone': phone,
                    'address': full_addr if len(full_addr) > 5 else city,
                    'email': email
                })
        print(f"[+] Fallback found {len(results)} establishments.")
        return results
    except Exception as e:
        print("[-] Fallback error:", e)
        return []

# Real Google Maps Scraper using Playwright with robust error handling
async def search_google_maps(query, city, country="Deutschland", radius="", max_scrolls=6):
    parts = [query, city]
    if radius:
        parts.append(radius)
    if country:
        parts.append(country)
    full_query = " ".join([p for p in parts if p]).strip()
    search_url = f"https://www.google.com/maps/search/{urllib.parse.quote_plus(full_query)}?hl=de"
    
    results = []
    browser = None
    
    try:
        print(f"[*] Attempting Google Maps Playwright: {search_url}")
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--no-first-run',
                    '--no-zygote',
                    '--single-process'
                ]
            )
            context = await browser.new_context(
                locale='de-DE',
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
            )
            page = await context.new_page()
            await page.goto(search_url, wait_until='domcontentloaded', timeout=20000)
            await page.wait_for_timeout(2000)
            
            # Consent popup
            try:
                consent = page.locator('button:has-text("Alle akzeptieren"), button:has-text("Ich stimme zu"), form[action*="consent"] button')
                if await consent.count() > 0:
                    await consent.first.click()
                    await page.wait_for_timeout(1500)
            except Exception:
                pass
                
            feed = page.locator('div[role="feed"]')
            if await feed.count() > 0:
                for _ in range(max_scrolls):
                    await feed.evaluate('el => el.scrollBy(0, 1500)')
                    await page.wait_for_timeout(700)
            else:
                await page.mouse.wheel(0, 2000)
                await page.wait_for_timeout(1000)
                
            links = page.locator('div[role="feed"] a[href*="/maps/place/"]')
            total = await links.count()
            print(f"[+] Found {total} raw Google Maps links")
            
            for i in range(total):
                link = links.nth(i)
                name = (await link.get_attribute('aria-label')) or ''
                href = (await link.get_attribute('href')) or ''
                
                if not name or name in [r['name'] for r in results]:
                    continue
                    
                parent = link.locator('xpath=ancestor::div[contains(@class, "Nv2PK")]')
                website = ''
                phone = ''
                address = ''
                
                if await parent.count() > 0:
                    web_el = parent.locator('a[data-value="Website"], a[aria-label*="Website"]')
                    if await web_el.count() > 0:
                        website = (await web_el.first.get_attribute('href')) or ''
                        if website.startswith('/aclk') or website.startswith('/'):
                            website = ''
                        
                    raw_text = await parent.inner_text()
                    lines = raw_text.split('\n')
                    for l in lines:
                        clean_l = l.strip()
                        if any(c.isdigit() for c in clean_l) and ('02' in clean_l or '01' in clean_l or '+49' in clean_l or '/' in clean_l):
                            match_phone = re.search(r'(\+49[\d\s/-]+|0[\d\s/-]{7,})', clean_l)
                            if match_phone:
                                phone = match_phone.group(0).strip()
                            else:
                                phone = clean_l
                        elif 'str' in clean_l.lower() or 'weg' in clean_l.lower() or 'platz' in clean_l.lower() or city.lower() in clean_l.lower():
                            if not address and len(clean_l) > 4:
                                address = clean_l
                                
                results.append({
                    'id': len(results) + 1,
                    'name': name,
                    'website': website,
                    'phone': phone or 'غير متوفر',
                    'address': address or city,
                    'email': 'قيد الاستخراج...'
                })
            await browser.close()
    except Exception as e:
        print("[-] Playwright encountered an issue in this environment:", e)
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
                
    # If Google Maps returned no results or failed, seamlessly use fallback
    if not results:
        print("[*] Switching to fallback engine...")
        results = await search_osm_fallback(query, city, country)
        
    return results

def scrape_email_from_website_sync(website_url):
    if not website_url:
        return 'لا يوجد موقع مسجل'
    if not website_url.startswith('http'):
        website_url = 'https://' + website_url
        
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    }
    
    found_emails = set()
    try:
        res = requests.get(website_url, headers=headers, timeout=5, verify=False)
        emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', res.text)
        for e in emails:
            ce = clean_email(e)
            if ce:
                found_emails.add(ce)
                
        if not found_emails:
            for sub in ['/impressum', '/kontakt', '/kontakt.html', '/impressum.html', '/karriere', '/jobs']:
                try:
                    sub_url = urljoin(website_url, sub)
                    sub_res = requests.get(sub_url, headers=headers, timeout=4, verify=False)
                    if sub_res.status_code == 200:
                        sub_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', sub_res.text)
                        for e in sub_emails:
                            ce = clean_email(e)
                            if ce:
                                found_emails.add(ce)
                        if found_emails:
                            break
                except:
                    pass
    except Exception:
        return 'تعذر فتح الموقع'
        
    if not found_emails:
        return 'غير متوفر بالموقع'
        
    sorted_emails = sorted(list(found_emails), key=lambda x: (
        0 if any(k in x for k in ['bewerbung', 'karriere', 'jobs', 'personal']) else
        1 if any(k in x for k in ['info', 'kontakt', 'leitung']) else 2
    ))
    return sorted_emails[0]

# --- API Endpoints ---

async def handle_scan(request):
    try:
        data = await request.json()
        query = data.get('query', 'Pflegedienst')
        city = data.get('city', 'Köln')
        country = data.get('country', 'Deutschland')
        radius = data.get('radius', '')
        print(f"[*] API Scan: '{query}' in '{city}', '{country}' (radius: '{radius}')")
        places = await search_google_maps(query, city, country, radius, max_scrolls=6)
        print(f"[+] Total places ready: {len(places)}")
        return JSONResponse({'places': places, 'count': len(places)})
    except Exception as e:
        print("[-] API Scan Error:", e)
        # Even on error, try fallback to return real results instead of 500 error!
        try:
            fallback_places = await search_osm_fallback(query, city, country)
            return JSONResponse({'places': fallback_places, 'count': len(fallback_places)})
        except Exception:
            return JSONResponse({'error': str(e), 'places': []}, status_code=200)

async def handle_extract(request):
    try:
        data = await request.json()
        places = data.get('places', [])
        limit = int(data.get('limit', len(places)))
        target_places = places[:limit]
        
        print(f"[*] Extracting emails for {len(target_places)} places...")
        loop = asyncio.get_event_loop()
        for idx, p in enumerate(target_places):
            site = p.get('website', '')
            email = await loop.run_in_executor(None, scrape_email_from_website_sync, site)
            p['email'] = email
            print(f"[{idx+1}/{len(target_places)}] {p['name']} -> {email}")
            
        return JSONResponse({'places': target_places})
    except Exception as e:
        print("[-] API Extract Error:", e)
        return JSONResponse({'error': str(e), 'places': []}, status_code=200)

async def handle_export(request):
    try:
        data = await request.json()
        places = data.get('places', [])
        city = data.get('city', 'Pflege')
        
        rows = []
        for p in places:
            rows.append({
                'اسم المنشأة': p.get('name', ''),
                'البريد الإلكتروني (E-Mail)': p.get('email', ''),
                'رقم الهاتف (Telefon)': p.get('phone', ''),
                'الموقع الإلكتروني (Website)': p.get('website', ''),
                'العنوان (Adresse)': p.get('address', '')
            })
            
        df = pd.DataFrame(rows)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Pflege_Bewerbungen')
        output.seek(0)
        
        headers = {
            'Content-Disposition': f'attachment; filename="Bewerbung_Pflege_{city}.xlsx"'
        }
        return Response(
            content=output.getvalue(),
            media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers=headers
        )
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=200)

async def handle_home(request):
    html_file = os.path.join(os.path.dirname(__file__), 'index.html')
    with open(html_file, 'r', encoding='utf-8') as f:
        content = f.read()
    return HTMLResponse(content)

async def handle_manifest(request):
    manifest_file = os.path.join(os.path.dirname(__file__), 'manifest.json')
    if os.path.exists(manifest_file):
        with open(manifest_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return Response(content, media_type='application/manifest+json')
    return Response("{}", media_type='application/json')

routes = [
    Route('/', handle_home, methods=['GET']),
    Route('/manifest.json', handle_manifest, methods=['GET']),
    Route('/api/scan', handle_scan, methods=['POST']),
    Route('/api/extract', handle_extract, methods=['POST']),
    Route('/api/export', handle_export, methods=['POST']),
]

middleware = [
    Middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
]

app = Starlette(routes=routes, middleware=middleware)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 7860))
    import socket
    local_ip = 'localhost'
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass
    print("=" * 60)
    print("🚀 محرك Google Maps واستخراج الإيميلات يعمل الآن بنجاح!")
    print(f"👉 على الكمبيوتر: http://localhost:{port}")
    print(f"👉 على هاتفك (عبر الواي فاي): http://{local_ip}:{port}")
    print("=" * 60)
    uvicorn.run(app, host='0.0.0.0', port=port)
