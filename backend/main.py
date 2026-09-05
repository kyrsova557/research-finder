from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import re
import os
from datetime import datetime


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

    if not keywords:
        return 0, 0

    title_lower = title.lower()
    context_lower = context.lower()

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


def is_probably_author_text(text: str):
    if not text:
        return False

    text = clean_text(text)

    parts = [
        part.strip()
        for part in text.split(",")
        if part.strip()
    ]

    if not parts:
        return False

    author_like = 0

    for part in parts:
        # Например:
        # ОВ Гречановська
        # ОМ Мегем
        # СА Гаркуша
        # НЯ Лепіш
        if re.fullmatch(
            r"[А-ЯІЇЄҐA-ZЁЙ]{1,5}\s+[А-ЯІЇЄҐA-ZЁЙ][а-яіїєґa-zё]+",
            part
        ):
            author_like += 1

    return author_like == len(parts)


def extract_journal(summary: str, year):
    """
    Пытаемся получить название журнала из publication_info.summary.

    Google Scholar иногда возвращает:
    'Название журнала …. 2023'

    В таком случае НЕ показываем обрезанное название с '…'.

    Также отбрасываем случаи, когда Scholar фактически
    подставил туда список авторов.
    """

    if not summary:
        return ""

    text = clean_text(summary)

    if not text:
        return ""

    # Убираем всё после года, если оно есть
    if year:
        year_match = re.search(
            rf"\b{year}\b",
            text
        )

        if year_match:
            before_year = text[:year_match.start()].strip()
        else:
            before_year = text
    else:
        before_year = text

    before_year = before_year.rstrip(
        " ,.;:-"
    )

    # Если Google Scholar сам обрезал название
    if "…" in before_year or "..." in before_year:
        return ""

    if not before_year:
        return ""

    # Если это просто авторы — не считаем их журналом
    if is_probably_author_text(before_year):
        return ""

    # Иногда Scholar возвращает авторов + журнал.
    # Берём часть после последнего ' - '.
    if " - " in before_year:
        candidate = before_year.split(
            " - "
        )[-1].strip()
    else:
        candidate = before_year

    candidate = candidate.strip(
        " ,.;:-"
    )

    if not candidate:
        return ""

    if "…" in candidate or "..." in candidate:
        return ""

    if is_probably_author_text(candidate):
        return ""

    # Слишком длинная строка почти наверняка не журнал
    if len(candidate) > 180:
        return ""

    return candidate


def extract_bibliographic_details(text: str, year):
    """
    Пытаемся дополнительно достать:
    - том
    - номер
    - страницы

    из уже имеющейся библиографической строки.

    Ничего не выдумываем.
    """

    if not text:
        return {
            "volume": "",
            "issue": "",
            "pages": ""
        }

    text = clean_text(text)

    volume = ""
    issue = ""
    pages = ""

    volume_match = re.search(
        r"(?:Т\.|Том|Vol\.?)\s*"
        r"([0-9]+(?:\s*\([0-9]+\))?)",
        text,
        re.IGNORECASE
    )

    if volume_match:
        volume = volume_match.group(1)

    issue_match = re.search(
        r"(?:№|No\.|Issue)\s*"
        r"([0-9]+)",
        text,
        re.IGNORECASE
    )

    if issue_match:
        issue = issue_match.group(1)

    pages_match = re.search(
        r"(?:С\.|Стор\.|Pages?|Pp?\.)\s*"
        r"([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",
        text,
        re.IGNORECASE
    )

    if pages_match:
        pages = pages_match.group(1)

    return {
        "volume": volume,
        "issue": issue,
        "pages": pages
    }


def format_bibliography(
    title,
    authors,
    year,
    summary,
    url
):
    title = clean_text(title)

    authors = [
        clean_text(a)
        for a in authors
        if clean_text(a)
    ]

    journal = extract_journal(
        summary,
        year
    )

    details = extract_bibliographic_details(
        summary,
        year
    )

    parts = []

    if authors:
        parts.append(
            ", ".join(authors) + "."
        )

    if title:
        parts.append(
            title + "."
        )

    if journal and journal.lower() not in title.lower():
        parts.append(
            journal + "."
        )

    if year:
        parts.append(
            str(year) + "."
        )

    if details["volume"]:
        parts.append(
            "Т. " + details["volume"] + "."
        )

    if details["issue"]:
        parts.append(
            "№ " + details["issue"] + "."
        )

    if details["pages"]:
        parts.append(
            "С. " + details["pages"] + "."
        )

    citation = " ".join(parts)

    if url:
        date = datetime.now().strftime(
            "%d.%m.%Y"
        )

        citation += (
            f" URL: {url} "
            f"(дата звернення: {date})."
        )

    return citation


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

        for item in data.get(
            "organic_results",
            []
        ):

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

            resources = item.get(
                "resources",
                []
            )

            free_url = None

            for resource in resources:
                link = resource.get(
                    "link",
                    ""
                )

                if link:
                    free_url = link
                    break

            final_url = (
                free_url
                or item.get("link", "")
            )

            if not final_url:
                continue

            score, percentage = relevance_score(
                title,
                snippet,
                query
            )

            cited_by = None

            if item.get("cited_by"):
                cited_by = item[
                    "cited_by"
                ].get("value")

            bibliography = format_bibliography(
                title,
                authors,
                year,
                summary,
                final_url
            )

            results.append({
                "title": title,
                "authors": authors,
                "year": year,
                "journal": extract_journal(
                    summary,
                    year
                ),
                "abstract": snippet,
                "url": final_url,
                "bibliography": bibliography,
                "found_in": "Google Scholar",
                "relevance": score,
                "match_percent": percentage,
                "cited_by": cited_by,
                "free_full_text": True
            })

        return results, None

    except Exception as e:
        return [], str(e)


def normalize_title(title):
    title = clean_text(
        title.lower()
    )

    return re.sub(
        r"[^a-zа-яіїєґ0-9 ]",
        "",
        title
    )


def remove_duplicates(results):
    unique = {}

    for result in results:

        key = normalize_title(
            result.get("title", "")
        )

        if not key:
            key = result.get(
                "url",
                ""
            )

        if key not in unique:
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
        follow_redirects=True
    ) as client:

        results, error = (
            await search_google_scholar(
                client,
                query,
                request.year_from,
                request.year_to,
                limit
            )
        )

        results = remove_duplicates(
            results
        )

        results.sort(
            key=lambda x: (
                x.get("relevance", 0),
                x.get("match_percent", 0),
                x.get("cited_by", 0) or 0
            ),
            reverse=True
        )

        results = results[:limit]

        return {
            "results": results,
            "total": len(results),
            "sources": {
                "google_scholar": len(results)
            },
            "errors": {
                "google_scholar": error
            }
        }