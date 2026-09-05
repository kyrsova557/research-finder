from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import re
import os
from datetime import datetime
from bs4 import BeautifulSoup

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None


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
    if not text:
        return ""

    text = text.replace("\u00ad", "")
    text = text.replace("\u00a0", " ")
    text = text.replace("\u2010", "-")
    text = text.replace("\u2011", "-")
    text = text.replace("\u2012", "–")
    text = text.replace("\u2013", "–")
    text = text.replace("\u2014", "—")

    return re.sub(r"\s+", " ", text).strip()


def clean_line(text: str):
    return clean_text(text).strip(" .,:;")


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


def format_author_name(name: str):
    """
    Приводит:
    ОВ Гречановська
    ОМ Мегем

    к:

    Гречановська О. В.
    Мегем О. М.
    """

    name = clean_text(name)

    if not name:
        return ""

    # Уже нормальный вариант
    if re.search(
        r"[А-ЯІЇЄҐ]\.\s*[А-ЯІЇЄҐ]\.",
        name
    ):
        return name

    # Формат:
    # ОВ Гречановська
    # ОМ Мегем
    match = re.fullmatch(
        r"([А-ЯІЇЄҐA-Z]{1,5})\s+(.+)",
        name
    )

    if match:

        initials = match.group(1)
        surname = match.group(2).strip()

        if re.search(
            r"[А-ЯІЇЄҐA-Z]",
            surname
        ):
            formatted_initials = " ".join(
                [
                    letter + "."
                    for letter in initials
                ]
            )

            return (
                f"{surname} "
                f"{formatted_initials}"
            )

    return name


def format_authors(authors):

    formatted = []

    for author in authors:

        author = format_author_name(author)

        if author and author not in formatted:
            formatted.append(author)

    return formatted


def is_probably_author(text: str):

    if not text:
        return False

    text = clean_text(text)

    # Гречановська О.В.
    # Мегем О.М.
    # Потапюк Л.М.
    if re.fullmatch(
        r"[А-ЯІЇЄҐA-Z][а-яіїєґa-zA-Z'-]+"
        r"\s+[А-ЯІЇЄҐA-Z]\.?\s*[А-ЯІЇЄҐA-Z]?\.?",
        text
    ):
        return True

    # ОВ Гречановська
    if re.fullmatch(
        r"[А-ЯІЇЄҐA-Z]{1,5}\s+"
        r"[А-ЯІЇЄҐA-Z][а-яіїєґa-zA-Z'-]+",
        text
    ):
        return True

    return False


def extract_journal_from_summary(summary: str, year):

    if not summary:
        return ""

    text = clean_text(summary)

    if year:

        match = re.search(
            rf"\b{year}\b",
            text
        )

        if match:
            text = text[:match.start()]

    text = text.strip(
        " ,.;:-"
    )

    # Главное:
    # никогда не показываем обрезанное название Scholar
    if "…" in text or "..." in text:
        return ""

    if not text:
        return ""

    if is_probably_author(text):
        return ""

    if " - " in text:
        text = text.split(
            " - "
        )[-1].strip()

    if is_probably_author(text):
        return ""

    if len(text) > 180:
        return ""

    return text


def extract_doi(text: str):

    if not text:
        return ""

    match = re.search(
        r"(?:https?://doi\.org/|doi:\s*)"
        r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",
        text,
        re.IGNORECASE
    )

    if match:
        doi = match.group(1).rstrip(
            ".,;)"
        )

        return doi

    return ""


