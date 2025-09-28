from flask import Flask, render_template, jsonify, request
import threading
import time
import os
from app1 import AdvancedWebScraper

app = Flask(__name__)

scraper_thread = None
scraper = None
scraper_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'scraped': 0,
    'current_url': '',
    'log': []
}

stop_flag = threading.Event()

def run_scraper(url, depth, threads, delay):
    global scraper, scraper_status, stop_flag
    scraper = AdvancedWebScraper(url, depth, threads, delay)
    scraper_status['running'] = True
    scraper_status['log'].append(f"Started scraping: {url}")
    stop_flag.clear()
    try:
        from collections import deque
        queue = deque([(scraper.base_url, 0, None)])
        futures = {}
        scraper.seen_links.add(scraper.base_url)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=scraper.max_threads) as executor:
            while (queue or futures) and not stop_flag.is_set():
                while queue and not stop_flag.is_set():
                    url, depth, parent = queue.popleft()
                    if url not in scraper.visited_urls:
                        future = executor.submit(scraper.scrape_page, url, depth, parent)
                        futures[future] = (url, depth, parent)
                done, _ = concurrent.futures.wait(
                    futures, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    url, depth, parent = futures[future]
                    try:
                        result = future.result()
                        if result:
                            scraper.scraped_data.append(result)
                            scraper_status['scraped'] = len(scraper.scraped_data)
                            scraper_status['log'].append(f"Scraped: {result['url']}")
                            if depth < scraper.max_depth:
                                for link in result['links']:
                                    if (link not in scraper.visited_urls and 
                                        link not in scraper.seen_links and
                                        scraper.can_fetch(link)):
                                        queue.append((link, depth + 1, url))
                                        scraper.seen_links.add(link)
                    except Exception as e:
                        scraper_status['log'].append(f"Error processing {url}: {e}")
                    finally:
                        del futures[future]
        scraper.export_data()
        scraper_status['log'].append(f"Scraping completed. {len(scraper.scraped_data)} pages scraped.")
    except Exception as e:
        scraper_status['log'].append(f"Error: {str(e)}")
    scraper_status['running'] = False

@app.route('/')
def index():
    return render_template('dashboard.html')

@app.route('/start', methods=['POST'])
def start_scraper():
    global scraper_thread, scraper_status
    if scraper_status['running']:
        return jsonify({'status': 'already running'})
    data = request.json
    url = data.get('url')
    depth = int(data.get('depth', 2))
    threads = int(data.get('threads', 3))
    delay = float(data.get('delay', 0.5))
    scraper_status['progress'] = 0
    scraper_status['scraped'] = 0
    scraper_status['log'] = []
    scraper_thread = threading.Thread(target=run_scraper, args=(url, depth, threads, delay))
    scraper_thread.start()
    return jsonify({'status': 'started'})

@app.route('/stop', methods=['POST'])
def stop_scraper():
    global stop_flag, scraper_status
    if scraper_status['running']:
        stop_flag.set()
        scraper_status['log'].append('Stop requested by user.')
        return jsonify({'status': 'stopping'})
    return jsonify({'status': 'not running'})

@app.route('/status')
def status():
    if scraper:
        scraper_status['scraped'] = len(scraper.scraped_data)
    return jsonify(scraper_status)

@app.route('/chart-data')
def chart_data():
    if scraper:
        return jsonify({
            'labels': list(range(1, len(scraper.scraped_data)+1)),
            'data': [1]*len(scraper.scraped_data)
        })
    return jsonify({'labels': [], 'data': []})

@app.route('/data')
def data():
    if scraper:
        return jsonify(scraper.scraped_data)
    return jsonify([])

if __name__ == '__main__':
    app.run(debug=True)
