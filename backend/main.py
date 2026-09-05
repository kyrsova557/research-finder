from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import re
import os
import asyncio


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchRequest(BaseModel):
    query: str
    year_from: int | None = None
    year_to: int | None = None
    limit: int = 20


@app.get("/health")
def health():
    return {
        "status": "ok",
        "message": "Research Finder работает!"
    }


def clean_text(text: str):
    return re.sub(r"\s+", " ", text or "").strip()


def make_keywords(query: str):
    words = re.findall(
        r"[а-яіїєґa-z0-9]+",
        query.lower()
    )

    stop_words = {
        "і", "й", "та", "або", "для", "про",
        "на", "у", "в", "з", "до", "як",
        "the", "and", "or", "for", "of",
        "in", "on", "to", "a", "an"
    }

    return [
        word
        for word in words
        if word not in stop_words and len(word) >= 3
    ]


def relevance_score(title: str, context: str, query: str):
    keywords = make_keywords(query)

    title_lower = title.lower()
    context_lower = context.lower()

    if not keywords:
        return 0, 0

    score = 0
    title_matches = 0

    for keyword in keywords:
        if keyword in title_lower:
            score += 10
            title_matches += 1

        if keyword in context_lower:
            score += 2

    if title_matches == len(keywords):
        score += 20

    percentage = round(
        min(
            100,
            (title_matches / len(keywords)) * 100
        )
    )

    return score, percentage


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


def year_is_valid(year, year_from, year_to):
    if year is None:
        return True

    if year_from is not None and year < year_from:
        return False

    if year_to is not None and year > year_to:
        return False

    return True


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

            snippet = clean_text(
                item.get("snippet", "")
            )

            year = extract_year(summary)

            if year is None:
                year = extract_year(snippet)

            if not year_is_valid(
                year,
                year_from,
                year_to
            ):
                continue

            free_url = None

            resources = item.get(
                "resources",
                []
            )

            for resource in resources:
                resource_link = resource.get(
                    "link",
                    ""
                )

                if resource_link:
                    free_url = resource_link
                    break

            main_url = item.get(
                "link",
                ""
            )

            final_url = free_url or main_url

            if not final_url:
                continue

            score, percentage = relevance_score(
                title,
                snippet,
                query
            )

            cited_by = None

            if item.get("cited_by"):
                cited_by = item["cited_by"].get(
                    "value"
                )

            results.append({
                "title": title,
                "authors": authors,
                "year": year,
                "journal": summary,
                "abstract": snippet,
                "url": final_url,
                "found_in": "Google Scholar",
                "relevance": score,
                "match_percent": percentage,
                "cited_by": cited_by,
                "free_full_text": True
            })

        return results, None

    except Exception as e:
        return [], str(e)



def normalize_title(title: str):
    title = clean_text(
        title.lower()
    )

    title = re.sub(
        r"[^a-zа-яіїєґ0-9 ]",
        "",
        title
    )

    return title


def remove_duplicates(results):
    unique = {}

    for result in results:
        title_key = normalize_title(
            result.get("title", "")
        )

        url = result.get("url", "")

        key = title_key or url

        if not key:
            continue

        if key not in unique:
            unique[key] = result

        else:
            current = unique[key]

            if result.get(
                "relevance",
                0
            ) > current.get(
                "relevance",
                0
            ):
                unique[key] = result

    return list(unique.values())


@app.post("/api/search")
async def search_sources(
    request: SearchRequest
):
    query = request.query.strip()

    if not query:
        return {
            "results": [],
            "total": 0,
            "sources": {},
            "errors": {},
            "message": "Введіть тему пошуку."
        }

    limit = max(
        1,
        min(request.limit, 100)
    )

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={
            "User-Agent":
                "ResearchFinder/1.0"
        }
    ) as client:

        scholar_results, scholar_error = (
            await search_google_scholar(
                client,
                query,
                request.year_from,
                request.year_to,
                limit
            )
        )

        
        all_results = scholar_results

        results = remove_duplicates(
            all_results
        )

        results.sort(
            key=lambda item: (
                item.get("relevance", 0),
                item.get("match_percent", 0),
                item.get("cited_by", 0) or 0
            ),
            reverse=True
        )

        results = results[:limit]

        return {
            "results": results,
            "total": len(results),
            "sources": {
                "google_scholar": len(
                    scholar_results
                ),
                "openalex": 0
            },
            "errors": {
                "google_scholar": scholar_error,
                "openalex": None
            }
        }