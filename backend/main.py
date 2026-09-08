from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import re
import os
from datetime import datetime
from urllib.parse import quote

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

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


@app.get("/health")
def health():
    return {
        "status": "ok",
        "message": "Research Finder работает!"
    }


def clean_text(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r"\s+", " ", str(text))
    return text.strip()


def clean_line(text: str) -> str:
    text = clean_text(text)
    text = text.strip("•|")
    return text.strip()


def make_keywords(query: str):
    words = re.findall(
        r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9]+",
        query.lower()
    )

    stop_words = {
        "і", "й", "та", "або", "в", "у", "на", "до",
        "з", "із", "за", "для", "про", "по", "як", "що",
        "the", "and", "of", "to", "in", "on", "for",
        "a", "an"
    }

    return [
        word
        for word in words
        if word not in stop_words and len(word) > 2
    ]


def relevance_score(title: str, snippet: str, query: str) -> int:

    text = f"{title} {snippet}".lower()
    keywords = make_keywords(query)

    if not keywords:
        return 0

    score = 0

    for keyword in keywords:

        if keyword in title.lower():
            score += 2

        elif keyword in text:
            score += 1

    max_score = len(keywords) * 2

    if max_score == 0:
        return 0

    return int(
        min(
            100,
            (score / max_score) * 100
        )
    )


def extract_year(text: str):

    if not text:
        return None

    matches = re.findall(
        r"\b(19\d{2}|20\d{2})\b",
        text
    )

    if not matches:
        return None

    for year in matches:

        value = int(year)

        if 1900 <= value <= datetime.now().year:
            return value

    return None


def year_is_valid(year, year_from, year_to):

    if year_from is None and year_to is None:
        return True

    if year is None:
        return False

    if year_from is not None and year < year_from:
        return False

    if year_to is not None and year > year_to:
        return False

    return True


def format_authors(authors):

    if not authors:
        return ""

    result = []
    seen = set()

    for author in authors:

        author = clean_text(author)

        if not author:
            continue

        author_key = author.lower()

        if author_key in seen:
            continue

        seen.add(author_key)
        result.append(author)

    return ", ".join(result)


def format_author_name(name: str):

    name = clean_text(name)

    if not name:
        return ""

    return re.sub(
        r"\s+",
        " ",
        name
    )


def extract_journal_from_summary(summary: str):

    if not summary:
        return ""

    summary = clean_text(summary)
    parts = summary.split(" - ")

    if len(parts) < 2:
        return ""

    journal = parts[-1].strip()

    if "…" in journal or "..." in journal:
        return ""

    return journal


