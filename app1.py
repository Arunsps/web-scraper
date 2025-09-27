import requests
import urllib.robotparser

from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import json
import re
from collections import deque
from typing import List, Dict, Set, Optional
import concurrent.futures
import argparse
import time
import logging
from pathlib import Path
import trafilatura
from trafilatura import extract
import backoff
import random
import chardet

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class AdvancedWebScraper:
    def __init__(self, base_url: str, max_depth: int = 3, max_threads: int = 5, 
                 delay: float = 0.5, use_trafilatura: bool = True,
                 respect_robots: bool = True):
        self.base_url = base_url
        parsed_url = urlparse(base_url)
        self.domain = parsed_url.netloc
        self.scheme = parsed_url.scheme
        self.max_depth = max_depth
        self.max_threads = max_threads
        self.delay = delay  # Delay between requests in seconds
        self.use_trafilatura = use_trafilatura  # Use trafilatura for content extraction
        self.respect_robots = respect_robots  # Respect robots.txt rules
        
        self.visited_urls: Set[str] = set()
        self.seen_links: Set[str] = set()  # For efficient queue duplicate checking
        self.scraped_data: List[Dict] = []
        self.navigation_tree: Dict = {}
        self.robots_parser = urllib.robotparser.RobotFileParser()

        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
        
        # Rate limiting
        self.last_request_time = 0
        
        # Initialize robots.txt parser if needed
        if self.respect_robots:
            self.init_robots_parser()
        
    def init_robots_parser(self):
        """Initialize and fetch robots.txt rules"""
        try:
            robots_url = f"{self.scheme}://{self.domain}/robots.txt"
            self.robots_parser.set_url(robots_url)
            self.robots_parser.read()
            logger.info(f"Loaded robots.txt from {robots_url}")
        except Exception as e:
            logger.warning(f"Could not load robots.txt: {e}")
            # Create a permissive parser as fallback
            self.robots_parser = urllib.robotparser.RobotFileParser()
            self.robots_parser.parse([])  # Empty rules allow everything
    
    def can_fetch(self, url: str) -> bool:
        """Check if we're allowed to fetch this URL according to robots.txt"""
        if not self.respect_robots:
            return True
            
        try:
            return self.robots_parser.can_fetch(self.session.headers['User-Agent'], url)
        except Exception as e:
            logger.warning(f"Error checking robots.txt for {url}: {e}")
            return True  # Allow on error
    
    def rate_limit(self):
        """Enforce rate limiting with optional jitter"""
        elapsed = time.time() - self.last_request_time
        if elapsed < self.delay:
            sleep_time = self.delay - elapsed + random.uniform(0, 0.1)  # Add jitter
            time.sleep(sleep_time)
        self.last_request_time = time.time()
        
    def is_same_domain(self, url: str) -> bool:
        """Check if the URL belongs to the same domain"""
        parsed_url = urlparse(url)
        return parsed_url.netloc == self.domain
        
    def clean_text(self, text: str) -> str:
        """Clean and normalize text content"""
        if not text:
            return ""
            
        # Remove extra whitespace and newlines
        text = re.sub(r'\s+', ' ', text)
        # Remove special characters but keep basic punctuation
        text = re.sub(r'[^\w\s.,!?;:\-()]', '', text)
        return text.strip()
    
    def handle_encoding(self, response: requests.Response) -> str:
        """Handle encoding issues in response content"""
        try:
            # First try to use apparent encoding detected by chardet
            if response.encoding is None or response.encoding.lower() == 'iso-8859-1':
                detected_encoding = chardet.detect(response.content)
                if detected_encoding and detected_encoding.get('confidence', 0) > 0.7:
                    response.encoding = detected_encoding['encoding']
                else:
                    response.encoding = response.apparent_encoding
            
            return response.text
        except Exception as e:
            logger.warning(f"Encoding detection failed: {e}, trying utf-8 with errors ignore")
            return response.content.decode('utf-8', errors='ignore')
    
    def extract_content_trafilatura(self, html: str, url: str) -> str:
        """Extract content using trafilatura library for better content extraction"""
        try:
            extracted = extract(html, url=url, include_comments=False, include_tables=False)
            return self.clean_text(extracted) if extracted else ""
        except Exception as e:
            logger.warning(f"Trafilatura extraction failed for {url}: {e}")
            return self.extract_content_fallback(html)
    
    def extract_content_fallback(self, html: str) -> str:
        """Fallback content extraction using BeautifulSoup"""
        soup = BeautifulSoup(html, 'html.parser')
        
        # Remove unwanted elements
        for element in soup(["script", "style", "nav", "header", "footer", "aside", "form", "menu"]):
            element.decompose()
            
        # Try to find main content areas in order of preference
        selectors = [
            "main", "article", 
            "[role='main']", "[class*='content']", "[class*='main']", 
            "[class*='post']", "[class*='article']", "div#content", "div#main"
        ]
        
        main_content = None
        for selector in selectors:
            main_content = soup.select_one(selector)
            if main_content:
                break
                
        text = main_content.get_text() if main_content else soup.get_text()
        return self.clean_text(text)
    
    def get_page_title(self, soup: BeautifulSoup, url: str) -> str:
        """Extract the best possible page title"""
        # First try to find an H1
        h1 = soup.find('h1')
        if h1 and h1.get_text().strip():
            return h1.get_text().strip()
            
        # Then try the title tag
        title = soup.find('title')
        if title and title.get_text().strip():
            return title.get_text().strip()
            
        # Fallback to URL-based title
        parsed_url = urlparse(url)
        path_parts = [p for p in parsed_url.path.split('/') if p]
        return path_parts[-1].replace('-', ' ').title() if path_parts else "Untitled"
    
    def get_breadcrumb_path(self, soup: BeautifulSoup) -> Optional[str]:
        """Try to extract breadcrumb navigation if available"""
        breadcrumb_selectors = [
            ".breadcrumb", "[class*='breadcrumb']", 
            "[aria-label='breadcrumb']", "nav[aria-label='breadcrumb']"
        ]
        
        for selector in breadcrumb_selectors:
            breadcrumb = soup.select_one(selector)
            if breadcrumb:
                items = breadcrumb.find_all(['li', 'span'], recursive=False)
                if items:
                    path = " -> ".join([item.get_text().strip() for item in items if item.get_text().strip()])
                    return path
        return None
    
    def get_page_links(self, url: str, soup: BeautifulSoup) -> List[str]:
        """Extract all internal links from a page"""
        links = []
        for link in soup.find_all('a', href=True):
            href = link['href']
            # Skip empty links and javascript links
            if not href or href.startswith(('javascript:', 'mailto:', 'tel:')):
                continue
                
            full_url = urljoin(url, href)
            # Remove fragments and query parameters for deduplication
            parsed_url = urlparse(full_url)
            clean_url = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
            
            # Only include same-domain links that are HTTP/HTTPS
            if (self.is_same_domain(clean_url) and 
                clean_url.startswith(('http://', 'https://')) and
                not any(ext in clean_url.lower() for ext in [
                    '.pdf', '.jpg', '.jpeg', '.png', '.gif', '.doc', '.docx',
                    '.zip', '.tar', '.gz', '.exe', '.dmg', '.mp4', '.avi', '.mov'
                ]) and
                self.can_fetch(clean_url)):  # Check robots.txt
                links.append(clean_url)
                
        return list(set(links))  # Remove duplicates
    
    @backoff.on_exception(backoff.expo, requests.RequestException, max_tries=3)
    def fetch_page(self, url: str) -> Optional[requests.Response]:
        """Fetch a page with retry logic and rate limiting"""
        self.rate_limit()
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            # Do not assign to response.text (read-only). Use a local variable for HTML text.
            if not response.encoding or response.encoding.lower() == 'iso-8859-1':
                html_text = self.handle_encoding(response)
            else:
                html_text = response.text
            # Attach html_text as a custom attribute for downstream use
            response._html_text = html_text
            return response
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            raise
    
    def scrape_page(self, url: str, depth: int, parent: str = None) -> Optional[Dict]:
        """Scrape a single page and return its data"""
        if url in self.visited_urls:
            return None
        self.visited_urls.add(url)
        logger.info(f"Scraping: {url} (Depth: {depth})")
        try:
            response = self.fetch_page(url)
            if not response:
                return None
            # Use the custom html_text attribute if present
            html_text = getattr(response, '_html_text', response.text)
            # Parse HTML
            soup = BeautifulSoup(html_text, 'html.parser')
            # Extract page title
            page_title = self.get_page_title(soup, url)
            # Extract content using the best available method
            if self.use_trafilatura:
                content = self.extract_content_trafilatura(html_text, url)
            else:
                content = self.extract_content_fallback(html_text)
            # Extract links for further crawling
            links = self.get_page_links(url, soup) if depth < self.max_depth else []
            # Try to get breadcrumb path for better navigation
            breadcrumb_path = self.get_breadcrumb_path(soup)
            # Create page data
            page_data = {
                'url': url,
                'title': page_title,
                'content': content,
                'depth': depth,
                'links': links,
                'parent': parent,
                'breadcrumb': breadcrumb_path,
                'scraped_at': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            # Add to navigation tree
            self.add_to_navigation_tree(url, page_title, depth, parent, breadcrumb_path)
            return page_data
        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            return None
    
    def add_to_navigation_tree(self, url: str, title: str, depth: int, 
                              parent: str = None, breadcrumb: str = None):
        """Add page to navigation tree structure"""
        node_data = {
            'title': title,
            'children': [],
            'parent': parent,
            'depth': depth,
            'breadcrumb': breadcrumb
        }
        
        self.navigation_tree[url] = node_data
        
        if parent and parent in self.navigation_tree:
            self.navigation_tree[parent]['children'].append(url)
    
    def get_navigation_path(self, url: str) -> str:
        """Generate the navigation path for a URL"""
        if url not in self.navigation_tree:
            return url
            
        # Prefer breadcrumb if available
        node = self.navigation_tree[url]
        if node.get('breadcrumb'):
            return node['breadcrumb']
            
        # Fallback to reconstructed path
        path_parts = []
        current_url = url
        
        while current_url:
            if current_url in self.navigation_tree:
                node = self.navigation_tree[current_url]
                path_parts.append(node['title'])
                current_url = node['parent']
            else:
                break
                
        path_parts.reverse()
        return " -> ".join(path_parts)
    
    def stream_data_to_file(self, data_file: str):
        """Stream data to file to reduce memory usage"""
        with open(data_file, 'w', encoding='utf-8') as f:
            f.write('[\n')
            first_item = True
            
            for data in self.scraped_data:
                if not first_item:
                    f.write(',\n')
                json.dump(data, f, ensure_ascii=False, indent=2)
                first_item = False
                
            f.write('\n]')
    
    def crawl(self):
        """Main crawling function using BFS with multi-threading"""
        queue = deque([(self.base_url, 0, None)])
        futures = {}
        
        # Add base URL to seen links
        self.seen_links.add(self.base_url)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_threads) as executor:
            while queue or futures:
                # Submit new URLs from the queue
                while queue:
                    url, depth, parent = queue.popleft()
                    if url not in self.visited_urls:
                        future = executor.submit(self.scrape_page, url, depth, parent)
                        futures[future] = (url, depth, parent)
                
                # Process completed futures
                done, _ = concurrent.futures.wait(
                    futures, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED
                )
                
                for future in done:
                    url, depth, parent = futures[future]
                    try:
                        result = future.result()
                        if result:
                            self.scraped_data.append(result)
                            # Add new links to the queue if we haven't reached max depth
                            if depth < self.max_depth:
                                for link in result['links']:
                                    if (link not in self.visited_urls and 
                                        link not in self.seen_links and
                                        self.can_fetch(link)):
                                        queue.append((link, depth + 1, url))
                                        self.seen_links.add(link)
                    except Exception as e:
                        logger.error(f"Error processing {url}: {e}")
                    finally:
                        del futures[future]
    
    def export_data(self, data_file: str = "scraped_data.json", log_file: str = "scraped_log.txt"):
        """Export scraped data and navigation log"""
        # Export scraped data as JSON with streaming for large datasets
        self.stream_data_to_file(data_file)
        
        # Export navigation log
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write("Navigation Log\n")
            f.write("=" * 50 + "\n\n")
            
            # Sort by depth for better organization
            sorted_urls = sorted(
                self.navigation_tree.keys(), 
                key=lambda url: self.navigation_tree[url]['depth']
            )
            
            for url in sorted_urls:
                path = self.get_navigation_path(url)
                f.write(f"{path} ==> scraped\n")
                
            f.write(f"\nTotal pages scraped: {len(self.scraped_data)}\n")
            f.write(f"Respected robots.txt: {self.respect_robots}\n")
            f.write(f"Scraping completed at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    def generate_embeddings(self, output_file: str = "embeddings.json"):
        """Generate embeddings for the scraped content"""
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer('all-MiniLM-L6-v2')
            
            texts = [item['content'] for item in self.scraped_data]
            embeddings = model.encode(texts)
            
            # Save embeddings separately
            embeddings_data = []
            for i, item in enumerate(self.scraped_data):
                embeddings_data.append({
                    'url': item['url'],
                    'title': item['title'],
                    'embedding': embeddings[i].tolist()
                })
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(embeddings_data, f, indent=2, ensure_ascii=False)
                
            logger.info(f"Embeddings saved to {output_file}")
            return True
        except ImportError:
            logger.warning("sentence-transformers not installed. Skipping embeddings.")
            return False
        except Exception as e:
            logger.error(f"Error generating embeddings: {e}")
            return False

def main():
    parser = argparse.ArgumentParser(description='Advanced Web Scraper for Vectorization Preparation')
    parser.add_argument('--url', required=True, help='Starting URL to scrape')
    parser.add_argument('--depth', type=int, default=3, help='Depth limit for crawling (default: 3)')
    parser.add_argument('--threads', type=int, default=5, help='Number of threads (default: 5)')
    parser.add_argument('--delay', type=float, default=0.5, help='Delay between requests in seconds (default: 0.5)')
    parser.add_argument('--no-trafilatura', action='store_true', help='Disable trafilatura content extraction')
    parser.add_argument('--no-robots', action='store_true', help='Ignore robots.txt rules')
    parser.add_argument('--embeddings', action='store_true', help='Generate embeddings using sentence-transformers')
    parser.add_argument('--data-file', default='scraped_data.json', help='Output data file name')
    parser.add_argument('--log-file', default='scraped_log.txt', help='Output log file name')
    parser.add_argument('--embeddings-file', default='embeddings.json', help='Output embeddings file name')
    
    args = parser.parse_args()
    
    # Validate URL
    if not args.url.startswith(('http://', 'https://')):
        logger.error("URL must start with http:// or https://")
        return
    
    logger.info(f"Starting scraping: {args.url} with depth {args.depth}")
    start_time = time.time()
    
    # Initialize and run scraper
    scraper = AdvancedWebScraper(
        args.url, 
        args.depth, 
        args.threads, 
        args.delay,
        use_trafilatura=not args.no_trafilatura,
        respect_robots=not args.no_robots
    )
    scraper.crawl()
    
    # Generate embeddings if requested
    if args.embeddings:
        scraper.generate_embeddings(args.embeddings_file)
    
    # Export data
    scraper.export_data(args.data_file, args.log_file)
    
    elapsed_time = time.time() - start_time
    logger.info(f"Scraping completed in {elapsed_time:.2f} seconds")
    logger.info(f"Scraped {len(scraper.scraped_data)} pages")
    logger.info(f"Respected robots.txt: {not args.no_robots}")
    logger.info(f"Data saved to {args.data_file}")
    logger.info(f"Navigation log saved to {args.log_file}")

if __name__ == "__main__":
    main()