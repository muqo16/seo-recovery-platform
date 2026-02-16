import concurrent.futures
import re
from typing import Dict, List, Tuple
from urllib.parse import urljoin, urlparse

import httpx

from .matching import normalize_url

EXCLUDED_EXTENSIONS = {
    ".css",
    ".js",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".json",
    ".map",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".zip",
}

EXCLUDED_PATH_PARTS = {"/build/", "/assets/"}


def run_quick_audit(urls: List[str], timeout_seconds: int = 8, max_workers: int = 8) -> List[Dict[str, str]]:
    unique_urls = list(dict.fromkeys(urls))
    results: List[Dict[str, str]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(audit_single_url, url, timeout_seconds): url for url in unique_urls}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda x: (severity_rank(x["severity"]), x["url"]), reverse=False)
    return results


def audit_single_url(url: str, timeout_seconds: int) -> Dict[str, str]:
    base = {
        "url": url,
        "status_code": "0",
        "severity": "info",
        "issues": "",
        "canonical": "",
        "final_url": "",
    }
    headers = {"User-Agent": "SEO-Recovery-Audit/1.0"}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers=headers) as client:
            response = client.get(url)
        html = response.text or ""
        final_url = str(response.url)
        issues: List[str] = []

        if response.status_code == 404:
            issues.append("404")
        elif response.status_code >= 500:
            issues.append("5xx")
        elif response.status_code >= 400:
            issues.append("4xx")

        if contains_noindex(html):
            issues.append("noindex")

        canonical = extract_canonical(html)
        if canonical:
            base["canonical"] = canonical
            if normalize_url(canonical) != normalize_url(final_url):
                issues.append("canonical_mismatch")

        if normalize_url(url) != normalize_url(final_url):
            issues.append("redirected")

        severity = classify_severity(issues)
        return {
            **base,
            "status_code": str(response.status_code),
            "severity": severity,
            "issues": ", ".join(issues),
            "final_url": final_url,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "severity": "critical",
            "issues": f"request_error: {str(exc)[:100]}",
        }


def contains_noindex(html: str) -> bool:
    return bool(re.search(r'<meta[^>]+name=["\']robots["\'][^>]*noindex', html, re.IGNORECASE))


def extract_canonical(html: str) -> str:
    match = re.search(
        r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
        html,
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def classify_severity(issues: List[str]) -> str:
    critical_items = {"5xx", "404", "request_error"}
    warning_items = {"4xx", "noindex", "canonical_mismatch"}
    issue_set = set(issues)
    if issue_set & critical_items:
        return "critical"
    if issue_set & warning_items:
        return "warning"
    return "info"


def severity_rank(severity: str) -> int:
    mapping = {"critical": 0, "warning": 1, "info": 2}
    return mapping.get(severity, 2)


def check_robots_and_sitemap(site_url: str, timeout_seconds: int = 8) -> List[Dict[str, str]]:
    site = ensure_site_url(site_url)
    robots_url = f"{site}/robots.txt"
    sitemap_url = f"{site}/sitemap.xml"

    checks = []
    for label, url in [("robots.txt", robots_url), ("sitemap.xml", sitemap_url)]:
        status, issue = fetch_status(url, timeout_seconds)
        checks.append(
            {
                "url": url,
                "status_code": str(status),
                "severity": "critical" if status >= 400 or status == 0 else "info",
                "issues": issue,
                "canonical": "",
                "final_url": "",
                "type": label,
            }
        )
    return checks


def fetch_status(url: str, timeout_seconds: int) -> Tuple[int, str]:
    headers = {"User-Agent": "SEO-Recovery-Audit/1.0"}
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers=headers) as client:
            response = client.get(url)
        if response.status_code >= 400:
            return response.status_code, "erisim_hatasi"
        return response.status_code, "ok"
    except Exception as exc:  # noqa: BLE001
        return 0, f"request_error: {str(exc)[:100]}"


def ensure_site_url(value: str) -> str:
    url = value.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return base


def discover_site_urls(site_url: str, limit: int = 200, timeout_seconds: int = 8) -> List[str]:
    base = ensure_site_url(site_url)
    discovered: List[str] = []

    sitemap_urls = fetch_sitemap_urls(base, timeout_seconds=timeout_seconds)
    for url in sitemap_urls:
        if is_same_domain(url, base) and should_include_url(url):
            discovered.append(url)
        if len(discovered) >= limit:
            break

    if len(discovered) >= limit:
        return list(dict.fromkeys(discovered))[:limit]

    crawled_urls = crawl_internal_links(base, limit=limit, timeout_seconds=timeout_seconds)
    discovered.extend(crawled_urls)
    return list(dict.fromkeys(discovered))[:limit]


def fetch_sitemap_urls(base_url: str, timeout_seconds: int = 8) -> List[str]:
    visited = set()
    queue = [f"{base_url}/sitemap.xml"]
    found: List[str] = []
    headers = {"User-Agent": "SEO-Recovery-Audit/1.0"}

    while queue:
        sitemap_url = queue.pop(0)
        if sitemap_url in visited:
            continue
        visited.add(sitemap_url)

        try:
            with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers=headers) as client:
                response = client.get(sitemap_url)
            if response.status_code >= 400:
                continue
            body = response.text or ""
        except Exception:  # noqa: BLE001
            continue

        loc_values = re.findall(r"<loc>(.*?)</loc>", body, flags=re.IGNORECASE | re.DOTALL)
        for raw in loc_values:
            candidate = raw.strip()
            if candidate.endswith(".xml"):
                queue.append(candidate)
            elif candidate.startswith(("http://", "https://")):
                found.append(candidate)

    return list(dict.fromkeys(found))


def crawl_internal_links(base_url: str, limit: int = 200, timeout_seconds: int = 8) -> List[str]:
    headers = {"User-Agent": "SEO-Recovery-Audit/1.0"}
    queue = [base_url]
    visited = set()
    found: List[str] = []

    with httpx.Client(follow_redirects=True, timeout=timeout_seconds, headers=headers) as client:
        while queue and len(found) < limit:
            current = queue.pop(0)
            normalized_current = normalize_url(current)
            if normalized_current in visited:
                continue
            visited.add(normalized_current)

            try:
                response = client.get(current)
            except Exception:  # noqa: BLE001
                continue

            final_url = str(response.url)
            if not is_same_domain(final_url, base_url):
                continue
            if response.status_code >= 400:
                continue

            normalized_final = normalize_url(final_url)
            if normalized_final not in visited:
                visited.add(normalized_final)
            if should_include_url(final_url):
                found.append(final_url)

            for link in extract_html_links(response.text or "", final_url):
                if not is_same_domain(link, base_url):
                    continue
                if not should_include_url(link):
                    continue
                link_norm = normalize_url(link)
                if link_norm in visited:
                    continue
                queue.append(link)
                if len(queue) > limit * 5:
                    queue = queue[: limit * 5]

    return list(dict.fromkeys(found))[:limit]


def extract_html_links(html: str, page_url: str) -> List[str]:
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
    links: List[str] = []
    for href in hrefs:
        if not href:
            continue
        lower = href.lower().strip()
        if lower.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(page_url, href).split("#")[0]
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)
    return links


def is_same_domain(url: str, base_url: str) -> bool:
    return urlparse(url).netloc.lower() == urlparse(base_url).netloc.lower()


def should_include_url(url: str) -> bool:
    parsed = urlparse(url)
    path = (parsed.path or "/").lower()

    for part in EXCLUDED_PATH_PARTS:
        if part in path:
            return False

    for ext in EXCLUDED_EXTENSIONS:
        if path.endswith(ext):
            return False

    return True
