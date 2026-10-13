#!/usr/bin/env python3
"""Sync ORCID + sourced fallback CV into Kaye-HQ/homepage (HugoBlox Academic CV).

Dry-run by default. Dependencies: pip install ruamel.yaml
Options: --apply (write), --push (commit and push only changed files), --diff.
ORCID_ACCESS_TOKEN or ORCID_CLIENT_ID + ORCID_CLIENT_SECRET are optional
when the anonymous ORCID API is available; set credentials if it returns 401.
"""
import argparse
import difflib
from functools import lru_cache
import hashlib
import io
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from ruamel.yaml import YAML

ORCID = "0000-0002-5753-2724"
BASE = f"https://pub.orcid.org/v3.0/{ORCID}"
ORCID_URL = f"https://orcid.org/{ORCID}"
YAML_RT = YAML(typ="rt")
YAML_RT.preserve_quotes = True
YAML_RT.width = 100
YAML_RT.indent(mapping=2, sequence=4, offset=2)
CHANGES = {}


def fetch_json(url, headers=None, data=None, optional=False):
    import json
    hdr = {"Accept": "application/json", "User-Agent": "Personal-HugoBlox-ORCID-Sync/1.0"}
    hdr.update(headers or {})
    req = Request(url, data=data, headers=hdr)
    try:
        with urlopen(req, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        if optional:
            return None
        raise RuntimeError(f"Request failed: {url}\n{exc}") from exc


@lru_cache(maxsize=1)
def orcid_headers():
    token = os.getenv("ORCID_ACCESS_TOKEN", "").strip()
    client_id = os.getenv("ORCID_CLIENT_ID", "").strip()
    client_secret = os.getenv("ORCID_CLIENT_SECRET", "").strip()
    if not token and client_id and client_secret:
        data = urlencode({"client_id": client_id, "client_secret": client_secret,
                          "grant_type": "client_credentials", "scope": "/read-public"}).encode()
        result = fetch_json("https://orcid.org/oauth/token", data=data)
        token = result["access_token"]
    headers = {"Accept": "application/vnd.orcid+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def orcid_record():
    try:
        return fetch_json(BASE + "/record", headers=orcid_headers())
    except RuntimeError as exc:
        raise RuntimeError(str(exc) + "\nORCID may require authentication. Configure "
                           "ORCID_ACCESS_TOKEN or ORCID_CLIENT_ID and ORCID_CLIENT_SECRET. "
                           "See https://orcid.org/developer-tools") from exc


def value(obj):
    if isinstance(obj, dict):
        return obj.get("value") or ""
    return obj or ""


def text(obj):
    return str(value(obj)).strip()


def date_of(obj):
    """Publication date. First-of-month/year is a sorting placeholder if precision is low."""
    if not isinstance(obj, dict):
        return None, None
    y, m, d = (text(obj.get(k)) for k in ("year", "month", "day"))
    if not y or not y.isdigit():
        return None, None
    precision = "day" if d else ("month" if m else "year")
    try:
        import datetime
        datestr = f"{int(y):04d}-{int(m or 1):02d}-{int(d or 1):02d}"
        datetime.date.fromisoformat(datestr)
        return datestr, precision
    except ValueError:
        return None, None


def fm_read(path):
    raw = path.read_text(encoding="utf-8")
    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", raw, re.S)
    if not match:
        raise ValueError(f"YAML frontmatter missing: {path}")
    return YAML_RT.load(match.group(1)) or {}, match.group(2)


def yaml_string(obj):
    stream = io.StringIO()
    YAML_RT.dump(obj, stream)
    return stream.getvalue()


def stage(path, content):
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old != content:
        CHANGES[path] = (old, content)


def stage_yaml(path, obj):
    stage(path, yaml_string(obj))


def stage_page(path, obj, body=""):
    stage(path, "---\n" + yaml_string(obj) + "---\n" + body)


def safe_norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def doi_norm(s):
    s = (s or "").strip()
    s = re.sub(r"(?i)^\s*(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", s)
    return s.strip().lower()


def external_doi(summary):
    items = (summary.get("external-ids") or {}).get("external-id") or []
    for item in items:
        if str(item.get("external-id-type", "")).lower() == "doi":
            doi = doi_norm(item.get("external-id-value", ""))
            if doi.startswith("10.") and "/" in doi:
                return doi
    return ""


def all_summaries(tree, key):
    if isinstance(tree, list):
        for x in tree:
            yield from all_summaries(x, key)
    elif isinstance(tree, dict):
        for k, v in tree.items():
            if k == key and isinstance(v, list):
                yield from (x for x in v if isinstance(x, dict))
            elif k == key and isinstance(v, dict):
                yield v
            else:
                yield from all_summaries(v, key)


def get_works(record):
    works = (record.get("activities-summary") or {}).get("works") or {}
    summaries = []
    for group in works.get("group", []):
        choices = group.get("work-summary", [])
        # Prefer a record with DOI and a more complete publication date.
        choices = sorted(choices, key=lambda w: (bool(external_doi(w)),
                         bool((w.get("publication-date") or {}).get("month"))), reverse=True)
        if choices:
            summaries.append(choices[0])
    result, seen_dois, seen_titles = [], set(), set()
    for work in sorted(summaries, key=lambda w: bool(external_doi(w)), reverse=True):
        title = text((work.get("title") or {}).get("title"))
        if not title:
            continue
        doi = external_doi(work)
        title_key = safe_norm(title)
        if title_key in seen_titles or (doi and doi in seen_dois):
            continue
        seen_titles.add(title_key)
        if doi:
            seen_dois.add(doi)
        result.append(work)
    return result


def crossref(doi):
    if not doi:
        return {}
    result = fetch_json("https://api.crossref.org/works/" + quote(doi, safe=""),
                        headers={"Accept": "application/json"}, optional=True)
    return result.get("message", {}) if result else {}


def pub_type(kind):
    mapping = {"journal-article": "article-journal", "conference-paper": "paper-conference",
               "book-chapter": "chapter", "posted-content": "manuscript",
               "preprint": "manuscript", "dissertation": "thesis"}
    normalized = mapping.get((kind or "").lower(), (kind or "").lower())
    return normalized if normalized in {"article-journal", "paper-conference", "thesis",
        "book", "chapter", "report", "patent", "manuscript", "article", "dataset"} else "article"


def make_pub(work, author_name):
    title = text((work.get("title") or {}).get("title"))
    doi = external_doi(work)
    cr = crossref(doi)
    title = title or ((cr.get("title") or [""])[0])
    venue = text(work.get("journal-title")) or (cr.get("container-title") or [""])[0]
    date, precision = date_of(work.get("publication-date"))
    if not date:
        parts = (cr.get("published") or cr.get("issued") or {}).get("date-parts") or []
        if parts:
            nums = parts[0]
            date, precision = date_of({"year": str(nums[0]),
                                       "month": str(nums[1]) if len(nums) > 1 else "",
                                       "day": str(nums[2]) if len(nums) > 2 else ""})
    authors = []
    for author in cr.get("author", []):
        name = " ".join(filter(None, [author.get("given", ""), author.get("family", "")])).strip()
        if name:
            authors.append("me" if safe_norm(name) == safe_norm(author_name) else name)
    if not authors and work.get("put-code"):
        detail = fetch_json(BASE + "/work/" + str(work["put-code"]),
                            headers=orcid_headers(), optional=True)
        for contributor in (detail or {}).get("contributors", {}).get("contributor", []) or []:
            name = text(contributor.get("credit-name"))
            if name:
                authors.append("me" if safe_norm(name) == safe_norm(author_name) else name)
    data = {"title": title, "publication_types": [pub_type(cr.get("type") or work.get("type"))],
            "featured": False, "orcid_sync": True, "orcid_record": ORCID_URL}
    if date:
        data["date"] = date
        data["orcid_date_precision"] = precision
    if authors:
        data["authors"] = list(dict.fromkeys(authors))
    if venue:
        data["publication"] = {"name": venue}
    if doi:
        data["doi"] = doi
        data["hugoblox"] = {"ids": {"doi": doi}}
    return data


def is_demo(info):
    return ("lorem ipsum" in str(info.get("abstract", "")).lower()
            or "HugoBlox Kit Conference" in str(info.get("publication", ""))
            or "Journal of Source Themes" in str(info.get("publication", "")))


def clean_demo(info, body):
    # Only delete unmistakable starter-template fields. Preserve user-written summaries.
    for k in ("awards", "funding", "author_notes", "peer_reviewed", "open_access",
              "license", "links", "image", "projects", "slides"):
        info.pop(k, None)
    if "lorem ipsum" in str(info.get("abstract", "")).lower():
        info.pop("abstract", None)
    if any(x in str(info.get("publication", "")) for x in ("Source Themes", "HugoBlox Kit")):
        info.pop("publication", None)
    hb = info.get("hugoblox")
    if isinstance(hb, dict):
        ids = hb.get("ids")
        if isinstance(ids, dict):
            for k in list(ids):
                if k == "arxiv" and ids[k] == "1512.04133v1":
                    del ids[k]
                if k == "doi" and ids[k] == "10.5555/123456":
                    del ids[k]
            if not ids:
                hb.pop("ids", None)
        if not hb:
            info.pop("hugoblox", None)
    if "Add the publication's **full text**" in body or "Click the *Cite*" in body:
        body = "\n"
    return body


def sync_publications(root, works, author_name):
    base = root / "content/publications"
    if not base.exists():
        print(f"WARNING: {base} missing; creating a publications folder")
    existing = []
    for path in base.glob("**/index.md"):
        info, body = fm_read(path)
        if is_demo(info):
            body = clean_demo(info, body)
            stage_page(path, info, body)
        existing.append((path, info, body))
    for work in works:
        item = make_pub(work, author_name)
        title = item["title"]
        doi = doi_norm(item.get("doi", ""))
        matched = None
        for path, info, body in existing:
            found_doi = doi_norm(str(info.get("doi") or (info.get("hugoblox") or {}).get("ids", {}).get("doi") or ""))
            same_title = safe_norm(str(info.get("title", ""))) == safe_norm(title)
            near_title = difflib.SequenceMatcher(None, safe_norm(str(info.get("title", ""))),
                                                  safe_norm(title)).ratio() >= 0.97
            if (doi and found_doi == doi) or same_title or near_title:
                if found_doi and doi and found_doi != doi and not is_demo(info):
                    print(f"WARNING: conflicting DOI in {path}; skipping")
                    matched = "skip"
                    break
                matched = (path, info, body)
                break
        if matched == "skip":
            continue
        if matched:
            path, info, body = matched
            if is_demo(info):
                body = clean_demo(info, body)
            # Auto-update bibliographic fields; preserve manually-written extras.
            keys = ["date", "orcid_date_precision", "publication_types", "publication", "authors", "doi"]
            for k in keys:
                if k in item:
                    info[k] = item[k]
            if doi:
                hb = info.setdefault("hugoblox", {})
                hb.setdefault("ids", {})["doi"] = doi
            if info.get("orcid_sync"):
                info["title"] = title
            stage_page(path, info, body)
        else:
            key = doi or safe_norm(title)
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:9]
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-")
            path = base / f"orcid-{slug}-{digest}" / "index.md"
            stage_page(path, item, "\n")
            existing.append((path, item, "\n"))
    return len(works)


# Verified secondary sources are used ONLY when a category has no public ORCID
# records. These are deliberately conservative. Review degree-awarding institution
# and the exact current job title before publishing.
# Sources:
#   https://zhang-hydrolab.github.io/people.html
#   https://www.researchgate.net/scientific-contributions/Qi-Huang-2329448154
FALLBACK_EDUCATION = [
    {"degree": "PhD (completed 2025)",
     "institution": "Institute of Geographic Sciences and Natural Resources Research, CAS",
     "summary": "PhD research at IGSNRR. Confirm the degree-awarding university before publication."},
    {"degree": "Master's study",
     "institution": "Sichuan University",
     "start": "2018-09-01", "end": "2021-06-01"},
]
FALLBACK_EXPERIENCE = [
    {"role": "Assistant Professor", "org": "National Institute of Natural Hazards"},
    {"role": "Visiting PhD Student", "org": "University of Zurich",
     "start": "2023-10-01", "end": "2024-10-01"},
]
DEMO_EDUCATION = {
    ("stanforduniversity", "phdcomputerscience"),
    ("carnegiemellonuniversity", "mscomputerscience"),
    ("mit", "bscomputerscience"),
}
DEMO_EXPERIENCE = {
    ("metaai", "seniorairesearchscientist"),
    ("openai", "airesearchintern"),
}


def public_affiliations(record, category, summary_key):
    """Extract public affiliation summaries, including ORCID group/summaries layout.

    The consolidated /record endpoint is preferred; if it omits a category,
    request the dedicated /educations or /employments endpoint as a fallback.
    """
    activities = record.get("activities-summary") or {}
    items = list(all_summaries(activities.get(category) or {}, summary_key))
    if not items:
        endpoint = fetch_json(BASE + "/" + category, headers=orcid_headers(), optional=True)
        if endpoint is None:
            print(f"WARNING: Unable to read ORCID /{category}; public items may be absent or restricted")
        else:
            items = list(all_summaries(endpoint, summary_key))
    # ORCID can group multiple assertions of the same affiliation together.
    deduped, keys = [], set()
    for item in items:
        org = text((item.get("organization") or {}).get("name"))
        role = text(item.get("role-title"))
        start, _ = date_of(item.get("start-date"))
        end, _ = date_of(item.get("end-date"))
        signature = (safe_norm(org), safe_norm(role), start or "", end or "")
        if org and signature not in keys:
            deduped.append(item)
            keys.add(signature)
    return deduped


def make_affiliation(item, category):
    org = text((item.get("organization") or {}).get("name"))
    role = text(item.get("role-title"))
    department = text(item.get("department-name"))
    if not org:
        return None
    if category == "education":
        entry = {"degree": role or ("Study in " + department if department else "Study"),
                 "institution": org}
    else:
        entry = {"role": role or ("Research, " + department if department else "Position"),
                 "org": org}
    for src, dest in (("start-date", "start"), ("end-date", "end")):
        dt, precision = date_of(item.get(src))
        if dt:
            # HugoBlox expects a full ISO date. The original ORCID precision is
            # not enhanced: an unknown month/day is explicitly announced here.
            entry[dest] = dt
            if precision != "day":
                print(f"NOTE: {category} {org}: {dest} was provided to {precision} precision; "
                      f"using {dt} for Hugo's date parser")
    return entry


def demo_entry(entry, category):
    if not isinstance(entry, dict):
        return False
    if category == "education":
        key = (safe_norm(text(entry.get("institution"))), safe_norm(text(entry.get("degree"))))
        return key in DEMO_EDUCATION
    key = (safe_norm(text(entry.get("org"))), safe_norm(text(entry.get("role"))))
    return key in DEMO_EXPERIENCE


def identity(entry, category):
    org = "institution" if category == "education" else "org"
    title = "degree" if category == "education" else "role"
    return safe_norm(text(entry.get(org))), safe_norm(text(entry.get(title)))


def merge_timeline(previous, incoming, category, remote_available=False, prune_fallback=False):
    """Keep legitimate manual entries and notes, remove unmistakable demo rows.

    Update machine-owned fields where an entry matches; when details are missing
    in ORCID, retain the existing value. An empty remote category never deletes
    legitimate manually written items.
    """
    known_fallback = (FALLBACK_EDUCATION if category == "education" else FALLBACK_EXPERIENCE)
    fallback_keys = {identity(x, category) for x in known_fallback}
    incoming_keys = {identity(x, category) for x in incoming}
    good = [dict(x) for x in (previous or [])
            if isinstance(x, dict) and not demo_entry(x, category)
            and not ((remote_available or prune_fallback) and identity(x, category) in fallback_keys
                     and identity(x, category) not in incoming_keys)]
    for source in incoming:
        incoming_id = identity(source, category)
        found = next((x for x in good if identity(x, category) == incoming_id), None)
        if found is None:
            good.append(dict(source))
        else:
            for key in ("start", "end"):
                if source.get(key) and (not found.get(key) or found.get("orcid_sync")):
                    found[key] = source[key]
            # Existing human-written summaries and other custom fields survive.
            if source.get("summary") and not found.get("summary"):
                found["summary"] = source["summary"]
    return good


def sync_author(root, record, orcid_only=False):
    path = root / "data/authors/me.yaml"
    author = YAML_RT.load(path.read_text(encoding="utf-8"))
    person = record.get("person") or {}
    name = person.get("name") or {}
    owner = author.setdefault("name", {})
    given, family = text(name.get("given-names")), text(name.get("family-name"))
    if given:
        owner["given"] = given
    if family:
        owner["family"] = family
    author_name = " ".join((given or owner.get("given", ""), family or owner.get("family", ""))).strip()
    if not owner.get("display") and author_name:
        owner["display"] = author_name
    author.setdefault("ids", {})["orcid"] = ORCID
    links = author.setdefault("links", [])
    for link in links:
        if link.get("label") == "Email":
            url = str(link.get("url") or "")
            if "@" in url and not url.startswith("mailto:"):
                link["url"] = "mailto:" + url
    if not any(ORCID in str(x.get("url", "")) for x in links):
        links.append({"icon": "academicons/orcid", "url": ORCID_URL, "label": "ORCID"})
    biography = str((person.get("biography") or {}).get("content") or "").strip()
    if biography and not author.get("bio"):
        author["bio"] = biography
    keywords = [str(x.get("content", "")).strip() for x in
                (person.get("keywords") or {}).get("keyword", []) if x.get("content")]
    demo_interest = ["Large Language Models", "Computer Vision", "Reinforcement Learning", "AI Ethics"]
    if keywords:
        author["interests"] = list(dict.fromkeys(keywords))
    elif list(author.get("interests", [])) == demo_interest:
        author["interests"] = [] if orcid_only else [
            "Hydrological extremes", "Hydrological regime change", "Hydrological modeling",
            "Runoff prediction in ungauged basins"]

    emp_raw = public_affiliations(record, "employments", "employment-summary")
    edu_raw = public_affiliations(record, "educations", "education-summary")
    jobs = [x for x in (make_affiliation(item, "experience") for item in emp_raw) if x]
    schools = [x for x in (make_affiliation(item, "education") for item in edu_raw) if x]
    if not jobs and not orcid_only:
        jobs = [dict(x) for x in FALLBACK_EXPERIENCE]
        print("NOTE: ORCID has no accessible employment records; using sourced fallback employment.")
    if not schools and not orcid_only:
        schools = [dict(x) for x in FALLBACK_EDUCATION]
        print("NOTE: ORCID has no accessible education records; using sourced fallback education.")
    author["experience"] = merge_timeline(author.get("experience", []), jobs, "experience", bool(emp_raw), orcid_only)
    author["education"] = merge_timeline(author.get("education", []), schools, "education", bool(edu_raw), orcid_only)

    # Prioritize the current public employer; ORCID does not always supply
    # start/end dates. Do not guess the start date from a publication affiliation.
    current_orgs = []
    for item in emp_raw:
        if not item.get("end-date") and (item.get("organization") or {}).get("name"):
            current_orgs.append(text(item["organization"]["name"]))
    if not current_orgs and not emp_raw and not orcid_only:
        current_orgs = ["National Institute of Natural Hazards"]
    affiliations = [x for x in (author.get("affiliations") or [])
                    if isinstance(x, dict) and safe_norm(text(x.get("name"))) != "metaai"
                    and not ((emp_raw or orcid_only) and safe_norm(text(x.get("name"))) ==
                             "nationalinstituteofnaturalhazards" and
                             all(safe_norm(name) != "nationalinstituteofnaturalhazards"
                                 for name in current_orgs) and len(x) == 1)]
    seen = {safe_norm(text(x.get("name"))) for x in affiliations}
    for org in current_orgs:
        if safe_norm(org) not in seen:
            affiliations.append({"name": org})
            seen.add(safe_norm(org))
    author["affiliations"] = affiliations
    if current_orgs:
        job = next((x for x in jobs if safe_norm(text(x.get("org"))) == safe_norm(current_orgs[0])), None)
        current_role = text((job or {}).get("role"))
        if current_role and (not author.get("role") or
                             str(author.get("role")) in ("Senior Hydrological extremes Research Scientist", "Assistant Professor")):
            author["role"] = current_role

    print(f"ORCID employment: {len(emp_raw)}; education: {len(edu_raw)}. "
          f"Home CV employment: {len(author['experience'])}; education: {len(author['education'])}.")
    stage_yaml(path, author)
    return author_name or "Qi Huang", biography


def sync_home(root, bio):
    path = root / "content/_index.md"
    home, body = fm_read(path)
    sections = home.get("sections") or []
    new_text = (bio or f"My research focuses on hydrological extremes. "
                f"See [my ORCID profile]({ORCID_URL}) for publicly listed research outputs.")
    for section in list(sections):
        content = section.get("content") or {}
        if section.get("block") == "markdown":
            old = str(content.get("text", ""))
            if "Moonshot team at DeepMind" in old or "science and technology in the economy" in old:
                content["text"] = new_text
        if section.get("block") == "cta-card" and section.get("demo") is True:
            sections.remove(section)
    stage_page(path, home, body)


def push_changes(root):
    if not CHANGES:
        return
    if not (root / ".git").exists():
        raise RuntimeError("--push requires a git clone with a .git directory")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode != 0:
        raise RuntimeError("Unrelated staged changes exist; please commit or unstage them before --push")
    paths = [str(p.relative_to(root)) for p in CHANGES]
    subprocess.run(["git", "add", "--", *paths], cwd=root, check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=root).returncode == 0:
        return
    subprocess.run(["git", "commit", "-m", "chore: sync ORCID profile and publications"], cwd=root, check=True)
    subprocess.run(["git", "push"], cwd=root, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="HugoBlox repository path")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--diff", action="store_true", help="Print unified diffs")
    parser.add_argument("--orcid-only", action="store_true",
                        help="Disable sourced fallback education and employment")
    parser.add_argument("--push", action="store_true", help="Commit and git push; implies --apply")
    args = parser.parse_args()
    root = args.root.resolve()
    for required in ("data/authors/me.yaml", "content/_index.md"):
        if not (root / required).exists():
            parser.error(f"Not the expected HugoBlox repo; missing: {required}")
    record = orcid_record()  # fail closed: no files touched if API errors
    author_name, bio = sync_author(root, record, orcid_only=args.orcid_only)
    sync_home(root, bio)
    nworks = sync_publications(root, get_works(record), author_name)
    print(f"ORCID public works: {nworks}; files to change: {len(CHANGES)}")
    for p, (before, after) in CHANGES.items():
        print("  " + str(p.relative_to(root)))
        if args.diff:
            print("".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                               fromfile="before", tofile="after")))
    if args.apply or args.push:
        for path, (_, content) in CHANGES.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        print("Wrote changed files. No files were deleted.")
    else:
        print("DRY RUN: no files written. Add --apply to save, --push to publish.")
    if args.push:
        push_changes(root)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, HTTPError, URLError, ValueError) as error:
        sys.exit("ERROR: " + str(error))