def extract_volume(text: str):

    if not text:
        return ""

    match = re.search(
        r"(?:Том|Т\.|Vol\.?|Volume)"
        r"\s*([0-9]+(?:\s*\([0-9]+\))?)",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return ""


def extract_issue(text: str):

    if not text:
        return ""

    match = re.search(
        r"(?:№|No\.|Issue)"
        r"\s*([0-9]+)",
        text,
        re.IGNORECASE
    )

    if match:
        return match.group(1)

    return ""


def extract_pages(text: str):

    if not text:
        return ""

    patterns = [
        r"(?:С\.|Стор\.|Pages?|Pp?\.)\s*"
        r"([0-9]+(?:\s*[-–—]\s*[0-9]+)?)",

        r"\bpages?\s*[:.]?\s*"
        r"([0-9]+(?:\s*[-–—]\s*[0-9]+)?)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return clean_text(
                match.group(1)
            )

    return ""


def extract_bibliographic_details(text: str):

    return {
        "volume": extract_volume(text),
        "issue": extract_issue(text),
        "pages": extract_pages(text),
        "doi": extract_doi(text)
    }


def parse_pdf_metadata(pdf_bytes: bytes):

    if PdfReader is None:
        return {}

    try:

        from io import BytesIO

        reader = PdfReader(
            BytesIO(pdf_bytes)
        )

        if not reader.pages:
            return {}

        # Берём первые страницы.
        # Именно там обычно находятся:
        # журнал
        # том
        # номер
        # DOI
        # авторы
        # название статьи
        first_pages = []

        for page in reader.pages[:2]:

            try:
                text = page.extract_text() or ""

                if text:
                    first_pages.append(text)

            except Exception:
                pass

        if not first_pages:
            return {}

        full_text = "\n".join(
            first_pages
        )

        lines = [
            clean_line(line)
            for line in full_text.splitlines()
            if clean_line(line)
        ]

        result = {
            "journal": "",
            "series": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "doi": "",
            "authors": [],
            "title": "",
        }

        # --------------------------------
        # ЖУРНАЛ
        # --------------------------------

        for line in lines:

            if (
                "Вчені записки" in line
                and len(line) < 250
            ):
                result["journal"] = line
                break

        # --------------------------------
        # СЕРИЯ
        # --------------------------------

        for line in lines:

            if "Серія:" in line:

                series_match = re.search(
                    r"Серія:\s*(.+)",
                    line
                )

                if series_match:
                    result["series"] = (
                        series_match.group(1)
                        .strip()
                    )

                break

        # --------------------------------
        # ТОМ
        # --------------------------------

        for line in lines:

            volume = extract_volume(line)

            if volume:
                result["volume"] = volume
                break

        # --------------------------------
        # НОМЕР
        # --------------------------------

        for line in lines:

            issue = extract_issue(line)

            if issue:
                result["issue"] = issue
                break

        # --------------------------------
        # DOI
        # --------------------------------

        result["doi"] = extract_doi(
            full_text
        )

        # --------------------------------
        # ГОД
        # --------------------------------

        year = extract_year(
            full_text[:2500]
        )

        result["year"] = year

        # --------------------------------
        # СТРАНИЦЫ PDF
        # --------------------------------

        first_page_number = None

        # Часто номер страницы стоит
        # отдельной строкой в начале PDF
        for line in lines[:15]:

            if re.fullmatch(
                r"\d{1,4}",
                line
            ):

                number = int(line)

                if 1 <= number <= 10000:
                    first_page_number = number
                    break

        if first_page_number is not None:

            total_pages = len(
                reader.pages
            )

            if total_pages > 1:

                last_page = (
                    first_page_number
                    + total_pages
                    - 1
                )

                result["pages"] = (
                    f"{first_page_number}–"
                    f"{last_page}"
                )

        # Если в тексте явно указаны страницы
        explicit_pages = extract_pages(
            full_text
        )

        if explicit_pages:
            result["pages"] = explicit_pages

        # --------------------------------
        # АВТОРЫ
        # --------------------------------

        doi_index = -1

        for i, line in enumerate(lines):

            if "DOI" in line.upper():
                doi_index = i
                break

        # Авторы обычно находятся
        # после DOI и перед названием
        search_start = (
            doi_index + 1
            if doi_index >= 0
            else 0
        )

        candidate_authors = []

        for line in lines[
            search_start:
            search_start + 15
        ]:

            if (
                "УДК" in line
                or "ВПЛИВ " in line.upper()
                or "ВПЛИВ СОЦІАЛЬНИХ" in line.upper()
            ):
                break

            # Гречановська О.В.
            # Мегем О.М.
            # Потапюк Л.М.
            author_match = re.fullmatch(
                r"([А-ЯІЇЄҐA-Z][а-яіїєґa-zA-Z'-]+)"
                r"\s+"
                r"([А-ЯІЇЄҐA-Z]\.?\s*"
                r"[А-ЯІЇЄҐA-Z]?\.?)",
                line
            )

            if author_match:

                surname = (
                    author_match.group(1)
                )

                initials = (
                    author_match.group(2)
                    .replace(" ", "")
                )

                if "." not in initials:
                    initials = " ".join(
                        [
                            char + "."
                            for char in initials
                            if char.isalpha()
                        ]
                    )

                candidate_authors.append(
                    f"{surname} {initials}"
                )

        if candidate_authors:
            result["authors"] = (
                candidate_authors
            )

        # --------------------------------
        # НАЗВАНИЕ
        # --------------------------------

        title_lines = []

        found_title = False

        for line in lines:

            upper = line.upper()

            if (
                "ВПЛИВ СОЦІАЛЬНИХ МЕРЕЖ" in upper
                or "THE IMPACT OF SOCIAL" in upper
            ):

                found_title = True

            if found_title:

                # Не захватываем аннотацию
                if (
                    line.startswith("Стаття")
                    or line.startswith("The article")
                    or line.startswith("Анотація")
                ):
                    break

                # Не добавляем служебные строки
                if (
                    "DOI" not in line.upper()
                    and "УДК" not in line.upper()
                ):
                    title_lines.append(line)

                # Обычно название занимает
                # 1–3 строки
                if len(title_lines) >= 4:
                    break

        if title_lines:

            result["title"] = clean_text(
                " ".join(title_lines)
            )

        return result

    except Exception:
        return {}


def parse_html_metadata(html: str):

    result = {
        "journal": "",
        "series": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "authors": [],
        "title": "",
        "year": None
    }

    if not html:
        return result

    try:

        soup = BeautifulSoup(
            html,
            "html.parser"
        )

        def meta_content(name):

            tag = soup.find(
                "meta",
                attrs={
                    "name": name
                }
            )

            if tag:
                return clean_text(
                    tag.get("content", "")
                )

            return ""

        result["title"] = meta_content(
            "citation_title"
        )

        result["journal"] = meta_content(
            "citation_journal_title"
        )

        result["volume"] = meta_content(
            "citation_volume"
        )

        result["issue"] = meta_content(
            "citation_issue"
        )

        result["pages"] = (
            meta_content(
                "citation_firstpage"
            )
        )

        last_page = meta_content(
            "citation_lastpage"
        )

        if (
            result["pages"]
            and last_page
        ):
            result["pages"] = (
                f"{result['pages']}–"
                f"{last_page}"
            )

        result["doi"] = (
            meta_content(
                "citation_doi"
            )
        )

        result["year"] = extract_year(
            meta_content(
                "citation_publication_date"
            )
        )

        authors = soup.find_all(
            "meta",
            attrs={
                "name": "citation_author"
            }
        )

        for author in authors:

            value = clean_text(
                author.get(
                    "content",
                    ""
                )
            )

            if value:
                result["authors"].append(
                    value
                )

    except Exception:
        pass

    return result


async def enrich_from_source(
    client,
    url
):

    if not url:
        return {}

    try:

        response = await client.get(
            url,
            headers={
                "User-Agent":
                    "Mozilla/5.0 "
                    "Research-Finder/1.0"
            },
            timeout=12
        )

        response.raise_for_status()

        content_type = (
            response.headers.get(
                "content-type",
                ""
            ).lower()
        )

        content = response.content

        # --------------------------------
        # PDF
        # --------------------------------

        if (
            "application/pdf" in content_type
            or url.lower().split("?")[0].endswith(
                ".pdf"
            )
        ):

            # Защита от огромных файлов
            if len(content) > 12 * 1024 * 1024:
                return {}

            return parse_pdf_metadata(
                content
            )

        # --------------------------------
        # HTML
        # --------------------------------

        if (
            "text/html" in content_type
            or "<html" in content[:1000].lower().decode(
                "utf-8",
                errors="ignore"
            )
        ):

            html = content.decode(
                "utf-8",
                errors="ignore"
            )

            return parse_html_metadata(
                html
            )

    except Exception:
        pass

    return {}


def merge_metadata(
    scholar_title,
    scholar_authors,
    scholar_year,
    scholar_summary,
    source_data
):

    title = scholar_title
    authors = scholar_authors[:]
    year = scholar_year

    journal = extract_journal_from_summary(
        scholar_summary,
        scholar_year
    )

    details = extract_bibliographic_details(
        scholar_summary
    )

    if source_data:

        # --------------------------------
        # TITLE
        # --------------------------------

        if source_data.get("title"):
            title = clean_text(
                source_data["title"]
            )

        # --------------------------------
        # AUTHORS
        # --------------------------------

        if source_data.get("authors"):
            authors = source_data[
                "authors"
            ]

        # --------------------------------
        # YEAR
        # --------------------------------

        if source_data.get("year"):
            year = source_data[
                "year"
            ]

        # --------------------------------
        # JOURNAL
        # --------------------------------

        if source_data.get("journal"):

            journal = clean_text(
                source_data["journal"]
            )

        # --------------------------------
        # VOLUME
        # --------------------------------

        if source_data.get("volume"):

            details["volume"] = clean_text(
                source_data["volume"]
            )

        # --------------------------------
        # ISSUE
        # --------------------------------

        if source_data.get("issue"):

            details["issue"] = clean_text(
                source_data["issue"]
            )

        # --------------------------------
        # PAGES
        # --------------------------------

        if source_data.get("pages"):

            details["pages"] = clean_text(
                source_data["pages"]
            )

        # --------------------------------
        # DOI
        # --------------------------------

        if source_data.get("doi"):

            details["doi"] = extract_doi(
                source_data["doi"]
            ) or source_data["doi"]

    authors = format_authors(
        authors
    )

    # НИКОГДА не возвращаем обрезанное название
    if (
        journal
        and (
            "…" in journal
            or "..." in journal
        )
    ):
        journal = ""

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "journal": journal,
        "volume": details["volume"],
        "issue": details["issue"],
        "pages": details["pages"],
        "doi": details["doi"]
    }