def extract_doi(text: str):

    if not text:
        return ""

    patterns = [
        r"(?:https?://doi\.org/|https?://dx\.doi\.org/|doi:\s*)"
        r"(10\.\d{4,9}/[-._;()/:A-Z0-9]+)",

        r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if not match:
            continue

        doi = clean_text(
            match.group(1)
        )

        doi = doi.rstrip(
            ".,;:)]}>\"'"
        )

        if is_valid_doi(doi):
            return f"https://doi.org/{doi}"

    return ""


def normalize_doi(doi: str):

    if not doi:
        return ""

    doi = clean_text(doi)

    doi = re.sub(
        r"^https?://doi\.org/",
        "",
        doi,
        flags=re.IGNORECASE
    )

    doi = re.sub(
        r"^https?://dx\.doi\.org/",
        "",
        doi,
        flags=re.IGNORECASE
    )

    doi = re.sub(
        r"^doi:\s*",
        "",
        doi,
        flags=re.IGNORECASE
    )

    return doi.rstrip(
        ".,;:)]}>\"'"
    ).strip()


def is_valid_doi(doi: str):

    doi_value = normalize_doi(doi)

    if not doi_value:
        return False

    if not doi_value.startswith("10."):
        return False

    if "/" not in doi_value:
        return False

    prefix, suffix = doi_value.split(
        "/",
        1
    )

    if not re.fullmatch(
        r"10\.\d{4,9}",
        prefix,
        re.IGNORECASE
    ):
        return False

    if len(suffix) < 4:
        return False

    if suffix.endswith(
        (
            "-",
            "_",
            ".",
            ":",
            ";",
            "(",
            "/"
        )
    ):
        return False

    if "…" in suffix or "..." in suffix:
        return False

    if not re.search(
        r"[A-Za-z0-9]",
        suffix
    ):
        return False

    return True


def extract_volume(text: str):

    if not text:
        return ""

    patterns = [
        r"\bТом\s+([0-9]+(?:\s*\([0-9]+\))?)",
        r"\bТ\.\s*([0-9]+(?:\s*\([0-9]+\))?)",
        r"\bVol\.\s*([0-9]+)"
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


def extract_issue(text: str):

    if not text:
        return ""

    patterns = [
        r"\b№\s*([0-9]+(?:\([0-9]+\))?)",
        r"\bNo\.\s*([0-9]+(?:\([0-9]+\))?)",
        r"\bIssue\s*([0-9]+(?:\([0-9]+\))?)"
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


def extract_pages(text: str):

    if not text:
        return ""

    patterns = [
        r"(?:С\.|Стор\.|Сторінки|Pages?|Pp?\.)\s*"
        r"([0-9]+)\s*[-–—]\s*([0-9]+)",

        r"(?:С\.|Стор\.|Сторінки|Pages?|Pp?\.)\s*"
        r"([0-9]+)"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            if len(match.groups()) == 2:

                return (
                    f"{match.group(1)}–"
                    f"{match.group(2)}"
                )

            return clean_text(
                match.group(1)
            )

    return ""


def extract_bibliographic_details(text: str):

    result = {
        "journal": "",
        "series": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "year": None
    }

    if not text:
        return result

    text = clean_text(text)

    result["year"] = extract_year(text)
    result["doi"] = extract_doi(text)
    result["volume"] = extract_volume(text)
    result["issue"] = extract_issue(text)
    result["pages"] = extract_pages(text)

    journal_match = re.search(
        r"(Вчені записки[^.]+)",
        text,
        re.IGNORECASE
    )

    if journal_match:

        journal = clean_text(
            journal_match.group(1)
        )

        if (
            "…" not in journal
            and "..." not in journal
        ):
            result["journal"] = journal

    series_match = re.search(
        r"Серія\s*:\s*([^.\n]+)",
        text,
        re.IGNORECASE
    )

    if series_match:

        result["series"] = clean_text(
            series_match.group(1)
        )

    return result


def parse_crossref_metadata(doi: str):

    result = {
        "type": "",
        "journal": "",
        "series": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "year": None,
        "authors": [],
        "title": "",
        "book_title": "",
        "publisher": ""
    }

    doi_value = normalize_doi(doi)

    if not is_valid_doi(doi_value):
        return result

    try:

        encoded_doi = quote(
            doi_value,
            safe=""
        )

        url = (
            "https://api.crossref.org/v1/works/"
            f"{encoded_doi}"
        )

        response = httpx.get(
            url,
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent":
                "Research-Finder/1.0 "
                "(academic-source-search)"
            }
        )

        if response.status_code != 200:
            return result

        data = response.json()

        message = data.get(
            "message",
            {}
        )

        if not isinstance(message, dict):
            return result

        result["type"] = clean_text(
            message.get(
                "type",
                ""
            )
        ).lower()

        returned_doi = clean_text(
            message.get(
                "DOI",
                ""
            )
        )

        if is_valid_doi(returned_doi):

            result["doi"] = (
                f"https://doi.org/"
                f"{returned_doi}"
            )

        titles = message.get(
            "title",
            []
        )

        if (
            isinstance(titles, list)
            and titles
        ):

            title = clean_text(
                titles[0]
            )

            if (
                title
                and "…" not in title
                and "..." not in title
            ):
                result["title"] = title

        authors = message.get(
            "author",
            []
        )

        if isinstance(authors, list):

            for author in authors:

                if not isinstance(
                    author,
                    dict
                ):
                    continue

                given = clean_text(
                    author.get(
                        "given",
                        ""
                    )
                )

                family = clean_text(
                    author.get(
                        "family",
                        ""
                    )
                )

                name = " ".join(
                    part
                    for part in [
                        given,
                        family
                    ]
                    if part
                )

                if name:
                    result["authors"].append(
                        name
                    )

        containers = message.get(
            "container-title",
            []
        )

        if (
            isinstance(containers, list)
            and containers
        ):

            container_title = clean_text(
                containers[0]
            )

            if container_title:

                result["journal"] = (
                    container_title
                )

                if result["type"] in {
                    "book-chapter",
                    "book-section"
                }:

                    result["book_title"] = (
                        container_title
                    )

        result["volume"] = clean_text(
            message.get(
                "volume",
                ""
            )
        )

        result["issue"] = clean_text(
            message.get(
                "issue",
                ""
            )
        )

        first_page = clean_text(
            message.get(
                "first-page",
                ""
            )
        )

        last_page = clean_text(
            message.get(
                "last-page",
                ""
            )
        )

        page_value = clean_text(
            message.get(
                "page",
                ""
            )
        )

        if (
            first_page
            and last_page
        ):

            result["pages"] = (
                f"{first_page}–"
                f"{last_page}"
            )

        elif page_value:

            result["pages"] = (
                page_value
                .replace("--", "–")
                .replace("-", "–")
            )

        elif first_page:

            result["pages"] = first_page

        date_candidates = [
            message.get(
                "published-print",
                {}
            ),
            message.get(
                "published",
                {}
            ),
            message.get(
                "published-online",
                {}
            ),
            message.get(
                "issued",
                {}
            )
        ]

        for date_data in date_candidates:

            if not isinstance(
                date_data,
                dict
            ):
                continue

            date_parts = date_data.get(
                "date-parts",
                []
            )

            if (
                isinstance(date_parts, list)
                and date_parts
                and isinstance(
                    date_parts[0],
                    list
                )
                and date_parts[0]
            ):

                try:

                    value = int(
                        date_parts[0][0]
                    )

                    if (
                        1900
                        <= value
                        <= datetime.now().year
                    ):

                        result["year"] = value
                        break

                except (
                    ValueError,
                    TypeError
                ):
                    pass

        return result

    except Exception as e:

        print(
            "CROSSREF ERROR:",
            repr(e)
        )

        return result


def parse_pdf_metadata(url: str):

    result = {
        "journal": "",
        "series": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "year": None,
        "authors": [],
        "title": ""
    }

    if PdfReader is None:
        return result

    try:

        response = httpx.get(
            url,
            timeout=15,
            follow_redirects=True
        )

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if (
            response.status_code != 200
            or "pdf" not in content_type
        ):
            return result

        temp_path = "/tmp/research_finder.pdf"

        with open(
            temp_path,
            "wb"
        ) as file:

            file.write(
                response.content
            )

        reader = PdfReader(
            temp_path
        )

        pages_to_read = min(
            2,
            len(reader.pages)
        )

        text_parts = []

        for index in range(
            pages_to_read
        ):

            page_text = (
                reader.pages[index].extract_text()
                or ""
            )

            text_parts.append(
                page_text
            )

        full_text = "\n".join(
            text_parts
        )

        lines = [
            clean_line(line)
            for line in full_text.splitlines()
            if clean_line(line)
        ]

        details = extract_bibliographic_details(
            full_text
        )

        result.update(
            details
        )

        for line in lines:

            if (
                "Вчені записки" in line
                and "…" not in line
                and "..." not in line
            ):

                result["journal"] = line
                break

        for line in lines:

            if "Серія:" in line:

                series_match = re.search(
                    r"Серія:\s*(.+)",
                    line
                )

                if series_match:

                    result["series"] = clean_text(
                        series_match.group(1)
                    )

                break

        result["doi"] = (
            extract_doi(full_text)
            or result["doi"]
        )

        result["year"] = (
            extract_year(full_text)
            or result["year"]
        )

        explicit_pages = extract_pages(
            full_text
        )

        if explicit_pages:

            result["pages"] = explicit_pages

        for line in lines:

            upper_line = line.upper()

            if (
                "ВПЛИВ СОЦІАЛЬНИХ МЕРЕЖ"
                in upper_line
            ):

                result["title"] = line
                break

            if (
                "THE IMPACT OF SOCIAL"
                in upper_line
            ):

                result["title"] = line
                break

        return result

    except Exception as e:

        print(
            "PDF ERROR:",
            repr(e)
        )

        return result


def parse_html_metadata(html: str):

    result = {
        "journal": "",
        "series": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "year": None,
        "authors": [],
        "title": ""
    }

    if (
        not html
        or BeautifulSoup is None
    ):
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
                    tag.get(
                        "content",
                        ""
                    )
                )

            return ""

        result["title"] = (
            meta_content(
                "citation_title"
            )
        )

        authors = soup.find_all(
            "meta",
            attrs={
                "name": "citation_author"
            }
        )

        result["authors"] = [
            clean_text(
                author.get(
                    "content",
                    ""
                )
            )
            for author in authors
            if author.get(
                "content"
            )
        ]

        result["journal"] = (
            meta_content(
                "citation_journal_title"
            )
        )

        result["year"] = extract_year(
            meta_content(
                "citation_publication_date"
            )
        )

        result["doi"] = extract_doi(
            meta_content(
                "citation_doi"
            )
        )

        first_page = meta_content(
            "citation_firstpage"
        )

        last_page = meta_content(
            "citation_lastpage"
        )

        if (
            first_page
            and last_page
        ):

            result["pages"] = (
                f"{first_page}–"
                f"{last_page}"
            )

        elif first_page:

            result["pages"] = first_page

        if not result["pages"]:

            citation_pages = meta_content(
                "citation_pages"
            )

            if citation_pages:
                result["pages"] = citation_pages

        result["volume"] = (
            meta_content(
                "citation_volume"
            )
        )

        result["issue"] = (
            meta_content(
                "citation_issue"
            )
        )

        if not result["doi"]:

            result["doi"] = extract_doi(
                html
            )

        return result

    except Exception as e:

        print(
            "HTML ERROR:",
            repr(e)
        )

        return result


def enrich_from_source(url: str):

    if not url:
        return {}

    try:

        response = httpx.get(
            url,
            timeout=15,
            follow_redirects=True,
            headers={
                "User-Agent":
                "Mozilla/5.0 "
                "Research Finder"
            }
        )

        if response.status_code != 200:
            return {}

        content_type = response.headers.get(
            "content-type",
            ""
        ).lower()

        if "pdf" in content_type:

            return parse_pdf_metadata(
                url
            )

        return parse_html_metadata(
            response.text
        )

    except Exception as e:

        print(
            "SOURCE ERROR:",
            repr(e)
        )

        return {}


def merge_metadata(original, extra):

    if not extra:
        return original

    for key in [
        "journal",
        "series",
        "volume",
        "issue",
        "pages",
        "doi",
        "year",
        "title"
    ]:

        if (
            not original.get(key)
            and extra.get(key)
        ):

            original[key] = extra[key]

    if (
        not original.get("authors")
        and extra.get("authors")
    ):

        original["authors"] = (
            extra["authors"]
        )

    return original


def apply_crossref_metadata(item):

    doi = item.get(
        "doi",
        ""
    )

    if not is_valid_doi(doi):
        return item

    crossref = parse_crossref_metadata(
        doi
    )

    if not crossref:
        return item

    if (
        not item.get("publication_type")
        and crossref.get("type")
    ):

        item["publication_type"] = (
            crossref["type"]
        )

    if (
        not item.get("title")
        and crossref.get("title")
    ):

        item["title"] = (
            crossref["title"]
        )

    if (
        not item.get("authors")
        and crossref.get("authors")
    ):

        item["authors"] = (
            crossref["authors"]
        )

    if (
        not item.get("doi")
        and crossref.get("doi")
    ):

        item["doi"] = (
            crossref["doi"]
        )

    if (
        not item.get("year")
        and crossref.get("year")
    ):

        item["year"] = (
            crossref["year"]
        )

    publication_type = (
        crossref.get(
            "type",
            ""
        )
    )

    if publication_type in {
        "book-chapter",
        "book-section"
    }:

        if (
            not item.get("book_title")
            and crossref.get("book_title")
        ):

            item["book_title"] = (
                crossref["book_title"]
            )

    else:

        if (
            not item.get("journal")
            and crossref.get("journal")
        ):

            item["journal"] = (
                crossref["journal"]
            )

        if (
            not item.get("volume")
            and crossref.get("volume")
        ):

            item["volume"] = (
                crossref["volume"]
            )

        if (
            not item.get("issue")
            and crossref.get("issue")
        ):

            item["issue"] = (
                crossref["issue"]
            )

    if (
        not item.get("pages")
        and crossref.get("pages")
    ):

        item["pages"] = (
            crossref["pages"]
        )

    return item


def format_bibliography(item):

    authors = item.get(
        "authors",
        []
    )

    author_text = format_authors(
        authors
    )

    title = clean_text(
        item.get(
            "title",
            ""
        )
    )

    publication_type = clean_text(
        item.get(
            "publication_type",
            ""
        )
    ).lower()

    journal = clean_text(
        item.get(
            "journal",
            ""
        )
    )

    series = clean_text(
        item.get(
            "series",
            ""
        )
    )

    book_title = clean_text(
        item.get(
            "book_title",
            ""
        )
    )

    year = item.get(
        "year"
    )

    volume = clean_text(
        item.get(
            "volume",
            ""
        )
    )

    issue = clean_text(
        item.get(
            "issue",
            ""
        )
    )

    pages = clean_text(
        item.get(
            "pages",
            ""
        )
    )

    doi = clean_text(
        item.get(
            "doi",
            ""
        )
    )

    if not is_valid_doi(doi):
        doi = ""

    url = clean_text(
        item.get(
            "url",
            ""
        )
    )

    parts = []

    if author_text:
        parts.append(
            f"{author_text}."
        )

    if title:
        parts.append(
            f"{title}."
        )

    if publication_type in {
        "book-chapter",
        "book-section"
    }:

        book_part = ""

        if book_title:
            book_part = (
                f"In: {book_title}"
            )

        if year:

            if book_part:
                book_part += (
                    f". {year}"
                )
            else:
                book_part = str(
                    year
                )

        if pages:

            if book_part:
                book_part += (
                    f". P. {pages}"
                )
            else:
                book_part = (
                    f"P. {pages}"
                )

        if book_part:
            parts.append(
                f"{book_part}."
            )

    elif journal:

        journal_part = journal

        if series:
            journal_part += (
                f". Серія: {series}"
            )

        if year:
            journal_part += (
                f". {year}"
            )

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

    if doi:

        parts.append(
            f"DOI: {doi}."
        )

    if url:

        parts.append(
            f"URL: {url}"
        )

    return " ".join(
        parts
    )


async def search_google_scholar(
    query: str,
    year_from=None,
    year_to=None
):

    api_key = os.getenv(
        "SERPAPI_KEY"
    )

    if not api_key:

        print(
            "SERPAPI ERROR: SERPAPI_KEY is not set"
        )

        return []

    params = {
        "engine": "google_scholar",
        "q": query,
        "api_key": api_key,
        "hl": "uk"
    }

    if year_from is not None:
        params["as_ylo"] = year_from

    if year_to is not None:
        params["as_yhi"] = year_to

    try:

        async with httpx.AsyncClient(
            timeout=30
        ) as client:

            response = await client.get(
                "https://serpapi.com/search.json",
                params=params
            )

            response.raise_for_status()

            data = response.json()

        if data.get("error"):

            print(
                "SERPAPI ERROR:",
                data.get("error")
            )

            return []

        organic_results = data.get(
            "organic_results",
            []
        )

        print(
            "SERPAPI RESULTS:",
            len(organic_results)
        )

        results = []

        for item in organic_results:

            try:

                title = clean_text(
                    item.get(
                        "title",
                        ""
                    )
                )

                link = item.get(
                    "link",
                    ""
                )

                snippet = clean_text(
                    item.get(
                        "snippet",
                        ""
                    )
                )

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

                year = extract_year(
                    summary
                )

                if not year:
                    year = extract_year(
                        snippet
                    )

                if not year:
                    year = extract_year(
                        title
                    )

                if not year_is_valid(
                    year,
                    year_from,
                    year_to
                ):
                    continue

                authors = []

                authors_data = (
                    publication_info.get(
                        "authors",
                        []
                    )
                )

                if isinstance(
                    authors_data,
                    list
                ):

                    for author in authors_data:

                        if isinstance(
                            author,
                            dict
                        ):

                            name = author.get(
                                "name",
                                ""
                            )

                        else:

                            name = str(
                                author
                            )

                        name = format_author_name(
                            name
                        )

                        if name:
                            authors.append(
                                name
                            )

                journal = (
                    extract_journal_from_summary(
                        summary
                    )
                )

                doi = extract_doi(
                    summary
                )

                if not doi:
                    doi = extract_doi(
                        snippet
                    )

                item_data = {
                    "title": title,
                    "url": link,
                    "snippet": snippet,
                    "authors": authors,
                    "journal": journal,
                    "series": "",
                    "volume": "",
                    "issue": "",
                    "pages": "",
                    "doi": doi,
                    "year": year,
                    "publication_type": "",
                    "book_title": "",
                    "source": "Google Scholar",
                    "relevance": relevance_score(
                        title,
                        snippet,
                        query
                    )
                }

                if link:

                    try:

                        extra = enrich_from_source(
                            link
                        )

                        item_data = merge_metadata(
                            item_data,
                            extra
                        )

                    except Exception as e:

                        print(
                            "ENRICH ERROR:",
                            repr(e)
                        )

                if item_data.get("doi"):

                    try:

                        item_data = (
                            apply_crossref_metadata(
                                item_data
                            )
                        )

                    except Exception as e:

                        print(
                            "CROSSREF APPLY ERROR:",
                            repr(e)
                        )

                item_data["relevance"] = (
                    relevance_score(
                        item_data.get(
                            "title",
                            ""
                        ),
                        item_data.get(
                            "snippet",
                            ""
                        ),
                        query
                    )
                )

                results.append(
                    item_data
                )

            except Exception as e:

                print(
                    "RESULT PROCESSING ERROR:",
                    repr(e)
                )

                continue

        return results

    except Exception as e:

        print(
            "SERPAPI ERROR:",
            repr(e)
        )

        return []


def deduplicate_results(results):

    unique = {}

    for item in results:

        title = clean_text(
            item.get(
                "title",
                ""
            )
        ).lower()

        doi = clean_text(
            item.get(
                "doi",
                ""
            )
        ).lower()

        url = clean_text(
            item.get(
                "url",
                ""
            )
        ).lower()

        if doi and is_valid_doi(doi):

            key = (
                "doi:",
                normalize_doi(doi)
            )

        elif title:

            key = (
                "title:",
                re.sub(
                    r"[^a-zа-яіїєґ0-9]+",
                    "",
                    title
                )
            )

        else:

            key = (
                "url:",
                url
            )

        if key not in unique:

            unique[key] = item

        else:

            current = unique[key]

            current_data = sum(
                bool(
                    current.get(field)
                )
                for field in [
                    "authors",
                    "journal",
                    "volume",
                    "issue",
                    "pages",
                    "doi"
                ]
            )

            new_data = sum(
                bool(
                    item.get(field)
                )
                for field in [
                    "authors",
                    "journal",
                    "volume",
                    "issue",
                    "pages",
                    "doi"
                ]
            )

            if new_data > current_data:
                unique[key] = item

    return list(
        unique.values()
    )


@app.post("/api/search")
async def search(
    request: SearchRequest
):

    results = await search_google_scholar(
        request.query,
        request.year_from,
        request.year_to
    )

    results = deduplicate_results(
        results
    )

    results.sort(
        key=lambda item: item.get(
            "relevance",
            0
        ),
        reverse=True
    )

    for item in results:

        item["bibliography"] = (
            format_bibliography(
                item
            )
        )

    return {
        "query": request.query,
        "total": len(results),
        "google_scholar_count": len(results),
        "results": results
    }
