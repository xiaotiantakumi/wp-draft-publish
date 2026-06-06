#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.31", "markdown>=3.5", "python-frontmatter>=1.0", "beautifulsoup4>=4.12"]
# ///
"""Publish Markdown / code files to WordPress as drafts via the REST API.

Auth uses a WordPress Application Password (HTTP Basic auth), which bypasses the
login-screen 2FA. Credentials are read from environment variables — provide them
via a .env file next to the script, by exporting them, or from any secret manager:

    uv run wp_publish.py path/to/notes/         # with a .env present
    WP_URL=... WP_USERNAME=... WP_APP_PASSWORD=... uv run wp_publish.py note.md

Required env vars:
    WP_URL           base URL incl. subdir if any, e.g. https://example.com/blog
    WP_USERNAME      WordPress login name
    WP_APP_PASSWORD  Application Password (spaces are stripped automatically)

Features:
  * .md / .markdown -> Markdown (YAML frontmatter + body).
  * any other file  -> whole file as a single code block (lang from extension).
  * Local images (Markdown ![alt](path) and Obsidian ![[file|alt]]) are uploaded
    to the media library and turned into Gutenberg image blocks.
  * Categories / tags resolved by name (created if missing) via --category/--tag
    or frontmatter `categories:` / `tags:`.
  * Featured image (eyecatch) via --featured / frontmatter `featured:` / cover.
  * After posting, fetches the draft back and prints a preview URL for checking.

No update feature by design: re-running creates a new draft.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import os
import re
import sys
from pathlib import Path

import frontmatter
import markdown
import requests
from bs4 import BeautifulSoup, NavigableString

MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}

EXT_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".php": "php", ".html": "html", ".css": "css",
    ".scss": "scss", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".sql": "sql", ".kt": "kotlin",
    ".swift": "swift", ".xml": "xml", ".txt": "text",
}

LANG_CLASS_RE = re.compile(r'class="language-([^"]+)"')

# image refs in Markdown source
OBSIDIAN_IMG_RE = re.compile(r"!\[\[([^\]\|]+?)(?:\|([^\]]*))?\]\]")
MD_IMG_RE = re.compile(r'!\[([^\]]*)\]\(\s*<?([^)\s"\'<>]+)>?(?:\s+"[^"]*")?\s*\)')


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def code_block(escaped_body: str, lang: str, style: str) -> str:
    if style == "prism":
        cls = f' class="language-{lang}"' if lang else ""
        return ("<!-- wp:html -->\n"
                f'<pre class="wp-block-code"><code{cls}>{escaped_body}</code></pre>\n'
                "<!-- /wp:html -->")
    return ("<!-- wp:code -->\n"
            f'<pre class="wp-block-code"><code>{escaped_body}</code></pre>\n'
            "<!-- /wp:code -->")


def md_to_html(body: str) -> str:
    md = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists", "attr_list"],
        output_format="html5",
    )
    return md.convert(body)


def _img_block(img, url_to_id: dict) -> str:
    src, alt = img.get("src", ""), img.get("alt", "")
    mid = url_to_id.get(src)
    if mid:
        return (f'<!-- wp:image {{"id":{mid},"sizeSlug":"large"}} -->\n'
                f'<figure class="wp-block-image size-large">'
                f'<img src="{src}" alt="{alt}" class="wp-image-{mid}"/></figure>\n'
                "<!-- /wp:image -->")
    return ("<!-- wp:image -->\n"
            f'<figure class="wp-block-image"><img src="{src}" alt="{alt}"/></figure>\n'
            "<!-- /wp:image -->")


def _list_block(el) -> str:
    ordered = el.name == "ol"
    items = [
        "<!-- wp:list-item -->\n"
        f"<li>{li.decode_contents().strip()}</li>\n"
        "<!-- /wp:list-item -->"
        for li in el.find_all("li", recursive=False)
    ]
    tag = "ol" if ordered else "ul"
    attrs = ' {"ordered":true}' if ordered else ""
    return (f"<!-- wp:list{attrs} -->\n"
            f'<{tag} class="wp-block-list">' + "".join(items) + f"</{tag}>\n"
            "<!-- /wp:list -->")


def _convert_el(el, url_to_id: dict, code_style: str) -> str:
    """Convert one top-level rendered-HTML node into Gutenberg block markup."""
    if isinstance(el, NavigableString):
        text = str(el).strip()
        return f"<!-- wp:paragraph -->\n<p>{text}</p>\n<!-- /wp:paragraph -->" if text else ""
    name = el.name
    if name == "p":
        imgs = el.find_all("img")
        if imgs and not el.get_text(strip=True):
            return "\n\n".join(_img_block(i, url_to_id) for i in imgs)
        return f"<!-- wp:paragraph -->\n{el}\n<!-- /wp:paragraph -->"
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(name[1])
        classes = el.get("class", [])
        if "wp-block-heading" not in classes:
            el["class"] = classes + ["wp-block-heading"]
        attrs = "" if level == 2 else f' {{"level":{level}}}'
        return f"<!-- wp:heading{attrs} -->\n{el}\n<!-- /wp:heading -->"
    if name in ("ul", "ol"):
        return _list_block(el)
    if name == "pre":
        code = el.find("code")
        body = code.decode_contents() if code else el.decode_contents()
        lang_match = LANG_CLASS_RE.search(str(code)) if code else None
        return code_block(body, lang_match.group(1) if lang_match else "", code_style)
    if name == "blockquote":
        inner = "".join(_convert_el(c, url_to_id, code_style) for c in el.children).strip()
        return ('<!-- wp:quote -->\n'
                f'<blockquote class="wp-block-quote">{inner}</blockquote>\n'
                '<!-- /wp:quote -->')
    if name == "img":
        return _img_block(el, url_to_id)
    if name == "table":
        return ('<!-- wp:table -->\n'
                f'<figure class="wp-block-table">{el}</figure>\n'
                '<!-- /wp:table -->')
    if name == "hr":
        return ('<!-- wp:separator -->\n'
                '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
                '<!-- /wp:separator -->')
    return str(el)


def html_to_blocks(html_str: str, url_to_id: dict, code_style: str) -> str:
    """Render markdown HTML into a clean, fully block-delimited Gutenberg document."""
    soup = BeautifulSoup(html_str, "html.parser")
    parts = (_convert_el(el, url_to_id, code_style) for el in soup.children)
    return "\n\n".join(p for p in (s.strip() for s in parts) if p)


def extract_title(meta: dict, body: str, fallback: str) -> tuple[str, str]:
    title = meta.get("title")
    if title:
        return str(title), body
    m = re.match(r"\s*#\s+(.+?)\s*(?:\n|$)", body)
    if m:
        return m.group(1).strip(), body[m.end():]
    return fallback, body


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def resolve_image(ref: str, base_dir: Path, attachments_dir: str | None) -> Path | None:
    ref = ref.strip()
    p = Path(ref)
    cands: list[Path] = []
    if p.is_absolute():
        cands.append(p)
    else:
        if attachments_dir:
            cands += [Path(attachments_dir) / ref, Path(attachments_dir) / p.name]
        cands += [base_dir / ref, base_dir / p.name]
    for c in cands:
        if c.is_file():
            return c
    name = p.name  # last resort: search by filename
    roots = [r for r in (attachments_dir, str(base_dir)) if r]
    for root in roots:
        for found in Path(root).rglob(name):
            if found.is_file():
                return found
    return None


# --------------------------------------------------------------------------- #
# WordPress client
# --------------------------------------------------------------------------- #
class WP:
    def __init__(self, base: str, user: str, app_password: str) -> None:
        self.base = base.rstrip("/")
        self.api = f"{self.base}/wp-json/wp/v2"
        self.session = requests.Session()
        self.session.auth = (user, app_password.replace(" ", ""))
        self.session.headers.update({"Accept": "application/json"})
        self._media_cache: dict[str, tuple[int, str]] = {}
        self._term_map: dict[str, dict[str, int]] = {}
        self._term_name: dict[str, dict[int, str]] = {}

    # ---- media ----
    def upload_media(self, path: Path, alt: str = "") -> tuple[int, str]:
        key = str(path.resolve())
        if key in self._media_cache:
            return self._media_cache[key]
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        r = self.session.post(
            f"{self.api}/media",
            headers={"Content-Disposition": f'attachment; filename="{path.name}"',
                     "Content-Type": mime},
            data=path.read_bytes(), timeout=180,
        )
        r.raise_for_status()
        j = r.json()
        mid, url = j["id"], j.get("source_url", "")
        if alt:
            self.session.post(f"{self.api}/media/{mid}",
                              json={"alt_text": alt, "title": alt}, timeout=30)
        self._media_cache[key] = (mid, url)
        print(f"    ↑ uploaded {path.name} -> media #{mid}")
        return mid, url

    # ---- taxonomy ----
    def _load_terms(self, tax: str) -> None:
        if tax in self._term_map:
            return
        name_to_id, id_to_name, page = {}, {}, 1
        while True:
            r = self.session.get(f"{self.api}/{tax}",
                                 params={"per_page": 100, "page": page}, timeout=30)
            if r.status_code != 200:
                break
            arr = r.json()
            for t in arr:
                name_to_id[t["name"].lower()] = t["id"]
                id_to_name[t["id"]] = t["name"]
            if len(arr) < 100:
                break
            page += 1
        self._term_map[tax] = name_to_id
        self._term_name[tax] = id_to_name

    def resolve_terms(self, names: list, tax: str) -> list[int]:
        if not names:
            return []
        self._load_terms(tax)
        name_to_id = self._term_map[tax]
        ids: list[int] = []
        for raw in names:
            n = str(raw).strip()
            if not n:
                continue
            key = n.lower()
            if key in name_to_id:
                ids.append(name_to_id[key])
                continue
            r = self.session.post(f"{self.api}/{tax}", json={"name": n}, timeout=30)
            if r.status_code in (200, 201):
                tid = r.json()["id"]
            elif r.status_code == 400 and r.json().get("code") == "term_exists":
                tid = r.json()["data"]["term_id"]
            else:
                print(f"    ! {tax} create failed '{n}': {r.status_code} {r.text[:100]}",
                      file=sys.stderr)
                continue
            name_to_id[key] = tid
            self._term_name[tax][tid] = n
            ids.append(tid)
            print(f"    + new {tax[:-3] if tax.endswith('ies') else tax.rstrip('s')}: {n} (#{tid})")
        return ids

    def term_name(self, tax: str, tid: int) -> str:
        self._load_terms(tax)
        return self._term_name[tax].get(tid, f"#{tid}")

    # ---- posts ----
    def create_post(self, payload: dict) -> requests.Response:
        return self.session.post(f"{self.api}/posts", json=payload, timeout=60)

    def get_post(self, pid: int) -> dict:
        return self.session.get(f"{self.api}/posts/{pid}",
                                params={"context": "edit"}, timeout=30).json()


# --------------------------------------------------------------------------- #
# Image upload + body rewrite
# --------------------------------------------------------------------------- #
def upload_images(body: str, base_dir: Path, wp: WP, attachments_dir: str | None):
    """Upload local images, rewrite refs to uploaded URLs. Returns
    (new_body, url_to_id, ordered_ids)."""
    url_to_id: dict[str, int] = {}
    ordered: list[int] = []

    def take(ref: str, alt: str):
        p = resolve_image(ref, base_dir, attachments_dir)
        if not p:
            print(f"    ! image not found, left as-is: {ref}", file=sys.stderr)
            return None
        mid, url = wp.upload_media(p, alt=alt)
        url_to_id[url] = mid
        if mid not in ordered:
            ordered.append(mid)
        return url

    def ob_repl(m: re.Match) -> str:
        name, alt = m.group(1).strip(), (m.group(2) or "").strip()
        if Path(name).suffix.lower() not in IMG_EXTS:
            return m.group(0)
        url = take(name, alt or Path(name).stem)
        return f"![{alt or Path(name).stem}]({url})" if url else m.group(0)

    def md_repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        if Path(src).suffix.lower() not in IMG_EXTS:
            return m.group(0)
        url = take(src, alt)
        return f"![{alt}]({url})" if url else m.group(0)

    body = OBSIDIAN_IMG_RE.sub(ob_repl, body)
    body = MD_IMG_RE.sub(md_repl, body)
    return body, url_to_id, ordered


def detect_images(body: str, base_dir: Path, attachments_dir: str | None) -> None:
    refs = []
    for m in OBSIDIAN_IMG_RE.finditer(body):
        if Path(m.group(1).strip()).suffix.lower() in IMG_EXTS:
            refs.append(m.group(1).strip())
    for m in MD_IMG_RE.finditer(body):
        src = m.group(2)
        if not src.startswith(("http://", "https://", "data:")) and Path(src).suffix.lower() in IMG_EXTS:
            refs.append(src)
    for r in refs:
        found = resolve_image(r, base_dir, attachments_dir)
        print(f"    image: {r}  ->  {'OK ' + str(found) if found else 'NOT FOUND'}")


# --------------------------------------------------------------------------- #
# Build one post
# --------------------------------------------------------------------------- #
def build_post(path: Path, wp: WP | None, args) -> dict:
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    if path.suffix.lower() in MARKDOWN_EXTS:
        post = frontmatter.loads(raw)
        meta = post.metadata
        title, body = extract_title(meta, post.content, path.stem)
        base_dir = path.parent
        url_to_id: dict[str, int] = {}
        ordered: list[int] = []
        if args.dry_run:
            detect_images(body, base_dir, args.attachments_dir)
        else:
            body, url_to_id, ordered = upload_images(body, base_dir, wp, args.attachments_dir)
        content = html_to_blocks(md_to_html(body), url_to_id, args.code_style)
    else:
        lang = args.lang or EXT_LANG.get(path.suffix.lower(), "")
        title = path.name
        content = code_block(html.escape(raw), lang, args.code_style)
        ordered = []

    if args.title:
        title = args.title

    payload: dict = {"title": title, "content": content, "status": args.status}

    cat_names = as_list(meta.get("categories")) + as_list(meta.get("category")) + (args.category or [])
    tag_names = as_list(meta.get("tags")) + as_list(meta.get("tag")) + (args.tag or [])
    excerpt = args.excerpt or meta.get("excerpt") or meta.get("description")
    slug = args.slug or meta.get("slug")
    if excerpt:
        payload["excerpt"] = str(excerpt)
    if slug:
        payload["slug"] = str(slug)

    featured_ref = args.featured or meta.get("featured") or meta.get("cover") or meta.get("thumbnail")

    if not args.dry_run:
        if cat_names:
            payload["categories"] = wp.resolve_terms(cat_names, "categories")
        if tag_names:
            payload["tags"] = wp.resolve_terms(tag_names, "tags")
        if featured_ref:
            fp = resolve_image(str(featured_ref), path.parent, args.attachments_dir)
            if fp:
                fid, _ = wp.upload_media(fp, alt=title)
                payload["featured_media"] = fid
            else:
                print(f"    ! featured image not found: {featured_ref}", file=sys.stderr)
        elif args.featured_from_first and ordered:
            payload["featured_media"] = ordered[0]
            print(f"    ★ featured = first image (media #{ordered[0]})")
    else:
        payload["_dry"] = {"categories": cat_names, "tags": tag_names,
                           "featured": str(featured_ref) if featured_ref else None,
                           "featured_from_first": args.featured_from_first}
    return payload


def report(wp: WP, pid: int) -> None:
    j = wp.get_post(pid)
    cats = [wp.term_name("categories", c) for c in j.get("categories", [])]
    tags = [wp.term_name("tags", t) for t in j.get("tags", [])]
    fm = j.get("featured_media") or 0
    print(f"      title    : {j['title']['raw']}")
    print(f"      status   : {j['status']}")
    print(f"      category : {cats or '(none)'}")
    print(f"      tags     : {tags or '(none)'}")
    print(f"      featured : {('#' + str(fm)) if fm else '(none)'}")
    print(f"      preview  : {wp.base}/?p={pid}&preview=true")
    print(f"      edit     : {wp.base}/wp-admin/post.php?post={pid}&action=edit")


# --------------------------------------------------------------------------- #
# File collection
# --------------------------------------------------------------------------- #
def collect_files(paths: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            globber = path.rglob if recursive else path.glob
            for ext in MARKDOWN_EXTS:
                files.extend(globber(f"*{ext}"))
        elif path.is_file():
            files.append(path)
        else:
            print(f"  ! not found: {p}", file=sys.stderr)
    seen, unique = set(), []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return sorted(unique, key=lambda f: str(f))


# --------------------------------------------------------------------------- #
# Credentials (.env fallback)
# --------------------------------------------------------------------------- #
def load_dotenv(explicit: str | None) -> None:
    """Fill MISSING env vars from a .env file. Real env vars (exported or from a secret manager) win.

    Lookup order: --env-file > ./.env > <script dir>/.env > <script dir>/../.env.
    Only the first existing file is used. Values are never printed.
    """
    if explicit:
        candidates = [Path(explicit)]
    else:
        here = Path(__file__).resolve().parent
        candidates = [Path.cwd() / ".env", here / ".env", here.parent / ".env"]
    for p in candidates:
        if not p.is_file():
            continue
        applied = []
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[7:]
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            if k and not os.environ.get(k):
                os.environ[k] = v
                applied.append(k)
        if applied:
            print(f"  (.env: loaded {', '.join(applied)} from {p})")
        return  # only the first file found


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Publish Markdown / code files to WordPress as drafts.")
    ap.add_argument("paths", nargs="+", help="Markdown/code files, or directories of *.md")
    ap.add_argument("-r", "--recursive", action="store_true")
    ap.add_argument("--status", default="draft", choices=["draft", "publish", "pending", "private"])
    ap.add_argument("--code-style", default="core", choices=["core", "prism"])
    ap.add_argument("--lang", default=None, help="language hint for non-markdown code files")
    ap.add_argument("--title", default=None, help="override title (single file only)")
    ap.add_argument("--category", action="append", help="category name (repeatable; created if missing)")
    ap.add_argument("--tag", action="append", help="tag name (repeatable; created if missing)")
    ap.add_argument("--featured", default=None, help="featured image path (eyecatch)")
    ap.add_argument("--featured-from-first", action="store_true",
                    help="use the first inline image as the featured image")
    ap.add_argument("--attachments-dir", default=None, help="extra dir to resolve Obsidian images")
    ap.add_argument("--excerpt", default=None)
    ap.add_argument("--slug", default=None)
    ap.add_argument("--no-verify", action="store_true", help="skip the post-publish fetch/report")
    ap.add_argument("--env-file", default=None,
                    help="path to a .env file (WP_URL/WP_USERNAME/WP_APP_PASSWORD); real env wins")
    ap.add_argument("--dry-run", action="store_true", help="render & preview without posting")
    args = ap.parse_args()

    files = collect_files(args.paths, args.recursive)
    if not files:
        print("No files to publish.", file=sys.stderr)
        return 1
    if args.title and len(files) > 1:
        print("--title can only be used with a single file.", file=sys.stderr)
        return 2

    wp = None
    if not args.dry_run:
        load_dotenv(args.env_file)
        url = (os.environ.get("WP_URL") or "").rstrip("/")
        user = os.environ.get("WP_USERNAME") or ""
        pw = os.environ.get("WP_APP_PASSWORD") or ""
        miss = [n for n, v in (("WP_URL", url), ("WP_USERNAME", user), ("WP_APP_PASSWORD", pw)) if not v]
        if miss:
            print(
                "Missing env vars: " + ", ".join(miss) + "\n"
                "Provide credentials in ONE of these ways:\n"
                "  1) a .env file next to the script (copy .env.example -> .env), OR\n"
                "  2) export them in your shell, OR\n"
                "  3) inject from a secret manager before the command (Vault, direnv, cloud secrets, …).\n"
                "Required: WP_URL (include subdir, e.g. https://example.com/blog), "
                "WP_USERNAME, WP_APP_PASSWORD.",
                file=sys.stderr)
            return 2
        wp = WP(url, user, pw)

    failures = 0
    for f in files:
        print(f"\n=== {f} ===")
        try:
            payload = build_post(f, wp, args)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ render/upload error: {e}", file=sys.stderr)
            failures += 1
            continue

        if args.dry_run:
            dry = payload.pop("_dry", {})
            print(f"  title   : {payload['title']}")
            print(f"  status  : {payload['status']}")
            print(f"  category: {dry.get('categories')}")
            print(f"  tags    : {dry.get('tags')}")
            print(f"  featured: {dry.get('featured') or ('first-image' if dry.get('featured_from_first') else None)}")
            print(f"  chars   : {len(payload['content'])}")
            print("  --- content (先頭400) ---")
            print("  " + payload["content"][:400].replace("\n", "\n  "))
            continue

        resp = wp.create_post(payload)
        if resp.status_code == 201:
            pid = resp.json().get("id")
            print(f"  ✓ draft #{pid}")
            if not args.no_verify:
                report(wp, pid)
        else:
            failures += 1
            print(f"  ✗ HTTP {resp.status_code} {resp.text[:400]}", file=sys.stderr)

    if failures:
        print(f"\nDone with {failures} failure(s).", file=sys.stderr)
        return 1
    print("\nAll done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