def format_bibliography(
    title,
    authors,
    year,
    journal,
    volume,
    issue,
    pages,
    doi,
    url
):

    parts = []

    if authors:
        parts.append(
            ", ".join(authors) + "."
        )

    if title:
        parts.append(
            title + "."
        )

    if journal:

        journal_part = journal

        if volume:
            journal_part += (
                f". Т. {volume}"
            )

        if issue:
            journal_part += (
                f", № {issue}"
            )

        if pages:
            journal_part += (
                f". С. {pages}"
            )

        journal_part += "."

        parts.append(
            journal_part
        )

    elif year:
        parts.append(
            f"{year}."
        )

    if journal and year:
        # Год ставим после журнала
        citation = " ".join(parts)

        # Переставляем год перед томом,
        # если есть том/номер/страницы
        if volume or issue or pages:

            citation = citation.replace(
                f". Т. {volume}",
                f". {year}. Т. {volume}",
                1
            ) if volume else citation

            if (
                not volume
                and issue
            ):
                citation = citation.replace(
                    f", № {issue}",
                    f". {year}, № {issue}",
                    1
                )

        elif f"{year}." not in citation:
            citation += f" {year}."

    else:
        citation = " ".join(parts)

    if doi:

        doi_clean = doi.strip()

        if doi_clean.startswith(
            "https://doi.org/"
        ):
            doi_url = doi_clean
        else:
            doi_url = (
                "https://doi.org/"
                + doi_clean
            )

        citation += (
            f" DOI: {doi_url}."
        )

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

    api_key = os.getenv(
        "SERPAPI_KEY"
    )

    if not api_key:
        return [], (
            "SERPAPI_KEY не встановлений"
        )

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
                item.get(
                    "title",
                    ""
                )
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
                    author.get(
                        "name",
                        ""
                    )
                )

                if name:
                    authors.append(
                        name
                    )

            snippet = clean_text(
                item.get(
                    "snippet",
                    ""
                )
            )

            year = extract_year(
                summary
            )

            if year is None:
                year = extract_year(
                    snippet
                )

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
                or item.get(
                    "link",
                    ""
                )
            )

            if not final_url:
                continue

            score, percentage = (
                relevance_score(
                    title,
                    snippet,
                    query
                )
            )

            cited_by = None

            if item.get(
                "cited_by"
            ):

                cited_by = item[
                    "cited_by"
                ].get(
                    "value"
                )

            # --------------------------------
            # ДОСТАЁМ ИНФОРМАЦИЮ ИЗ ИСТОЧНИКА
            # --------------------------------

            source_data = await enrich_from_source(
                client,
                final_url
            )

            metadata = merge_metadata(
                title,
                authors,
                year,
                summary,
                source_data
            )

            bibliography = format_bibliography(
                metadata["title"],
                metadata["authors"],
                metadata["year"],
                metadata["journal"],
                metadata["volume"],
                metadata["issue"],
                metadata["pages"],
                metadata["doi"],
                final_url
            )

            results.append({
                "title": metadata["title"],
                "authors": metadata["authors"],
                "year": metadata["year"],
                "journal": metadata["journal"],
                "volume": metadata["volume"],
                "issue": metadata["issue"],
                "pages": metadata["pages"],
                "doi": metadata["doi"],
                "abstract": snippet,
                "url": final_url,
                "bibliography": bibliography,
                "found_in": "Google Scholar",
                "relevance": score,
                "match_percent": percentage,
                "cited_by": cited_by,
                "free_full_text": bool(
                    free_url
                )
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
            result.get(
                "title",
                ""
            )
        )

        if not key:

            key = result.get(
                "url",
                ""
            )

        if key not in unique:

            unique[key] = result

    return list(
        unique.values()
    )


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
            "message":
                "Введіть тему пошуку."
        }

    limit = max(
        1,
        min(
            request.limit,
            100
        )
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
                x.get(
                    "relevance",
                    0
                ),
                x.get(
                    "match_percent",
                    0
                ),
                x.get(
                    "cited_by",
                    0
                ) or 0
            ),
            reverse=True
        )

        results = results[:limit]

        return {
            "results": results,
            "total": len(results),
            "sources": {
                "google_scholar":
                    len(results)
            },
            "errors": {
                "google_scholar":
                    error
            }
        }