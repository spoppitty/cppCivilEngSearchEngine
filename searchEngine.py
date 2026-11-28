#-------------------------------------------------------------------------
# AUTHOR: Alex Kim, Nathan Li, Sarah Liu, Tin Pham
# FILENAME: searchEngine.py
# SPECIFICATION: Vertical search engine for CPP Civil Engineering faculty pages
# FOR: CS 4250 - Project
# TIME SPENT: 45 mins 
#-------------------------------------------------------------------------

# Make sure mongoDB is running first, then run this to create the database:
#       python searchEngine.py crawl --all

# Then, you can run search queries, example:
#       python searchEngine.py search "traffic safety" --page 1

import argparse
import math
import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pymongo import MongoClient


SEED_URL = "https://www.cpp.edu/engineering/ce/index.shtml"
CPP_HOST = "www.cpp.edu"
DEPARTMENT_PREFIX = "/engineering/ce/"
FACULTY_PREFIX = "/faculty/"
DEFAULT_TARGET_COUNT = 13
PAGE_SIZE = 5

# Resolve absolute/relative links and remove fragments.
def normalize_url(base_url: str, href: str) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    absolute = urljoin(base_url, href)
    absolute, _ = urldefrag(absolute)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != CPP_HOST:
        return None

    # Treat the different versions of a faculty home page as one URL.
    faculty_match = re.fullmatch(
        r"/faculty/([^/]+)(?:/index\.shtml)?/?",
        parsed.path,
        flags=re.IGNORECASE,
    )
    if faculty_match:
        absolute = f"{parsed.scheme}://{parsed.netloc}/faculty/{faculty_match.group(1)}/"

    return absolute

# Return True only for a faculty profile's main page.
def is_faculty_profile_url(url: str) -> bool:
    path = urlparse(url).path
    return re.fullmatch(
        r"/faculty/[^/]+(?:/index\.shtml)?/?",
        path,
        flags=re.IGNORECASE,
    ) is not None

# Allow the department site and linked CPP faculty profiles only.
def allowed_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.startswith(DEPARTMENT_PREFIX) or is_faculty_profile_url(url)

def clean_text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

# Extract the profile name from the common CPP faculty header.
def extract_faculty_name(soup: BeautifulSoup) -> str | None:
    for selector in ("main h1", "article h1", "h1", "main h2", "article h2"):
        heading = soup.select_one(selector)
        if heading:
            name = clean_text(heading)
            if name and len(name.split()) >= 2 and len(name) <= 120:
                return name
    return None

# Identify a target page using the faculty URL and department header text.
def is_civil_faculty_page(url: str, soup: BeautifulSoup) -> bool:
    if not is_faculty_profile_url(url):
        return False
    text = clean_text(soup)
    civil_markers = ("civil engineering", "civil & environmental engineering")
    return any(marker in text.lower() for marker in civil_markers) and extract_faculty_name(soup) is not None

def extract_indexed_content(soup: BeautifulSoup) -> str:
    # Extract the profile's main searchable sections, excluding site chrome.
    soup = BeautifulSoup(str(soup), "html.parser")
    for element in soup.select("script, style, noscript, nav, footer, header, aside, form, .navbar"):
        element.decompose()
    main = soup.select_one("main, article, #main, #content, .content") or soup.body or soup
    text = clean_text(main)
    return text

def extract_links(url: str, soup: BeautifulSoup) -> Iterable[str]:
    for anchor in soup.select("a[href]"):
        link = normalize_url(url, anchor.get("href", ""))
        if link and allowed_url(link):
            yield link

@dataclass
class FetchedPage:
    url: str
    html: str
    status_code: int
    error: str | None = None

class CivilCrawler:
    def __init__(self, db, seed_url: str = SEED_URL, delay: float = 0.2):
        self.db = db
        self.seed_url = seed_url
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "CPP-Civil-Search-Engine/1.0"})

    def fetch_html(self, url: str) -> FetchedPage:
        try:
            response = self.session.get(url, timeout=15)
            response.raise_for_status()
            return FetchedPage(url, response.text, response.status_code)
        except requests.RequestException as exc:
            return FetchedPage(url, "", getattr(exc.response, "status_code", 0) or 0, str(exc))

    def crawl(self, target_count: int | None = DEFAULT_TARGET_COUNT) -> int:
        frontier = deque([self.seed_url])
        seen = {self.seed_url}
        targets_found = 0
        highest_id = self.db.documents.find_one(sort=[("_id", -1)])
        next_doc_id = highest_id["_id"] if highest_id and isinstance(highest_id["_id"], int) else 0

        while frontier and (target_count is None or targets_found < target_count):
            url = frontier.popleft()
            page = self.fetch_html(url)
            soup = BeautifulSoup(page.html, "html.parser") if page.html else None
            faculty_name = None
            content = None

            if soup and is_civil_faculty_page(url, soup):
                faculty_name = extract_faculty_name(soup)
                content = extract_indexed_content(soup)
                existing = self.db.documents.find_one({"url": url}, {"_id": 1})
                document_id = existing["_id"] if existing else next_doc_id + 1
                if not existing:
                    next_doc_id = document_id
                self.db.documents.update_one(
                    {"url": url},
                    {"$set": {"_id": document_id, "url": url, "facultyName": faculty_name, "content": content}},
                    upsert=True,
                )
                targets_found += 1
                target_label = "all" if target_count is None else str(target_count)
                print(f"[{targets_found}/{target_label}] found: {faculty_name} -> {url}")

            self.db.crawled_pages.update_one(
                {"url": url},
                {"$set": {"url": url, "facultyName": faculty_name, "statusCode": page.status_code, "error": page.error}},
                upsert=True,
            )

            if (target_count is None or targets_found < target_count) and soup:
                for link in extract_links(url, soup):
                    if link not in seen:
                        seen.add(link)
                        frontier.append(link)
            time.sleep(self.delay)

        if targets_found:
            build_inverted_index(self.db)
        print(f"Crawl complete. Found {targets_found} Civil Engineering faculty pages.")
        if target_count is not None and targets_found < target_count:
            print(f"Warning: target count was {target_count}; frontier exhausted before reaching it.")
        return targets_found

TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")

def tokenize(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())

# Create one posting per term with docId, tf, and token positions.
def build_inverted_index(db) -> None:
    db.inverted_index.delete_many({})
    documents = list(db.documents.find({}, {"_id": 1, "content": 1}))
    postings_by_term: dict[str, list[dict]] = {}
    for document in documents:
        terms = tokenize(document.get("content", ""))
        positions_by_term: dict[str, list[int]] = {}
        for position, term in enumerate(terms):
            positions_by_term.setdefault(term, []).append(position)
        for term, positions in positions_by_term.items():
            postings_by_term.setdefault(term, []).append(
                {"docId": document["_id"], "tf": len(positions), "positions": positions}
            )
    if postings_by_term:
        db.inverted_index.insert_many(
            [{"term": term, "postings": postings} for term, postings in postings_by_term.items()]
        )
    print(f"Indexed {len(documents)} documents and {len(postings_by_term)} terms.")

def search(db, query: str, page_number: int = 1, page_size: int = PAGE_SIZE) -> tuple[list[dict], int]:
    terms = tokenize(query)
    if not terms:
        return [], 0
    unique_terms = set(terms)
    total_docs = max(db.documents.count_documents({}), 1)
    scores: Counter[int] = Counter()
    for entry in db.inverted_index.find({"term": {"$in": list(unique_terms)}}):
        df = len(entry["postings"])
        idf = math.log((total_docs + 1) / (df + 1)) + 1
        for posting in entry["postings"]:
            scores[posting["docId"]] += posting["tf"] * idf

    ranked = scores.most_common()
    total_results = len(ranked)
    start = max(page_number - 1, 0) * page_size
    selected = ranked[start : start + page_size]
    results = []
    for doc_id, score in selected:
        document = db.documents.find_one({"_id": doc_id})
        if document:
            results.append({**document, "score": round(score, 4), "snippet": make_snippet(document["content"], terms)})
    return results, total_results

def make_snippet(content: str, terms: list[str], width: int = 220) -> str:
    lower = content.lower()
    locations = [lower.find(term) for term in terms if lower.find(term) >= 0]
    start = max(min(locations) - 80, 0) if locations else 0
    snippet = content[start : start + width].strip()
    if start > 0:
        snippet = "..." + snippet
    if start + width < len(content):
        snippet += "..."
    return snippet

def get_database():
    client = MongoClient(os.getenv("MONGODB_URI", "mongodb://localhost:27017"))
    return client[os.getenv("MONGODB_DATABASE", "cpp_civil_search")]

def main() -> None:
    parser = argparse.ArgumentParser(description="CPP Civil Engineering vertical search engine")
    subparsers = parser.add_subparsers(dest="command", required=True)
    crawl_parser = subparsers.add_parser("crawl", help="crawl Civil Engineering faculty pages")
    crawl_parser.add_argument("--target-count", type=int, default=DEFAULT_TARGET_COUNT)
    crawl_parser.add_argument("--all", dest="crawl_all", action="store_true", help="crawl until all reachable Civil Engineering links are exhausted")
    crawl_parser.add_argument("--delay", type=float, default=0.2)
    subparsers.add_parser("reindex", help="rebuild the inverted index")
    search_parser = subparsers.add_parser("search", help="search indexed faculty pages")
    search_parser.add_argument("query")
    search_parser.add_argument("--page", type=int, default=1)
    args = parser.parse_args()
    db = get_database()

    if args.command == "crawl":
        target_count = None if args.crawl_all else args.target_count
        CivilCrawler(db, delay=args.delay).crawl(target_count)
    elif args.command == "reindex":
        build_inverted_index(db)
    elif args.command == "search":
        results, total = search(db, args.query, max(args.page, 1))
        print(f"{total} result(s), page {max(args.page, 1)}")
        for number, result in enumerate(results, start=1):
            print(f"\n{number}. {result['facultyName']}  [score={result['score']}]\n{result['url']}\n{result['snippet']}")

if __name__ == "__main__":
    main()
