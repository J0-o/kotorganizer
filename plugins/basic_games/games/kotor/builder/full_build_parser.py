import argparse
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_FETCH_TIMEOUT_SECONDS = 20



@dataclass
class BuildModEntry:
    name: str
    urls: list[str]



def fetch_build_entries(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT_SECONDS) -> list[BuildModEntry]:
    return parse_markdown(fetch_text(url, timeout), url)



def fetch_text(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT_SECONDS) -> str:
    request = Request(url, headers={"User-Agent": "KOTORganizer-MO2-Builder/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except URLError as exc:
        raise RuntimeError(str(exc)) from exc



def parse_markdown(markdown_text: str, base_url: str = "") -> list[BuildModEntry]:
    links_by_mod: dict[str, list[str]] = {}
    title_by_mod: dict[str, str] = {}
    mod_order: list[str] = []
    current_mod = ""
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_mod = ""
            continue
        if stripped.startswith("### "):
            current_mod = stripped[4:].strip()
            links_by_mod.setdefault(current_mod, [])
            if current_mod not in mod_order:
                mod_order.append(current_mod)
            continue
        if not current_mod:
            continue
        markdown_links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", line)
        raw_urls = re.findall(r"(?i)(?<!\()https?://[^\s<>)]+", line)
        is_name_line = re.match(r"(?i)^\s*\**\s*(name|mod|mod\s+name)\s*:\s*", stripped) is not None
        for title, url in markdown_links:
            clean_title = _clean_link_title(title)
            if is_name_line and clean_title and not _is_generic_link_title(clean_title):
                title_by_mod[current_mod] = clean_title
            links_by_mod[current_mod].append(urljoin(base_url, url))
        links_by_mod[current_mod].extend(raw_urls)

    entries: list[BuildModEntry] = []
    for mod_name in mod_order:
        links = _unique_urls(links_by_mod.get(mod_name, []))
        if not links:
            continue
        base_name = title_by_mod.get(mod_name, "") or mod_name
        for index, url in enumerate(links):
            name = base_name
            if index == 1:
                name = f"{name} [Patch]"
            elif index > 1:
                name = f"{name} [Patch{index}]"
            entries.append(BuildModEntry(name=name, urls=[url]))
    return entries



def canonical_url(url: str) -> str:
    raw = html.unescape(url.strip().strip('"').strip("'"))
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = "https"
    path = unquote(parsed.path).rstrip("/")
    if not path:
        path = "/"

    if host.endswith("nexusmods.com"):
        return f"{scheme}://{host}{path}"
    if host.endswith("mega.nz"):
        fragment = parsed.fragment.strip()
        return f"{scheme}://{host}{path}#{fragment}" if fragment else f"{scheme}://{host}{path}"

    return f"{scheme}://{host}{path}"



def _unique_urls(urls) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        canonical = canonical_url(url)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        ordered.append(canonical)
    return ordered



def _clean_link_title(value: str) -> str:
    text = html.unescape(value).strip()
    text = re.sub(r"[*_`]+", "", text)
    return " ".join(text.split())



def _is_generic_link_title(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return normalized in {
        "main",
        "main file",
        "mod",
        "download",
        "link",
        "mirror",
        "patch",
        "patches",
        "compatibility patch",
        "compatibility patches",
    }



def _entries_payload(entries: list[BuildModEntry]) -> list[dict]:
    return [asdict(entry) for entry in entries]



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch or parse a KOTOR full-build markdown file to JSON.")
    parser.add_argument("source", help="Markdown URL or local markdown file path.")
    parser.add_argument("-o", "--output", help="Write JSON to this path instead of stdout.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_FETCH_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    if re.match(r"(?i)^https?://", args.source):
        entries = fetch_build_entries(args.source, args.timeout)
        source = args.source
    else:
        source_path = Path(args.source)
        source = source_path.resolve().as_uri()
        entries = parse_markdown(source_path.read_text(encoding="utf-8"), source)

    payload = {
        "source": source,
        "mods": _entries_payload(entries),
    }
    text = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
