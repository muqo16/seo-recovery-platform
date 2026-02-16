import csv
import io
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional
from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def path_of(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return path


def infer_type(url: str) -> str:
    path = path_of(url).lower()
    if "/products/" in path:
        return "product"
    if "/collections/" in path or "/category/" in path:
        return "category"
    if "/blog" in path or "/blogs/" in path:
        return "blog"
    return "page"


def slug_of(url: str) -> str:
    path = path_of(url)
    slug = path.rsplit("/", 1)[-1]
    return slug.lower()


def tokenize_slug(slug: str) -> List[str]:
    return [t for t in re.split(r"[-_\s]+", slug) if t]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def match_urls(old_rows: List[Dict[str, str]], new_rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    new_by_norm: Dict[str, str] = {}
    new_by_path: Dict[str, str] = {}
    new_by_type: Dict[str, List[str]] = {"product": [], "category": [], "page": [], "blog": []}
    new_all_urls: List[str] = []

    for row in new_rows:
        url = row["url"]
        normalized = normalize_url(url)
        p = path_of(url)
        page_type = (row.get("type") or infer_type(url)).lower()
        new_by_norm[normalized] = url
        new_by_path[p.lower()] = url
        if page_type not in new_by_type:
            page_type = "page"
        new_by_type[page_type].append(url)
        new_all_urls.append(url)

    results: List[Dict[str, str]] = []
    for row in old_rows:
        old_url = row["url"]
        old_type = (row.get("type") or infer_type(old_url)).lower()
        old_norm = normalize_url(old_url)
        old_path = path_of(old_url).lower()
        old_slug = slug_of(old_url)

        if old_norm in new_by_norm:
            target = new_by_norm[old_norm]
            results.append(result_row(old_url, target, 100, "exact_url", old_type))
            continue

        if old_path in new_by_path:
            target = new_by_path[old_path]
            results.append(result_row(old_url, target, 95, "exact_path", old_type))
            continue

        candidates = new_by_type.get(old_type) or new_all_urls
        best_target: Optional[str] = None
        best_score = 0.0
        for candidate in candidates:
            cand_slug = slug_of(candidate)
            seq_score = similarity(old_slug, cand_slug)
            old_tokens = set(tokenize_slug(old_slug))
            cand_tokens = set(tokenize_slug(cand_slug))
            union = len(old_tokens | cand_tokens) or 1
            jaccard = len(old_tokens & cand_tokens) / union
            score = (seq_score * 0.75) + (jaccard * 0.25)
            if score > best_score:
                best_score = score
                best_target = candidate

        percent = int(round(best_score * 100))
        if best_target and percent >= 60:
            results.append(result_row(old_url, best_target, percent, "slug_similarity", old_type))
        else:
            results.append(result_row(old_url, "", 0, "manual_required", old_type))

    return results


def result_row(old_url: str, new_url: str, score: int, reason: str, page_type: str) -> Dict[str, str]:
    return {
        "old_url": old_url,
        "new_url": new_url,
        "score": score,
        "reason": reason,
        "type": page_type,
        "old_path": path_of(old_url),
        "new_path": path_of(new_url) if new_url else "",
    }


def build_redirects_csv(matches: List[Dict[str, str]], score_threshold: int = 70) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Redirect from", "Redirect to"])

    seen = set()
    for item in matches:
        if item["score"] < score_threshold:
            continue
        source = item["old_path"] or "/"
        target = item["new_path"] or "/"
        if not source.startswith("/"):
            source = f"/{source}"
        if not target.startswith("/"):
            target = f"/{target}"
        if source == target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        writer.writerow([source, target])

    return output.getvalue()


def build_manual_review_csv(matches: List[Dict[str, str]], score_threshold: int = 70) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["old_url", "onerilen_new_url", "score", "reason"])
    for item in matches:
        if item["score"] >= score_threshold:
            continue
        writer.writerow([item["old_url"], item["new_url"], item["score"], item["reason"]])
    return output.getvalue()


def summarize_matches(matches: List[Dict[str, str]], score_threshold: int = 70) -> Dict[str, int]:
    total = len(matches)
    auto_matched = sum(1 for m in matches if m["score"] >= score_threshold)
    manual_required = total - auto_matched
    return {
        "total_old_urls": total,
        "auto_matched": auto_matched,
        "manual_required": manual_required,
    }


def build_gsc_map(gsc_rows: List[Dict[str, str]]) -> Dict[str, Dict[str, float]]:
    result: Dict[str, Dict[str, float]] = {}
    for row in gsc_rows:
        url = normalize_url(row["url"])
        try:
            clicks = float(row.get("clicks", 0) or 0)
        except ValueError:
            clicks = 0.0
        try:
            impressions = float(row.get("impressions", 0) or 0)
        except ValueError:
            impressions = 0.0
        try:
            position = float(row.get("position", 999) or 999)
        except ValueError:
            position = 999.0
        result[url] = {"clicks": clicks, "impressions": impressions, "position": position}
    return result


def rank_urgent_actions(matches: List[Dict[str, str]], gsc_map: Dict[str, Dict[str, float]]) -> List[Dict[str, str]]:
    ranked: List[Dict[str, str]] = []
    for item in matches:
        metrics = gsc_map.get(normalize_url(item["old_url"]), {"clicks": 0.0, "impressions": 0.0, "position": 999.0})
        position_bonus = max(0.0, 50.0 - metrics["position"])
        impact = (metrics["clicks"] * 2.0) + (metrics["impressions"] * 0.1) + position_bonus
        if item["score"] < 70:
            impact += 25.0
        if not item["new_url"]:
            impact += 25.0
        ranked.append(
            {
                "old_url": item["old_url"],
                "new_url": item["new_url"],
                "score": item["score"],
                "impact": round(impact, 2),
                "reason": item["reason"],
            }
        )

    ranked.sort(key=lambda x: x["impact"], reverse=True)
    return ranked[:20]
