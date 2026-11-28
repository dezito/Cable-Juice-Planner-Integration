"""docs/ is also a website (GitHub Pages, deploy from the branch main, folder /docs): every public page must work there
and on github.com."""
from __future__ import annotations

import pathlib
import posixpath
import re
import unicodedata

import pytest
import yaml

ROOT = pathlib.Path(__file__).parents[1]
DOCS = ROOT / "docs"
# docs/development/ is for contributors and is left out of the site (_config.yml)
PAGES = sorted(path for path in DOCS.rglob("*.md") if "development" not in path.relative_to(DOCS).parts)
LINK = re.compile(r'(?:\]\(|(?:src|href)=")([^)"\s]+)[)"\s]')


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _without_code(text: str) -> str:
    """The page without its fenced code blocks and inline code."""
    text = re.sub(r"(?ms)^\s*(```+|~~~+).*?^\s*\1\s*$", "", text)
    return re.sub(r"`[^`\n]*`", "", text)


def _anchors(text: str) -> set[str]:
    """The anchors of a page on the site: the heading ids the site's Markdown (kramdown GFM) makes, and the page's own
    <a id="..."> anchors."""
    text = re.sub(r"(?ms)^\s*(```+|~~~+).*?^\s*\1\s*$", "", text)
    seen: dict[str, int] = {}
    found = set(re.findall(r'<a id="([^"]+)"', text))
    for heading in re.findall(r"(?m)^#{1,6} +(.+?)\s*#*$", text):
        heading = re.sub(r"<[^>]+>", "", re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading))
        slug = "".join(c for c in heading.lower() if c in "- \t" or unicodedata.category(c)[0] in "LNM" or unicodedata.category(c) == "Pc")
        slug = slug.replace(" ", "-").replace("\t", "-")
        number = seen.get(slug, 0)
        seen[slug] = number + 1
        found.add(f"{slug}-{number}" if number else slug)
    return found


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.relative_to(DOCS).as_posix())
def test_every_link_and_image_stays_in_the_site_and_exists(page: pathlib.Path) -> None:
    folder = page.parent.relative_to(DOCS).as_posix()
    problems = []
    for url in LINK.findall(_without_code(_text(page))):
        if re.match(r"[a-z][a-z0-9+.-]*:", url):
            continue
        target, _, anchor = url.partition("#")
        path = posixpath.normpath(posixpath.join(folder, target)) if target else page.relative_to(DOCS).as_posix()
        if path.startswith("..") or path.split("/")[0] == "development":
            problems.append(f"{url}: not part of the site (give a file outside docs/ its full github.com address)")
        elif not (DOCS / path).exists():
            problems.append(f"{url}: no such file")
        elif anchor and path.endswith(".md") and anchor not in _anchors(_text(DOCS / path)):
            problems.append(f"{url}: no such heading")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.relative_to(DOCS).as_posix())
def test_the_site_builder_leaves_jinja_examples_and_folded_parts_as_they_are(page: pathlib.Path) -> None:
    text = _text(page)
    start, end = "<!-- {% raw %} -->", "<!-- {% endraw %} -->"
    body = text.replace(start, "").replace(end, "")
    if re.search(r"\{\{|\{%", body):
        lines = text.rstrip("\n").split("\n")
        assert lines[0].startswith("# "), "the title comes first: the site takes the page's title from it"
        assert text.count(start) == 1 and text.count(end) == 1 and lines[-1] == end, "wrap the page in raw tags inside comments"
        assert not re.search(r"\{\{|\{%", text.split(start)[0]), "no Jinja before the raw tag"
        assert "endraw" not in body
    assert not re.search(r"<details(?![^>]*markdown=\"1\")[^>]*>", _without_code(text)), '<details markdown="1">: else its Markdown stays text'


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.relative_to(DOCS).as_posix())
def test_a_heading_with_an_emoji_selector_has_an_anchor_without_it(page: pathlib.Path) -> None:
    """An emoji such as the clock has an invisible variation selector, which the site keeps in the heading\'s id and
    links leave out: the page gives the id without it itself."""
    text = re.sub(r"(?ms)^\s*(```+|~~~+).*?^\s*\1\s*$", "", _text(page))
    explicit = set(re.findall(r'<a id="([^"]+)"', text))
    # (not the title: it stays on the first line, the site takes the page's title from it, and a link goes to the page)
    for anchor in _anchors(re.sub(r'(?m)^# .*$|<a id="[^"]+"></a>', "", text)):
        if "\ufe0f" in anchor:
            assert anchor.replace("\ufe0f", "") in explicit, anchor


def test_the_images_of_the_readme_exist() -> None:
    for url in LINK.findall(_without_code(_text(ROOT / "README.md"))):
        if "images/" in url and not url.startswith("http"):
            assert url.startswith("docs/images/") and (ROOT / url).is_file(), url


def _address(path: str) -> str:
    return "/" if path == "README.md" else "/" + path.removesuffix(".md") + ".html"


def test_the_menu_has_every_page_of_the_documentation_index() -> None:
    config = yaml.safe_load(_text(DOCS / "_config.yml"))
    # a line break in the Markdown is no line break on the page, as on github.com
    assert config["kramdown"] == {"input": "GFM", "hard_wrap": False} and "development" in config["exclude"]
    menu = [item["url"] for group in yaml.safe_load(_text(DOCS / "_data" / "navigation.yml")) for item in group["pages"]]
    assert len(menu) == len(set(menu))
    pages = {_address(path.relative_to(DOCS).as_posix()) for path in PAGES}
    assert set(menu) <= pages, set(menu) - pages
    index = {
        _address(posixpath.normpath(url.partition("#")[0]))
        for url in LINK.findall(_without_code(_text(DOCS / "README.md")))
        if not re.match(r"[a-z]+:", url) and url.partition("#")[0].endswith(".md")
    }
    assert index <= set(menu), index - set(menu)
    for name in ("_layouts/default.html", "assets/site.css"):
        assert (DOCS / name).is_file(), name
