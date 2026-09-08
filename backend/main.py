```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import re
import os


app = FastAPI()


# =========================================================
# CORS
# =========================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# REQUEST MODEL
# =========================================================

class SearchRequest(BaseModel):
    query: str
    year_from: int | None = None
    year_to: int | None = None
    limit: int = 20


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "message": "Research Finder работает!"
    }


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(text: str):
    return re.sub(r"\s+", " ", text or "").strip()


def make_keywords(query: str):
    words = re.findall(
        r"[а-яіїєґa-z0-9]+",
        query.lower()
    )

    return [
        word if len(word) < 6 else word[:6]
        for word in words
    ]


def relevance_score(title: str, context: str, query: str):
    keywords = make_keywords(query)

    title_lower = title.lower()
    context_lower = context.lower()

    score = 0

    for keyword in keywords:
        if keyword in title_lower:
            score += 10

        if keyword in context_lower:
            score += 2

    if keywords:
        matches = sum(
            1
            for keyword in keywords
            if keyword in title_lower
        )

        if matches == len(keywords):
            score += 20

    return score


def extract_year(text: str):
    if not text:
        return None

    match = re.search(
        r"\b(19\d{2}|20\d{2}|21\d{2})\b",
        text
    )

    if match:
        return int(match.group(1))

    return None


def extract_pages(text: str):
    if not text:
        return None

    patterns = [
        r"\b[CС]\.\s*(\d+\s*[-–—]\s*\d+)\b",
        r"\b[pP]\.\s*(\d+\s*[-–—]\s*\d+)\b",
        r"\bpages?\s+(\d+\s*[-–—]\s*\d+)\b",
        r"\b(\d+)\s*[-–—]\s*(\d+)\b"
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if match:
            if len(match.groups()) == 1:
                return clean_text(match.group(1))

            return f"{match.group(1)}–{match.group(2)}"

    return None


def normalize_doi(doi: str | None):
    if not doi:
        return None

    doi = clean_text(doi)

    doi = re.sub(
        r"^(https?://)?(dx\.)?doi\.org/",
        "",
        doi,
        flags=re.IGNORECASE
    )

    doi = doi.rstrip(".,;")

    if doi.lower().startswith("doi:"):
        doi = doi[4:].strip()

    return doi or None


def extract_doi(text: str):
    if not text:
        return None

    pattern = r"(10\.\d{4,9}/[-._;()/:a-z0-9]+)"

    match = re.search(
        pattern,
        text,
        flags=re.IGNORECASE
    )

    if not match:
        return None

    doi = normalize_doi(match.group(1))

    if not doi:
        return None

    return doi


def extract_issue(text: str):
    if not text:
        return None

    patterns = [
        r"\b№\s*(\d+)",
        r"\bNo\.?\s*(\d+)",
        r"\bissue\s*(\d+)"
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


def extract_volume(text: str):
    if not text:
        return None

    patterns = [
        r"\bТ\.\s*(\d+)",
        r"\bТом\s*(\d+)",
        r"\bVol\.?\s*(\d+)"
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


# =========================================================
# GOOGLE SCHOLAR
# =========================================================

async def search_google_scholar(
    client,
    query,
    year_from,
    year_to,
    limit
):
    api_key = os.getenv("SERPAPI_KEY")

    if not api_key:
        return [], "SERPAPI_KEY не встановлений"

    params = {
        "engine": "google_scholar",
        "q": query,
        "api_key": api_key,
        "hl": "uk",
        "num": min(limit, 20)
    }

    if year_from is not None:
        params["as_ylo"] = year_from

    if year_to is not None:
        params["as_yhi"] = year_to

    try:
        response = await client.get(
            "https://serpapi.com/search.json",
            params=params
        )

        response.raise_for_status()

        data = response.json()

        if "error" in data:
            return [], data["error"]

        results = []

        for item in data.get("organic_results", []):

            title = clean_text(
                item.get("title", "")
            )

            if not title:
                continue

            publication_info = item.get(
                "publication_info",
                {}
            )

            summary = clean_text(
                publication_info.get(
                    "summary",
                    ""
                )
            )

            snippet = clean_text(
                item.get("snippet", "")
            )

            # -------------------------------------------------
            # AUTHORS
            # -------------------------------------------------

            authors = []

            for author in publication_info.get(
                "authors",
                []
            ):
                name = clean_text(
                    author.get("name", "")
                )

                if name:
                    authors.append(name)

            # -------------------------------------------------
            # YEAR
            # -------------------------------------------------

            year = extract_year(summary)

            if year is None:
                year = extract_year(snippet)

            if year is not None:

                if (
                    year_from is not None
                    and year < year_from
                ):
                    continue

                if (
                    year_to is not None
                    and year > year_to
                ):
                    continue

            # -------------------------------------------------
            # DOI
            # -------------------------------------------------

            doi = extract_doi(
                title + " " +
                summary + " " +
                snippet
            )

            # -------------------------------------------------
            # PAGES
            # -------------------------------------------------

            pages = extract_pages(summary)

            if pages is None:
                pages = extract_pages(snippet)

            # -------------------------------------------------
            # VOLUME / ISSUE
            # -------------------------------------------------

            volume = extract_volume(summary)

            issue = extract_issue(summary)

            # -------------------------------------------------
            # JOURNAL
            # -------------------------------------------------

            journal = summary

            # Remove author/year information from obvious
            # Scholar metadata where possible.
            if " - " in journal:
                parts = journal.split(" - ")

                if len(parts) >= 2:
                    journal = parts[-1].strip()

            # -------------------------------------------------
            # CITATIONS
            # -------------------------------------------------

            cited_by = None

            if item.get("cited_by"):
                cited_by = item.get(
                    "cited_by",
                    {}
                ).get("value")

            # -------------------------------------------------
            # URL
            # -------------------------------------------------

            url = clean_text(
                item.get("link", "")
            )

            # -------------------------------------------------
            # FREE FULL TEXT
            # -------------------------------------------------
            #
            # Google Scholar often provides a direct PDF
            # under "resources". We save those links when
            # available, otherwise use the Scholar result link.
            #

            resources = item.get(
                "resources",
                []
            )

            full_text_url = None

            if resources:
                for resource in resources:

                    resource_link = clean_text(
                        resource.get(
                            "link",
                            ""
                        )
                    )

                    if resource_link:
                        full_text_url = resource_link
                        break

            if full_text_url:
                url = full_text_url

            # -------------------------------------------------
            # RESULT
            # -------------------------------------------------

            result = {
                "title": title,
                "authors": authors,
                "year": year,
                "journal": journal,
                "volume": volume,
                "issue": issue,
                "page": pages,
                "pages": pages,
                "doi": doi,
                "abstract": snippet,
                "url": url,
                "found_in": "Google Scholar",
                "relevance": relevance_score(
                    title,
                    snippet,
                    query
                ),
                "cited_by": cited_by
            }

            results.append(result)

        return results, None

    except Exception as e:
        return [], str(e)


# =========================================================
# DEDUPLICATION
# =========================================================

def normalize_title(title: str):
    title = clean_text(title).lower()

    title = re.sub(
        r"[^\wа-яіїєґ ]",
        "",
        title
    )

    return title


def deduplicate_results(results):
    unique = {}

    for result in results:

        title = normalize_title(
            result.get("title", "")
        )

        doi = normalize_doi(
            result.get("doi")
        )

        url = clean_text(
            result.get("url", "")
        ).lower()

        key = doi or title or url

        if not key:
            continue

        if key not in unique:
            unique[key] = result

    return list(unique.values())


# =========================================================
# SORTING
# =========================================================

def sort_results(results):
    results.sort(
        key=lambda item: (
            item.get("relevance", 0),
            item.get("cited_by") or 0,
            item.get("year") or 0
        ),
        reverse=True
    )

    return results


# =========================================================
# BIBLIOGRAPHY FORMAT
# =========================================================

def format_bibliography(result):
    parts = []

    authors = result.get("authors") or []

    if authors:
        parts.append(
            ", ".join(authors) + "."
        )

    title = clean_text(
        result.get("title", "")
    )

    if title:
        parts.append(
            title + "."
        )

    journal = clean_text(
        result.get("journal", "")
    )

    if journal:
        parts.append(
            journal + "."
        )

    year = result.get("year")

    if year:
        parts.append(
            str(year) + "."
        )

    volume = result.get("volume")

    if volume:
        parts.append(
            f"Т. {volume}."
        )

    issue = result.get("issue")

    if issue:
        parts.append(
            f"№ {issue}."
        )

    pages = (
        result.get("pages")
        or result.get("page")
    )

    if pages:
        parts.append(
            f"С. {pages}."
        )

    doi = normalize_doi(
        result.get("doi")
    )

    if doi:
        parts.append(
            f"DOI: https://doi.org/{doi}"
        )

    return " ".join(parts)


# =========================================================
# SEARCH API
# =========================================================

@app.post("/api/search")
async def search_sources(
    request: SearchRequest
):
    query = request.query.strip()

    if not query:
        return {
            "results": [],
            "total": 0,
            "sources": {
                "google_scholar": 0
            },
            "errors": {
                "google_scholar": "Введіть тему пошуку."
            }
        }

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent":
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140 Safari/537.36"
        }
    ) as client:

        scholar_results, scholar_error = (
            await search_google_scholar(
                client,
                query,
                request.year_from,
                request.year_to,
                request.limit
            )
        )

    # ---------------------------------------------------------
    # DEDUPLICATE
    # ---------------------------------------------------------

    results = deduplicate_results(
        scholar_results
    )

    # ---------------------------------------------------------
    # SORT
    # ---------------------------------------------------------

    results = sort_results(
        results
    )

    # ---------------------------------------------------------
    # LIMIT
    # ---------------------------------------------------------

    results = results[
        :request.limit
    ]

    # ---------------------------------------------------------
    # ADD BIBLIOGRAPHY
    # ---------------------------------------------------------

    for result in results:
        result["bibliography"] = (
            format_bibliography(result)
        )

    # ---------------------------------------------------------
    # RESPONSE
    # ---------------------------------------------------------

    return {
        "results": results,
        "total": len(results),
        "sources": {
            "google_scholar": len(
                scholar_results
            )
        },
        "errors": {
            "google_scholar": scholar_error
        }
    }
```
