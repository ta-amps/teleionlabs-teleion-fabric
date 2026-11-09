#!/usr/bin/env python3
"""Build teleionlabs.com from its content Task Objects.

Every visible string lives in content/*.json (Task Objects, MUST-FOLLOW rule 12). This script and
templates/ hold markup only. The build fails when a content file or a required field is missing.

Usage: python3 build.py [--out dist]
Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
CONTENT = HERE / "content"
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"

SHARED_WORDS = Path(os.environ.get("BANNED_WORDS") or HERE.parents[2] / "coordination-2026-10-31" / "rules" / "banned-words.json")
SAMPLE_WORDS = HERE / "tests" / "sample-word-list.json"
WORD_CATEGORIES = ("always", "visible_text", "agent_apps_outside_models_page")
WORKER_WORDS = HERE / "worker" / "banned.generated.js"
WORKER_MEDIA = HERE / "worker" / "media.generated.js"
WORKER_TEXT = HERE / "worker" / "text.generated.js"
# The whole design of a text page (owner, Nov 2: text pages and a film on the home page, nothing more). Light only (rule 13).
TEXT_CSS = ("header>p:first-of-type{white-space:nowrap;font-size:min(1em,3.2vw);text-align:center}a[href=\"#main\"]:not(:focus){position:absolute;left:-999px}#film-lead{text-align:center;margin:.75rem calc(50% - 50vw) 0;padding:0 1rem}html{-webkit-tap-highlight-color:transparent;color-scheme:only light;background:#F5F2EA;color:#11161F;overflow-x:clip;"
            "font:16px/1.6 ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace}"
            "body{max-width:72ch;margin:auto;padding:1rem;overflow-wrap:anywhere}a{color:#24519B}a:hover{color:#11161F}"
            "h1,h2,h3{font-size:1em;margin:2em 0 .4em}table{border-collapse:collapse;display:block;overflow-x:auto}"
            "th,td{padding:0 2ch .4em 0;text-align:left;vertical-align:top}pre{white-space:pre-wrap;background:#FFFDF8;padding:.5em}figure{margin:0}"
            "video{width:100%;height:auto}input,button{font:inherit;min-height:44px;padding:0 1ch}"
            "blockquote{white-space:pre-line;margin:1em 0;padding:.5em 1ch;background:#FFFDF8}input{box-sizing:border-box;width:100%}footer{margin-top:3em;font-size:.875em;text-align:center}footer a{margin:0 1ch;display:inline-block}footer div{margin-top:-.5em;padding-bottom:.5em;font-size:11px;color:#5A5F68}"
            "nav{display:flex;flex-wrap:wrap;justify-content:center;gap:.25em 2ch;margin:0 calc(50% - 50vw);padding:0 1rem}"
            "#film-wrap{margin:1rem calc(50% - 50vw) 0}#film-wrap video{display:block;width:min(100%,max(28rem,calc((100svh - 15rem)*16/9)));height:auto;margin:auto;-webkit-mask-image:linear-gradient(90deg,transparent,#000 2.5%,#000 97.5%,transparent),linear-gradient(transparent,#000 2.5%,#000 97.5%,transparent);mask-image:linear-gradient(90deg,transparent,#000 2.5%,#000 97.5%,transparent),linear-gradient(transparent,#000 2.5%,#000 97.5%,transparent);-webkit-mask-composite:source-in;mask-composite:intersect}"
            "#film-row{text-align:center;margin:.75rem calc(50% - 50vw);padding:0 1rem}#film-row a{margin-right:2ch;display:inline-block}#film-row button{margin:0 2ch .25rem 0;background:#11161F;color:#F5F2EA;border:2px solid #11161F;border-radius:4px;font-weight:700;cursor:pointer}#film-row button:hover{background:#24519B;border-color:#24519B}")
KEEP_ATTR = re.compile(r"^data-(scene|seconds|c|film|svc|src|on|off)$")

PAGES = ["disclaimer", "home", "fabric", "omninode", "shim", "intelligence", "weight_space", "models", "feeds", "video", "explain", "explain_connect", "explain_task", "explain_find", "explain_shape", "explain_eranos", "benchmarks", "methodology", "learning", "free", "docs", "status", "security", "privacy", "terms", "company", "errors"]
REQUIRED = ["site", "llms", "link", *PAGES]


class BuildError(Exception):
    pass


def load(name: str) -> dict:
    path = CONTENT / f"{name}.json"
    if not path.is_file():
        raise BuildError(f"missing content Task Object: {path.relative_to(HERE)}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("kind") != "task_object" or not str(data.get("id", "")).startswith("teleion.instructions/"):
        raise BuildError(f"{path.name}: not a teleion.instructions Task Object")
    return data


def req(obj: dict, key: str, where: str):
    if key not in obj or obj[key] in (None, "", []):
        raise BuildError(f"{where}: missing '{key}'")
    return obj[key]


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def textify(markup: str) -> str:
    """Strip the page to text: no classes, drawings, images or behaviour hooks."""
    markup = re.sub(r"<svg\b.*?</svg>", "", markup, flags=re.S)
    markup = re.sub(r"</a>\s*(<a\b)", r"</a> &nbsp; \1", markup)  # links stay apart with no stylesheet
    markup = re.sub(r"<noscript>.*?</noscript>", "", markup, flags=re.S)
    markup = re.sub(r"<img\b[^>]*>", "", markup)
    markup = re.sub(r"<button\b(?![^>]*data-film)[^>]*>.*?</button>", "", markup, flags=re.S)
    markup = re.sub(r"""\s(?:class|style|tabindex)="[^"]*\"""", "", markup)
    def data(m):
        return m.group(0) if KEEP_ATTR.match(m.group(1)) else ""
    return re.sub(r"""\s(data-[a-z-]+)(?:="[^"]*")?""", data, markup)


class ToText(HTMLParser):
    BLOCK = {"p", "div", "section", "article", "ul", "ol", "table", "pre", "figure", "figcaption", "details", "summary", "nav", "footer", "main", "tr"}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.href, self.pre, self.first_cell = [], 0, None, 0, True
    def nl(self, n=2):
        self.out.append("\n" * n)
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "button", "canvas", "video", "head", "title") :
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "br":
            self.nl(1)
            return
        if tag == "span" and a.get("data-svc"):
            self.out.append(f"[[{a['data-svc']}]]")
            self.skip_span = True
            self.skip += 1
            self.svc_open = True
            return
        if tag in ("h1", "h2", "h3"):
            self.nl(3)
        elif tag == "li":
            self.nl(1); self.out.append("- ")
        elif tag in ("td", "th"):
            if not self.first_cell: self.out.append("  ")
            self.first_cell = False
        elif tag == "tr":
            self.nl(1); self.first_cell = True
        elif tag in self.BLOCK:
            self.nl()
        if tag == "pre": self.pre += 1
        if tag == "a" and a.get("href") and not a["href"].startswith("#"):
            self.href = a["href"]
    def handle_endtag(self, tag):
        if tag in ("style", "script", "button", "canvas", "video", "head", "title") or (tag == "span" and getattr(self, "svc_open", False)):
            if self.skip: self.skip -= 1
            self.svc_open = False
            return
        if self.skip:
            return
        if tag == "a" and self.href:
            self.out.append(f" <{self.href}>")
            self.href = None
        if tag == "pre": self.pre -= 1
        if tag in ("h1", "h2", "h3") or tag in self.BLOCK:
            self.nl(1)
    def handle_data(self, data):
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def to_text(markup: str) -> str:
    p = ToText()
    markup = re.sub(r'<p id="film-(?:controls|row)".*?</p>', "", markup, flags=re.S)
    markup = markup.replace('<a href="#main">Skip to content</a>', "")
    markup = re.sub(r'<script\b.*?</script>', "", markup, flags=re.S)
    p.feed(markup)
    text = "".join(p.out)
    text = "\n".join(line.strip() if not line.startswith(" ") else line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def text_route(route: str) -> str:
    return "index.txt" if route == "/" else route.strip("/") + ".txt"


class Renderer:
    def __init__(self, site: dict):
        self.site = site
        self.origin = req(site, "origin", "site")
        self.host = urlsplit(self.origin).hostname
        self.mark = (TEMPLATES / "mark.svg").read_text(encoding="utf-8").strip()
        self.chips = req(site, "chips", "site")
        self.labels = req(site, "labels", "site")

    def banner(self) -> str:
        noun = req(self.site, "banner", "site")
        for k in BANNER_FIELDS:
            req(noun, k, "site/banner")
        value = {"schema_version": 1, "kind": "instructions", "key": "agent-banner", **{k: noun[k] for k in BANNER_FIELDS}}
        blob = json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        return (f'<ta-agent-banner class="agent-banner"><script type="application/json">{blob}</script>'
                f'<p class="agent-fallback">{BANNER_ICON}<span class="agent-fallback-lead"><b>{esc(noun["lead_strong"])}</b> {esc(noun["lead"])}</span> '
                f'<a href="{esc(req(noun, "fallback_href", "site/banner"))}">{esc(noun["chip"])}</a></p></ta-agent-banner>')

    def is_external(self, href: str) -> bool:
        parts = urlsplit(href)
        return bool(parts.scheme in ("http", "https") and parts.hostname and parts.hostname != self.host)

    def link(self, label: str, href: str, cls: str = "") -> str:
        mark = esc(self.site["external_mark"]) if self.is_external(href) else ""
        klass = f' class="{cls}"' if cls else ""
        return f'<a{klass} href="{esc(href)}">{esc(label)}{mark}</a>'

    INLINE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)|`([^`]+)`")

    def inline(self, text: str) -> str:
        out, pos = [], 0
        for m in self.INLINE.finditer(text):
            out.append(esc(text[pos:m.start()]))
            if m.group(1) is not None:
                out.append(self.link(m.group(1), m.group(2)))
            else:
                out.append(f"<code>{esc(m.group(3))}</code>")
            pos = m.end()
        out.append(esc(text[pos:]))
        return "".join(out)

    def chip(self, key: str | None) -> str:
        if not key:
            return ""
        if key not in self.chips:
            raise BuildError(f"unknown chip '{key}'")
        return f'<span class="chip chip--{esc(key)}">{esc(self.chips[key])}</span>'

    def button(self, item: dict, where: str) -> str:
        style = item.get("style", "secondary")
        return self.link(req(item, "label", where), req(item, "href", where), f"btn btn--{esc(style)}")

    def code(self, text: str) -> str:
        return f'<pre class="code" data-generated><code>{esc(text)}</code></pre>'

    def block(self, b: dict, where: str) -> str:
        kind = req(b, "type", where)
        fn = getattr(self, f"b_{kind}", None)
        if fn is None:
            raise BuildError(f"{where}: unknown block type '{kind}'")
        return fn(b, f"{where}/{b.get('id', kind)}")

    def b_film(self, b, where):
        # The home page plays the rendered film: muted on arrival, with a Sound button; the scenes are its text, below it.
        v = req(b, "video", where)
        media, captions, poster = esc(req(v, "media", where)), esc(req(v, "captions", where)), esc(req(v, "poster", where))
        secs = []
        for s in req(b, "scenes", where):
            ps = "".join(f"<p>{self.inline(p)}</p>" for p in s.get("paragraphs", []))
            steps = "".join(f"<li>{self.inline(p)}</li>" for p in s.get("steps", []))
            steps = f"<ul>{steps}</ul>" if steps else ""
            secs.append(f'<section><h2>{self.inline(req(s, "heading", where))}</h2>{ps}{steps}</section>')
        ctl = req(b, "controls", where)
        next_links = "".join(f' <a href="{esc(req(l, "href", where))}">{esc(req(l, "label", where))}</a>' for l in ctl.get("next", []))
        return (f'<p id="film-lead">{esc(req(b, "lead", where))}</p>'
                f'<figure id="film-wrap"><video id="film" controls autoplay muted playsinline preload="metadata" width="1280" height="720" '
                f'poster="{poster}" aria-label="{esc(req(b, "label", where))}"><source src="{media}" type="video/mp4">'
                f'<track kind="captions" src="{captions}" srclang="en" label="{esc(req(v, "captions_label", where))}">'
                f'<a href="{media}">{esc(req(v, "download", where))}</a></video></figure>'
                f'<p id="film-row"><button type="button" data-film="sound" data-on="{esc(req(v, "sound_on", where))}" data-off="{esc(req(v, "sound_off", where))}" hidden>{esc(v["sound_on"])}</button>'
                f'<button type="button" data-film="captions" data-on="{esc(req(v, "captions_on", where))}" data-off="{esc(req(v, "captions_off", where))}" hidden>{esc(v["captions_on"])}</button>'
                f'<a href="#text">{esc(req(ctl, "read", where))}</a>{next_links}</p>'
                f'<article id="text"><h1>{esc(req(b, "heading", where))}</h1>{"".join(secs)}</article>')

    def b_status(self, b, where):
        items = "".join(
            f'<li><b>{esc(req(it, "name", where))}</b> <span data-svc="{esc(it["id"])}">{esc(req(b, "initial", where))}</span><br>'
            f'{self.inline(req(it, "text", where))}<br>{self.link(req(it, "host", where), req(it, "href", where))}</li>'
            for it in req(b, "items", where))
        return self._section(b, where, f"<ul>{items}</ul>")

    def _head(self, b: dict, where: str, cls: str) -> str:
        parts = [f'<section class="{cls}" aria-labelledby="page-title"><div class="wrap">']
        if b.get("eyebrow"):
            parts.append(f'<p class="eyebrow">{esc(b["eyebrow"])}</p>')
        parts.append(f'<h1 id="page-title">{esc(req(b, "title", where))}</h1>')
        if b.get("lead"):
            parts.append(f'<p class="lead">{self.inline(b["lead"])}</p>')
        if b.get("sub"):
            parts.append(f'<p class="hero__sub">{self.inline(b["sub"])}</p>')
        if b.get("actions"):
            parts.append('<div class="actions">' + "".join(self.button(a, where) for a in b["actions"]) + "</div>")
        if b.get("fixes"):
            fx = b["fixes"]
            pills = "".join(self.link(req(it, "label", where), req(it, "href", where), "hero__fix-pill")
                            for it in req(fx, "items", where))
            parts.append(f'<div class="hero__fixes"><span class="hero__fixes-label">{esc(req(fx, "label", where))}</span>{pills}</div>')
        if b.get("note"):
            parts.append(f'<p class="hero__note">{self.inline(b["note"])}</p>')
        parts.append("</div></section>")
        return "".join(parts)

    def b_hero(self, b, where):
        return self._head(b, where, "hero")

    def b_page_head(self, b, where):
        return self._head(b, where, "page-head")

    def _section(self, b: dict, where: str, inner: str, cls: str = "section") -> str:
        sid = esc(req(b, "id", where))
        intro = f'<p class="section__intro">{self.inline(b["intro"])}</p>' if b.get("intro") else ""
        return (f'<section class="{cls}" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap">'
                f'<h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>{intro}{inner}</div></section>')

    def b_flow(self, b, where):
        steps = "".join(
            f'<li class="flow__step">{self.chip(s.get("chip"))}<h3>{esc(req(s, "title", where))}</h3>'
            f'<p>{self.inline(req(s, "text", where))}</p></li>' for s in req(b, "steps", where))
        return self._section(b, where, f'<ol class="flow">{steps}</ol>')

    def b_cards(self, b, where):
        items = []
        for it in req(b, "items", where):
            link = self.link(it["link"]["label"], it["link"]["href"], "card__link") if it.get("link") else ""
            items.append(f'<li class="card">{self.chip(it.get("chip"))}<h3>{esc(req(it, "title", where))}</h3>'
                         f'<p>{self.inline(req(it, "text", where))}</p>{link}</li>')
        return self._section(b, where, f'<ul class="cards">{"".join(items)}</ul>')

    def b_feature(self, b, where):
        sid = esc(req(b, "id", where))
        what = "".join(f"<p>{self.inline(p)}</p>" for p in req(b, "what", where))
        limits = "".join(f"<li>{self.inline(x)}</li>" for x in req(b, "limits", where))
        link = self.link(b["link"]["label"], b["link"]["href"], "feature__link") if b.get("link") else ""
        return (f'<section class="section feature" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap feature__grid">'
                f'<div class="feature__text">{self.chip(b.get("chip"))}<h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>{what}</div>'
                f'<div class="feature__side"><h3 class="feature__label">{esc(self.labels["how"])}</h3>'
                f'<p>{self.inline(req(b, "how", where))}</p>{self.code(req(b, "code", where))}'
                f'<h3 class="feature__label">{esc(self.labels["limits"])}</h3><ul class="limits">{limits}</ul>{link}</div>'
                f'</div></section>')

    def b_table(self, b, where):
        cols = req(b, "columns", where)
        head = "".join(f'<th scope="col">{esc(c)}</th>' for c in cols)
        rows = []
        for r in req(b, "rows", where):
            if len(r) != len(cols):
                raise BuildError(f"{where}: row has {len(r)} cells, expected {len(cols)}")
            rows.append(f'<tr><th scope="row">{self.inline(r[0])}</th>' + "".join(f"<td>{self.inline(c)}</td>" for c in r[1:]) + "</tr>")
        notes = "".join(f'<p class="table-note">{self.inline(n)}</p>' for n in b.get("notes", []))
        table = f'<table class="limits-table"><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table>{notes}'
        return self._section(b, where, table)

    def b_prose(self, b, where):
        paras = "".join(f"<p>{self.inline(p)}</p>" for p in req(b, "paragraphs", where))
        return self._section(b, where, f'<div class="prose">{paras}</div>')

    def b_steps(self, b, where):
        items = []
        for it in req(b, "items", where):
            code = self.code(it["code"]) if it.get("code") else ""
            items.append(f'<li class="step"><h3>{esc(req(it, "title", where))}</h3><p>{self.inline(req(it, "text", where))}</p>{code}</li>')
        return self._section(b, where, f'<ol class="steps">{"".join(items)}</ol>')

    def b_callout(self, b, where):
        sid = esc(req(b, "id", where))
        paras = "".join(f"<p>{self.inline(p)}</p>" for p in req(b, "paragraphs", where))
        link = self.button({"label": b["link"]["label"], "href": b["link"]["href"]}, where) if b.get("link") else ""
        return (f'<section class="section" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap"><div class="callout">'
                f'<div class="callout__body"><h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>{paras}</div>{link}</div></div></section>')

    def b_contact(self, b, where):
        email = req(b, "email", where)
        inner = (f'<div class="prose"><p>{self.inline(req(b, "text", where))}</p>'
                 f'<p class="contact__email"><a href="mailto:{esc(email)}">{esc(email)}</a></p>'
                 f'<p class="contact__note">{self.inline(req(b, "note", where))}</p></div>')
        return self._section(b, where, inner)

    def b_links(self, b, where):
        items = "".join(f'<li>{self.link(req(it, "label", where), req(it, "href", where), "ref__link")} '
                        f'<span class="ref__text">{self.inline(req(it, "text", where))}</span></li>' for it in req(b, "items", where))
        return self._section(b, where, f'<ul class="refs">{items}</ul>')

    def b_actions(self, b, where):
        return '<div class="wrap"><div class="actions actions--page">' + "".join(self.button(a, where) for a in req(b, "items", where)) + "</div></div>"

    def b_problems(self, b, where):
        items = []
        for idx, it in enumerate(req(b, "items", where), start=1):
            pid = esc(it.get("id") or f"why-{idx}")
            bench = self.link(it["link"]["label"], it["link"]["href"], "problem__bench") if it.get("link") else ""
            items.append(
                f'<li class="problem" id="{pid}"><h3 class="problem__title">{esc(req(it, "problem", where))}</h3>'
                f'<p class="problem__text">{self.inline(req(it, "problem_text", where))}</p>'
                f'<div class="problem__fix"><p class="problem__label">{esc(req(b, "fix_label", where))}</p>'
                f'<p class="problem__fix-title">{esc(req(it, "fix", where))}</p>'
                f'<p class="problem__fix-text">{self.inline(req(it, "fix_text", where))}</p>{bench}</div></li>')
        fourth_html = ""
        if b.get("fourth"):
            f4 = b["fourth"]
            fid = esc(f4.get("id") or "why-4")
            flink = self.link(f4["link"]["label"], f4["link"]["href"], "problem__bench") if f4.get("link") else ""
            fourth_html = (
                f'<article class="problem-fourth" id="{fid}">'
                f'<div class="problem-fourth__main"><h3 class="problem__title">{esc(req(f4, "title", where))}</h3>'
                f'<p class="problem__fix-text">{self.inline(req(f4, "text", where))}</p></div>'
                f'{flink}</article>')
        under = f'<p class="problems__under">{self.inline(b["under"])}</p>' if b.get("under") else ""
        return self._section(b, where, f'<ul class="problems">{"".join(items)}</ul>{fourth_html}{under}', "section section--problems")

    def b_metrics(self, b, where):
        cards = []
        for it in req(b, "items", where):
            rec = self.link(req(it["record"], "label", where), req(it["record"], "href", where), "metric-card__record") if it.get("record") else ""
            cards.append(
                f'<li class="metric-card">'
                f'<span class="metric-card__value">{esc(req(it, "value", where))}</span>'
                f'<p class="metric-card__text">{self.inline(req(it, "label", where))}</p>'
                f'<span class="metric-card__meta"><span>{esc(req(it, "meta", where))}</span>{rec}</span></li>')
        more = f'<p class="metrics-more">{self.link(b["link"]["label"], b["link"]["href"], "metrics-more__link")}</p>' if b.get("link") else ""
        return self._section(b, where, f'<ul class="metric-grid">{"".join(cards)}</ul>{more}', "section")

    def b_kits(self, b, where):
        sid = esc(req(b, "id", where))
        cards = []
        for k in req(b, "kits", where):
            bullets = "".join(f"<li>{self.inline(pt)}</li>" for pt in req(k, "points", where))
            klink = self.link(req(k["link"], "label", where), req(k["link"], "href", where), "btn btn--secondary btn--small kit-card__btn")
            cards.append(
                f'<article class="kit-card">'
                f'<span class="kit-card__id">{esc(req(k, "kit_id", where))}</span>'
                f'<h3>{esc(req(k, "title", where))}</h3>'
                f'<p>{self.inline(req(k, "text", where))}</p>'
                f'<ul class="kit-card__list">{bullets}</ul>'
                f'{klink}</article>')
        vet = req(b, "vetting", where)
        vsteps = "".join(
            f'<li><span class="kits-step-num" aria-hidden="true">{i}</span><span>{self.inline(s)}</span></li>'
            for i, s in enumerate(req(vet, "steps", where), start=1))
        reuse = req(b, "reuse", where)
        pills = "".join(f'<li class="kits-pill">{esc(p)}</li>' for p in req(reuse, "parts", where))
        lib_link = self.link(req(reuse["link"], "label", where), req(reuse["link"], "href", where), "feature__link")
        cmd = self.code(req(b, "code", where))
        inner = (
            f'<p class="section__intro">{self.inline(req(b, "intro", where))}</p>'
            f'<div class="kit-grid">{"".join(cards)}</div>'
            f'<div class="kits-subgrid">'
            f'<div class="kits-col"><h3>{esc(req(vet, "heading", where))}</h3><ol class="kits-steps">{vsteps}</ol></div>'
            f'<div class="kits-col"><h3>{esc(req(reuse, "heading", where))}</h3><p>{self.inline(req(reuse, "text", where))}</p>'
            f'<ul class="kits-pills">{pills}</ul><p class="kits-safety">{self.inline(req(reuse, "safety", where))}</p>{lib_link}</div>'
            f'</div>'
            f'<div class="kits-cmd"><h3 class="feature__label">{esc(self.labels["how"])}</h3><p>{self.inline(req(b, "how", where))}</p>{cmd}</div>'
        )
        return (f'<section class="section section--kits" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap">'
                f'{self.chip(b.get("chip"))}<h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>{inner}</div></section>')

    def b_benchmarks(self, b, where):
        method = req(b, "method", where)
        m_items = "".join(f"<li>{self.inline(x)}</li>" for x in req(method, "items", where))
        head_html = (
            f'<section class="page-head bench-head" aria-labelledby="page-title"><div class="wrap bench-head__grid">'
            f'<div><p class="eyebrow">{esc(req(b, "eyebrow", where))}</p>'
            f'<h1 id="page-title">{esc(req(b, "title", where))}</h1>'
            f'<p class="lead">{self.inline(req(b, "lead", where))}</p></div>'
            f'<aside class="bench-method" aria-labelledby="bench-method-title">'
            f'<h2 id="bench-method-title" class="bench-method__title">{esc(req(method, "heading", where))}</h2>'
            f'<ul class="bench-method__list">{m_items}</ul></aside></div></section>'
        )
        groups = req(b, "groups", where)
        chips = []
        for g in groups:
            key = req(g, "key", where)
            pressed = "true" if key == "all" else "false"
            chips.append(
                f'<button type="button" class="bench-chip" aria-pressed="{pressed}" data-bench-filter="{esc(key)}">'
                f'{esc(req(g, "label", where))}</button>')
        entries = req(b, "entries", where)
        sections = []
        for g in groups:
            key = req(g, "key", where)
            if key == "all":
                continue
            g_entries = [e for e in entries if e.get("group") == key]
            cards = []
            for e in g_entries:
                val = e.get("value")
                if val:
                    rec = (
                        self.link(req(b, "record_label", where), e["record_href"], "metric-card__record")
                        if e.get("record_href") and b.get("record_label")
                        else ""
                    )
                    meta = f'{esc(req(e, "setup", where))}<span aria-hidden="true"> · </span>{esc(req(e, "date", where))}'
                    cards.append(
                        f'<article class="metric-card">'
                        f'<span class="metric-card__value">{esc(val)}</span>'
                        f'<p class="metric-card__text">{self.inline(req(e, "label", where))}</p>'
                        f'<span class="metric-card__meta"><span>{meta}</span>{rec}</span></article>')
                else:
                    cards.append(
                        f'<article class="metric-card metric-card--pending">'
                        f'<span class="metric-card__pending">{esc(req(b, "pending_label", where))}</span>'
                        f'<p class="metric-card__text">{self.inline(req(e, "label", where))}</p>'
                        f'<p class="metric-card__note">{self.inline(req(b, "pending_note", where))}</p>'
                        f'<span class="metric-card__meta"><span>{esc(req(e, "setup", where))}</span></span></article>')
            sections.append(
                f'<section class="bench-group" id="bench-{esc(key)}" aria-labelledby="bench-{esc(key)}-title" data-bench-group="{esc(key)}">'
                f'<h2 id="bench-{esc(key)}-title">{esc(req(g, "label", where))}</h2>'
                f'<div class="metric-grid">{"".join(cards)}</div></section>')
        rerun = req(b, "rerun", where)
        body_html = (
            f'<div class="section bench-body" data-benchmarks><div class="wrap">'
            f'<div class="bench-filters" role="group" aria-label="{esc(req(b, "filter_label", where))}" data-needs-js>{"".join(chips)}</div>'
            f'{"".join(sections)}'
            f'<section class="bench-rerun" aria-labelledby="bench-rerun-title">'
            f'<h2 id="bench-rerun-title">{esc(req(rerun, "heading", where))}</h2>'
            f'<p>{self.inline(req(rerun, "text", where))}</p></section>'
            f'</div></div>'
        )
        return head_html + body_html

    def b_video_teaser(self, b, where):
        sid = esc(req(b, "id", where))
        actions = "".join(self.button(a, where) for a in req(b, "actions", where))
        meta = "".join(f'<span class="video-pill">{esc(m)}</span>' for m in req(b, "meta", where))
        poster = req(b, "poster", where)
        poster_href = req(b, "poster_href", where)
        poster_alt = req(b, "poster_alt", where)
        return (
            f'<section class="section video-teaser" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap video-teaser__grid">'
            f'<div class="video-teaser__text">'
            f'<p class="eyebrow">{esc(req(b, "eyebrow", where))}</p>'
            f'<h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>'
            f'<p>{self.inline(req(b, "text", where))}</p>'
            f'<div class="video-pills">{meta}</div>'
            f'<div class="actions">{actions}</div>'
            f'</div>'
            f'<a class="video-teaser__frame" href="{esc(poster_href)}">'
            f'<img src="{esc(poster)}" alt="{esc(poster_alt)}" width="960" height="540" loading="lazy">'
            f'</a></div></section>'
        )

    def b_video_head(self, b, where):
        return self._head(b, where, "page-head video-head")

    def b_video(self, b, where):
        media = esc(req(b, "media", where))
        captions = esc(req(b, "captions", where))
        transcript = "".join(f'<p>{self.inline(p)}</p>' for p in req(b, "transcript", where))
        return (
            '<div class="wrap video-body"><figure id="film-wrap">'
            f'<video id="film" controls autoplay muted playsinline preload="metadata" width="1280" height="720" poster="{esc(req(b, "poster", where))}" '
            f'aria-label="{esc(req(b, "label", where))}" aria-describedby="video-context">'
            f'<source src="{media}" type="video/mp4">'
            f'<track kind="captions" src="{captions}" srclang="en" label="{esc(req(b, "captions_label", where))}">'
            f'<a href="{media}">{esc(req(b, "download", where))}</a></video></figure>'
            f'<p id="film-row"><button type="button" data-film="sound" data-on="{esc(req(b, "sound_on", where))}" data-off="{esc(req(b, "sound_off", where))}" hidden>{esc(b["sound_on"])}</button>'
            f'<button type="button" data-film="captions" data-on="{esc(req(b, "captions_on", where))}" data-off="{esc(req(b, "captions_off", where))}" hidden>{esc(b["captions_on"])}</button></p>'
            f'<p id="video-context">{self.inline(req(b, "context", where))}</p>'
            f'<p class="video-download"><a href="{media}" download>{esc(b["download"])}</a> · '
            f'<a href="{captions}">{esc(req(b, "captions_download", where))}</a></p>'
            f'<details class="video-transcript"><summary>{esc(req(b, "transcript_label", where))}</summary>{transcript}</details></div>'
        )

    def b_explainer(self, b, where):
        # An explainer film at the top of its page: plays on arrival with the sound off, as browsers require; the words are in the text below it.
        media = esc(req(b, "media", where))
        captions = esc(req(b, "captions", where))
        return (
            '<div class="wrap video-body"><figure class="video-player">'
            f'<video controls autoplay muted playsinline preload="metadata" width="1920" height="1080" '
            f'aria-label="{esc(req(b, "label", where))}">'
            f'<source src="{media}" type="video/mp4">'
            f'<track kind="captions" src="{captions}" srclang="en" label="English">'
            f'<a href="{media}">{esc(req(b, "download", where))}</a></video>'
            f'<figcaption>{self.inline(req(b, "note", where))}</figcaption></figure>'
            f'<p class="video-download"><a href="{media}" download>{esc(b["download"])}</a> · '
            f'<a href="{captions}">{esc(req(b, "captions_download", where))}</a></p></div>'
        )

    def b_band(self, b, where):
        sid = esc(req(b, "id", where))
        paras = "".join(f"<p>{self.inline(p)}</p>" for p in b.get("paragraphs", []))
        facts = "".join(f"<li>{self.inline(x)}</li>" for x in b.get("facts", []))
        facts = f'<ul class="facts">{facts}</ul>' if facts else ""
        links = "".join(self.link(l["label"], l["href"], "band__link") for l in b.get("links", []))
        links = f'<p class="band__links">{links}</p>' if links else ""
        if b.get("diagram"):
            aside = self.diagram(b["diagram"], f"{where}/diagram", sid)
        elif b.get("code"):
            cap = f'<figcaption class="band__caption">{self.inline(b["code_caption"])}</figcaption>' if b.get("code_caption") else ""
            aside = (f'<figure class="band__figure band__figure--code">{cap}'
                     f'<pre class="code code--band" tabindex="0" aria-label="{esc(req(b, "code_label", where))}" data-generated><code>{esc(b["code"])}</code></pre></figure>')
        else:
            raise BuildError(f"{where}: a band needs a diagram or code")
        return (f'<section class="section band" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap band__grid">'
                f'<div class="band__text">{self.chip(b.get("chip"))}<h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>'
                f'{paras}{facts}{links}</div>{aside}</div></section>')

    TONES = ("task", "object", "check", "plain", "muted")

    def diagram(self, d: dict, where: str, sid: str) -> str:
        w, h = int(req(d, "width", where)), int(req(d, "height", where))
        nodes = {}
        for n in req(d, "nodes", where):
            tone = n.get("tone", "plain")
            if tone not in self.TONES:
                raise BuildError(f"{where}: unknown tone '{tone}'")
            nodes[req(n, "id", where)] = n
        out = [f'<figure class="band__figure"><svg class="diagram" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" '
               f'aria-labelledby="{sid}-dt {sid}-dd" focusable="false"><title id="{sid}-dt">{esc(req(d, "title", where))}</title>'
               f'<desc id="{sid}-dd">{esc(req(d, "desc", where))}</desc>'
               f'<defs><marker id="{sid}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
               f'<path d="M0 0L10 5L0 10z" class="diagram__head"/></marker></defs>']
        for g in d.get("groups", []):
            x, y, gw, gh = (int(g[k]) for k in ("x", "y", "w", "h"))
            out.append(f'<g class="diagram__group diagram__group--{esc(g.get("tone", "plain"))}"><rect x="{x}" y="{y}" width="{gw}" height="{gh}" rx="14"/>'
                       f'<text class="diagram__group-label" x="{x + 14}" y="{y + 24}">{esc(req(g, "label", where))}</text></g>')
        for e in d.get("edges", []):
            a, z = nodes[req(e, "from", where)], nodes[req(e, "to", where)]
            pts = self._route(a, z, e)
            path = "M" + " L".join(f"{px} {py}" for px, py in pts)
            dashed = " diagram__edge--refused" if e.get("refused") else ""
            out.append(f'<g class="diagram__edge{dashed}"><path d="{path}" marker-end="url(#{sid}-arrow)"/>'
                       + ("" if e.get("refused") else f'<path class="diagram__flow" d="{path}"/>'))
            if e.get("label"):
                lx, ly, auto = self._mid(pts)
                if e.get("label_at"):
                    lx, ly = e["label_at"]
                anchor = e.get("anchor", auto)
                out.append(f'<text class="diagram__label" x="{lx}" y="{ly}" text-anchor="{esc(anchor)}">{esc(e["label"])}</text>')
            out.append("</g>")
        for n in nodes.values():
            x, y, nw, nh = (int(n[k]) for k in ("x", "y", "w", "h"))
            sub = n.get("sub")
            ty = y + nh / 2 + (-4 if sub else 6)
            out.append(f'<g class="diagram__node diagram__node--{esc(n.get("tone", "plain"))}"><rect x="{x}" y="{y}" width="{nw}" height="{nh}" rx="10"/>'
                       f'<text class="diagram__title" x="{x + nw / 2:g}" y="{ty:g}" text-anchor="middle">{esc(req(n, "title", where))}</text>')
            if sub:
                out.append(f'<text class="diagram__sub" x="{x + nw / 2:g}" y="{ty + 20:g}" text-anchor="middle">{esc(sub)}</text>')
            out.append("</g>")
        out.append("</svg>")
        if d.get("caption"):
            out.append(f'<figcaption class="band__caption">{self.inline(d["caption"])}</figcaption>')
        out.append("</figure>")
        return "".join(out)

    @staticmethod
    def _side(n: dict, side: str) -> tuple[float, float]:
        x, y, w, h = (int(n[k]) for k in ("x", "y", "w", "h"))
        return {"right": (x + w, y + h / 2), "left": (x, y + h / 2), "top": (x + w / 2, y), "bottom": (x + w / 2, y + h)}[side]

    def _route(self, a: dict, z: dict, e: dict) -> list[tuple[float, float]]:
        if e.get("points"):
            return [tuple(p) for p in e["points"]]
        ax, ay = int(a["x"]) + int(a["w"]) / 2, int(a["y"]) + int(a["h"]) / 2
        zx, zy = int(z["x"]) + int(z["w"]) / 2, int(z["y"]) + int(z["h"]) / 2
        horizontal = abs(zx - ax) >= abs(zy - ay)
        s = e.get("from_side") or (("right" if zx > ax else "left") if horizontal else ("bottom" if zy > ay else "top"))
        t = e.get("to_side") or (("left" if zx > ax else "right") if horizontal else ("top" if zy > ay else "bottom"))
        p0, p1 = self._side(a, s), self._side(z, t)
        if p0[0] == p1[0] or p0[1] == p1[1]:
            return [p0, p1]
        if s in ("left", "right") and t in ("left", "right"):
            mx = (p0[0] + p1[0]) / 2
            return [p0, (mx, p0[1]), (mx, p1[1]), p1]
        if s in ("top", "bottom") and t in ("top", "bottom"):
            my = (p0[1] + p1[1]) / 2
            return [p0, (p0[0], my), (p1[0], my), p1]
        return [p0, (p1[0], p0[1]), p1] if s in ("left", "right") else [p0, (p0[0], p1[1]), p1]

    @staticmethod
    def _mid(pts: list[tuple[float, float]]) -> tuple[float, float, str]:
        best = max(zip(pts, pts[1:]), key=lambda s: abs(s[1][0] - s[0][0]) + abs(s[1][1] - s[0][1]))
        (x0, y0), (x1, y1) = best
        if y0 == y1:
            return ((x0 + x1) / 2, y0 - 8, "middle")
        return (x0 + 8, (y0 + y1) / 2 + 5, "start")

    def b_setup(self, b, where):
        sid = esc(req(b, "id", where))
        values, fields = {}, []
        for ch in req(b, "choices", where):
            name = req(ch, "name", where)
            values[name] = req(ch, "default", where)
            opts = "".join(
                f'<label class="choice"><input type="radio" name="{esc(name)}" value="{esc(req(o, "value", where))}"'
                f'{" checked" if o["value"] == ch["default"] else ""} data-setup-input><span>{esc(req(o, "label", where))}</span></label>'
                for o in req(ch, "options", where))
            fields.append(f'<fieldset class="setup__group"><legend>{esc(req(ch, "legend", where))}</legend>{opts}</fieldset>')
        adv = req(b, "advanced", where)
        extra = []
        for f in req(adv, "fields", where):
            name, kind = req(f, "name", where), req(f, "kind", where)
            fid = f"{sid}-{esc(name)}"
            hint = f'<span class="setup__hint" id="{fid}-hint">{self.inline(f["hint"])}</span>' if f.get("hint") else ""
            described = f' aria-describedby="{fid}-hint"' if f.get("hint") else ""
            if kind == "text":
                values[name] = req(f, "default", where)
                extra.append(f'<div class="setup__field"><label for="{fid}">{esc(req(f, "label", where))}</label>'
                             f'<input id="{fid}" name="{esc(name)}" type="text" value="{esc(f["default"])}" maxlength="64" '
                             f'autocomplete="off" spellcheck="false" data-setup-input{described}>{hint}</div>')
            elif kind == "select":
                values[name] = req(f, "default", where)
                opts = "".join(f'<option value="{esc(o["value"])}"{" selected" if o["value"] == f["default"] else ""}>{esc(o["label"])}</option>'
                               for o in req(f, "options", where))
                extra.append(f'<div class="setup__field"><label for="{fid}">{esc(req(f, "label", where))}</label>'
                             f'<select id="{fid}" name="{esc(name)}" data-setup-input{described}>{opts}</select>{hint}</div>')
            elif kind == "checkbox":
                values[name] = bool(f.get("default"))
                extra.append(f'<div class="setup__field setup__field--check"><label class="choice"><input id="{fid}" name="{esc(name)}" type="checkbox"'
                             f'{" checked" if f.get("default") else ""} data-setup-input{described}><span>{esc(req(f, "label", where))}</span></label>{hint}</div>')
            else:
                raise BuildError(f"{where}: unknown field kind '{kind}'")
        show = req(b, "show", where)
        values["show"] = req(show, "default", where)
        tabs = "".join(
            f'<label class="choice choice--tab"><input type="radio" name="show" value="{esc(o["value"])}"'
            f'{" checked" if o["value"] == show["default"] else ""} data-setup-input><span>{esc(o["label"])}</span></label>'
            for o in req(show, "options", where))
        data = {k: b[k] for k in ("templates", "tokens")}
        data["copy"] = req(b, "copy", where)
        for key in data["templates"]:
            if len(key.split(".")) != 3:
                raise BuildError(f"{where}: template key '{key}' is not goal.via.show")
        default = setup_text(data, values)
        blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        copy = data["copy"]
        return (
            f'<section class="section band band--setup" id="{sid}" aria-labelledby="{sid}-title"><div class="wrap">'
            f'<div class="setup" data-setup><div class="setup__choices"><h2 id="{sid}-title">{esc(req(b, "heading", where))}</h2>'
            f'<div class="setup__inputs" data-needs-js>{"".join(fields)}'
            f'<details class="setup__more"><summary>{esc(req(adv, "summary", where))}</summary><div class="setup__fields">{"".join(extra)}</div></details></div>'
            f'<p class="setup__note">{self.inline(req(b, "note", where))}</p>'
            f'<noscript><p class="setup__note">{esc(req(b, "noscript", where))}</p></noscript></div>'
            f'<div class="setup__out"><fieldset class="setup__tabs" data-needs-js><legend>{esc(req(show, "legend", where))}</legend>{tabs}</fieldset>'
            f'<pre class="code setup__code" tabindex="0" aria-label="{esc(req(b, "output_label", where))}" data-generated><code data-setup-output>{esc(default)}</code></pre>'
            f'<div class="setup__actions" data-needs-js><button type="button" class="btn btn--secondary btn--small" data-setup-copy>{esc(copy["label"])}</button>'
            f'<span class="setup__status" role="status" aria-live="polite" data-setup-status></span></div></div></div>'
            f'<script type="application/json" id="setup-data">{blob}</script></div></section>')

    def b_network(self, b, where):
        page = req(b, "words", where)
        text = attr = esc
        geo = json.loads((STATIC / "assets/network-geo.json").read_text(encoding="utf-8"))
        def network_json(value):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        def network_world(_nouns, label, _page):
            if not re.fullmatch(r"[0-9Mmh. -]+", geo["dots"]):
                raise BuildError("network land must be a plain dot path")
            return (f'<div class="nw-stage"><svg class="nw-world" viewBox="{esc(" ".join(map(str, geo["viewbox"])))}" role="img" aria-label="{esc(label)}" focusable="false">'
                    f'<path class="nw-land" d="{esc(geo["dots"])}"/></svg><div class="nw-layer" data-layer></div>'
                    '<div class="nw-pulses" data-pulses aria-hidden="true"></div><div class="nw-card" data-card role="group" hidden></div></div>')
        def network_geo_block(_nouns):
            data = {key: geo[key] for key in ("viewbox", "projection", "continents")}
            return f'<script type="application/json" class="nw-geo">{network_json(data)}</script>'
        nouns = None
        c, f = page["controls"], page["feed"]
        legend = page["legend"]
        how = page["how"]
        action = lambda name, label: (f'<button type="button" class="nw-action" data-act="{name}" aria-pressed="false" hidden>'
                                      f"<span>{text(label)}</span></button>")
        cols = lambda items: "".join(f'<th scope="col">{text(x)}</th>' for x in items)
        shows = "".join(f"<li>{text(x)}</li>" for x in how["shows"])
        never = "".join(f"<li>{text(x)}</li>" for x in how["never"])
        t = page["table"]
        main = "\n".join([
            f'<section class="nw" data-network data-mode="page" data-source="{attr(page["source"])}">',
            '<div class="wrap page-head pg-head nw-head">',
            '<div class="nw-head-text">',
            f'<p class="pg-kicker">{text(page["eyebrow"])}</p>',
            f'<h1 id="page-title">{text(page["heading"])}</h1>',
            f'<p class="lead">{text(page["lead"])}</p>',
            "</div>",
            '<div class="nw-headline">',
            f'<p class="nw-big" role="status" data-totals-big>{text(page["loading"])}</p>',
            '<p class="nw-sub" data-totals-sub></p>',
            "</div>",
            "</div>",
            '<div class="wrap nw-page">',
            '<div class="nw-controls">',
            '<div class="nw-actions">' + action("replay", c["replay"]) + action("pause", c["pause"]) + action("table", c["table"]) + "</div>",
            f'<div class="nw-group" role="group" aria-label="{attr(page["filter"]["label"])}" data-kinds hidden>'
            f'<span class="nw-group-label">{text(page["filter"]["label"])}</span></div>',
            "</div>",
            f'<p class="nw-band" data-band hidden><b>{text(page["replay_band"]["label"])}</b><span aria-hidden="true"> · </span><span>{text(page["replay_band"]["text"])}</span></p>',
            '<p class="nw-status" data-status role="status"></p><p class="nw-freshness" data-freshness></p>',
            '<div class="nw-body">',
            '<div class="nw-view" data-view-map>',
            '<figure class="nw-map">',
            network_world(nouns, page["loading"], True),
            '<p class="nw-line" data-quiet hidden></p><p class="nw-line" data-empty hidden></p>',
            '<figcaption class="nw-legend">'
            f'<span class="nw-legend-item"><span class="nw-key nw-key-dot" aria-hidden="true"></span>{text(legend["data_center"])}</span>'
            f'<span class="nw-legend-item"><span class="nw-key nw-key-continent" aria-hidden="true"></span>{text(legend["continent"])}</span>'
            "</figcaption>",
            "</figure>",
            "</div>",
            # Beside the map the feed is as tall as the map and scrolls; under it, on a phone, it shows the latest few.
            '<div class="nw-feed" data-feed-card hidden><div class="nw-feed-in">',
            f'<div class="nw-feed-head"><h2 id="nw-feed-h" data-feed-title>{text(f["title"])}</h2>'
            f'<span class="nw-state" data-state-chip data-state="unknown" hidden>{text(f["state"]["unknown"])}</span></div>',
            '<div class="nw-feed-scroll" role="region" aria-labelledby="nw-feed-h" tabindex="0">',
            '<ol class="nw-feed-list" data-feed></ol>',
            '<p class="nw-feed-empty" data-feed-empty hidden></p>',
            "</div>",
            "</div></div>",
            "</div>",
            '<div class="nw-tables" data-view-table hidden>',
            f'<div class="nw-table-wrap"><table class="nw-table"><caption>{text(t["centers_caption"])}</caption><thead><tr>{cols(t["centers_cols"])}</tr></thead>'
            '<tbody data-rows-centers></tbody></table></div>',
            f'<div class="nw-table-wrap"><table class="nw-table"><caption>{text(t["claims_caption"])}</caption><thead><tr>{cols(t["claims_cols"])}</tr></thead>'
            '<tbody data-rows-claims></tbody></table></div>',
            "</div>",
            f'<p class="nw-shown">{text(page["shown"]["text"])} <a href="#{attr(how["id"])}">{text(page["shown"]["link"])}</a> · '
            f'<a href="{attr(page["herma"]["dashboard"])}">{text(page["shown"]["herma"])} ↗</a></p>',
            f'<noscript><p class="nw-noscript">{text(page["noscript"])}</p></noscript>',
            f'<script type="application/json" class="nw-words">{network_json(page)}</script>',
            network_geo_block(nouns),
            "</div>",
            "</section>",
            '<div class="wrap nw-page nw-page-more">',
            f'<section id="{attr(how["id"])}" class="nw-about" aria-labelledby="nw-about-h">',
            f'<h2 id="nw-about-h">{text(how["heading"])}</h2>',
            '<div class="nw-about-grid">',
            f'<div><h3>{text(how["shows_heading"])}</h3><ul>{shows}</ul></div>',
            f'<div><h3>{text(how["never_heading"])}</h3><ul>{never}</ul></div>',
            "</div>",
            f'<p class="nw-optout">{text(how["opt_out"])}</p>',
            f'<p class="nw-source">{text(how["source"])} <a href="{attr(page["source"])}">{text(how["data_link"])}</a></p>',
            f'<p class="nw-credits">{text(how["credits"])}</p>',
            '<div class="actions">' + ''.join(self.button(action, where) for action in page['actions']) + '</div>',
            "</section>",
            "</div>",

        ])
        return main

    def b_story(self, b, where):
        labels = req(b, "labels", where)
        scenes = req(b, "scenes", where)
        duration = req(b, "duration_seconds", where)
        data_labels = " ".join(
            f'data-label-{esc(key.replace("_", "-"))}="{esc(value)}"'
            for key, value in labels.items()
        )
        total = len(scenes)
        parts = [
            f'<div class="story" id="teleion-story" data-duration="{esc(duration)}" {data_labels}>',
            '<div class="story-topbar">',
            f'<span class="study-label">{esc(labels["study"])}</span>',
            f'<button type="button" class="text-button js-only" id="story-contents-button" aria-expanded="false" aria-controls="story-contents">{esc(labels["contents"])}</button>',
            '</div>',
            f'<nav class="contents" id="story-contents" aria-label="{esc(labels["contents_navigation"])}" hidden>',
        ]
        for i, scene in enumerate(scenes):
            title = req(scene, "title", where)
            parts.append(
                f'<button type="button" data-story-go="{i}" aria-current="{str(i == 0).lower()}">'
                f'<span class="mono" data-generated>{i + 1:02d}</span><span>{esc(title)}</span></button>'
            )
        parts.extend([
            '</nav>',
            f'<noscript><p class="story-fallback">{esc(labels["no_script"])}</p></noscript>',
            f'<div class="presentation" id="story-presentation" tabindex="-1" role="region" aria-label="{esc(labels["presentation"])}">',
        ])
        for i, scene in enumerate(scenes):
            title = req(scene, "title", where)
            markup = req(scene, "html", where)
            # This is authored local content, never worker- or user-supplied HTML.
            # Keep the static presentation compatible with the site's CSP.
            if re.search(r"<\s*(script|style|iframe|object|embed)\b|\s(?:style|on[a-z]+)\s*=", markup, re.I):
                raise BuildError(f"{where}: unsupported active markup in story scene {i + 1}")
            if i == 0:
                markup = markup.replace('<h1 ', '<h1 id="page-title" ', 1)
            accessible = labels["scene_announcement"].format(current=i + 1, total=total, name=title)
            parts.append(
                f'<section class="slide {esc(scene.get("class_name", ""))}" '
                f'data-story-scene="{esc(req(scene, "id", where))}" '
                f'data-start="{esc(req(scene, "start", where))}" data-end="{esc(req(scene, "end", where))}" '
                f'data-title="{esc(title)}" aria-label="{esc(title)}">{markup}</section>'
            )
        parts.extend([
            '</div>',
            '<div class="control-deck js-only">',
            f'<progress class="progress-track" id="story-progress" max="{esc(duration)}" value="0" aria-label="{esc(labels["progress"])}"></progress>',
            '<div class="control-main">',
            f'<div class="scene-position"><span id="story-count" data-generated>01 / {total}</span><span id="story-name">{esc(scenes[0]["title"])}</span></div>',
            '<div class="transport">',
            f'<button type="button" class="nav-button" id="story-back" aria-label="{esc(labels["previous"])}" disabled>← <span>{esc(labels["back"])}</span></button>',
            f'<button type="button" class="play-button" id="story-play" aria-pressed="false">{esc(labels["play"])}</button>',
            f'<button type="button" class="nav-button" id="story-next" aria-label="{esc(labels["next"])}"><span>{esc(labels["forward"])}</span> →</button>',
            '</div>',
            '<div class="clock"><span id="story-clock" data-generated>0:00</span><span data-generated> / 3:00</span></div>',
            '</div>',
            f'<div class="control-notes"><span>{esc(labels["keyboard_hint"])}</span><label><input id="story-reduce-motion" type="checkbox">{esc(labels["reduce_motion"])}</label></div>',
            '</div>',
            '<p class="sr-only" id="story-announcement" role="status" aria-live="polite"></p>',
            '</div>',
        ])
        return "".join(parts)

    def page(self, page: dict, layout: str) -> str:
        where = page["id"]
        route = req(page, "route", where)
        site = self.site
        nav = req(site, "nav", "site")
        nav_items = []
        for it in req(nav, "items", "site/nav"):
            current = ' aria-current="page"' if it["href"] == route else ""
            nav_items.append(f'<a href="{esc(it["href"])}"{current}>{esc(it["label"])}</a>')
        footer = req(site, "footer", "site")
        footer_items = " ".join(f"{self.link(it['label'], it['href'])}" for it in req(footer, "links", "site/footer"))
        scripts = ""
        kinds = {b.get("type") for b in page["blocks"]}
        if "film" in kinds or "video" in kinds or "explainer" in kinds:
            scripts = '<script src="/assets/home-film.js" defer></script>\n'

        noindex = page.get("noindex", False)
        canonical = self.origin + ("/" if noindex else route)
        banner = esc(req(site, "banner_line", "site"))
        values = {
            "lang": req(site, "lang", "site"),
            "title": req(page, "title", where),
            "description": req(page, "description", where),
            "robots": '<meta name="robots" content="noindex">\n' if noindex else "",
            "canonical": canonical,
            "site_name": req(site["brand"], "name", "site/brand"),
            "og_image": req(site, "og_image", "site"),
            "scripts": scripts,
            "skip_link": req(site, "skip_link", "site"),
            "home_label": req(site["brand"], "home_label", "site/brand"),
            "brand": req(site["brand"], "name", "site/brand"),
            "nav_label": req(nav, "label", "site/nav"),
            "footer_label": req(footer, "label", "site/footer"),
            "footer_line": req(footer, "line", "site/footer"),
            "footer_beta": req(footer, "beta", "site/footer"),
        }
        raw = {
            "banner": banner,
            "mark": self.mark,
            "nav_items": " ".join(nav_items),
            "footer_items": footer_items,
            "blocks": "\n".join(self.block(b, where) for b in req(page, "blocks", where)),
            "robots": values.pop("robots"),
            "scripts": values.pop("scripts"),
        }

        def fill(m: re.Match) -> str:
            key = m.group(1)
            if key in raw:
                return raw[key]
            if key in values:
                return esc(values[key])
            raise BuildError(f"template placeholder '{key}' has no value")

        raw["blocks"] = textify(raw["blocks"])
        values["text_css"] = TEXT_CSS
        raw["text_css"] = TEXT_CSS
        return re.sub(r"\{\{(\w+)\}\}", fill, layout)


BANNER_FIELDS = ("label", "lead_strong", "lead", "lead_short", "chip", "copy_text", "copy", "copy_short", "copied", "copied_status",
                 "copy_failed", "more", "less", "panel_title", "panel_note", "panel_line", "can_title", "can", "rules_title", "rules", "links")
BANNER_ICON = ('<svg class="agent-fallback-icon" viewBox="0 0 40 40" width="18" height="18" aria-hidden="true" focusable="false">'
               '<circle cx="20" cy="20" r="17" fill="none" stroke="currentColor" stroke-width="2.6"/>'
               '<path d="M20 12v-3h-3" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>'
               '<rect x="12" y="14" width="16" height="12" rx="3" fill="none" stroke="currentColor" stroke-width="2.6"/>'
               '<path d="M17 19v2M23 19v2" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/></svg>')


def setup_text(data: dict, values: dict) -> str:
    key = f"{values['goal']}.{values['via']}.{values['show']}"
    if key not in data["templates"]:
        raise BuildError(f"setup: no template '{key}'")
    agent = re.sub(r"[^a-zA-Z0-9 ._:-]", "", str(values["agent"]))[:64] or "agent"
    swap = {"agent": agent, "queue": values["queue"], "map": data["tokens"]["map_on" if values["map"] else "map_off"]}
    return re.sub(r"\{(agent|queue|map)\}", lambda m: swap[m.group(1)], data["templates"][key])


def write_agent_files(out: Path, llms_text: str) -> list[str]:
    """The discovery files agents look for: where the MCP servers are, with no key. Pointers only."""
    fabric, shim, eranos, site = "https://fabric.teleionlabs.com", "https://shim.teleionlabs.com", "https://eranosforgood.org", "https://teleionlabs.com"
    servers = {
        "teleion-fabric": {"type": "http", "url": f"{fabric}/mcp", "description": "Shared Tasks, Task Objects, queues and search. No key."},
        "teleion-fabric-shim": {"type": "http", "url": f"{shim}/mcp", "description": "24 tools: search, list_task_objects, read_task_object, list_tasks, read_task, find, list_builtin_provisions, preview_shim, extract_entities, next_tasks, resume_session, claim_task, heartbeat_task, checkpoint_task, submit_task, release_task, frontier_dedup, grid_optimize, rl_policy_step, compile_macro_dag, bootstrap_local_cell, list_weight_models, run_weight_model and register_weight_model. No key."},
        "eranos": {"type": "http", "url": f"{eranos}/mcp", "description": "Nonprofit agent kit catalog, read-only. No key."},
    }
    card = {
        "name": "teleion-fabric",
        "title": "Teleion Fabric",
        "description": "Teleion Fabric: shared work for AI agents. Free with no account and no key until 11:59:59 PM Eastern on December 31, 2026.",
        "url": site,
        "documentationUrl": f"{site}/llms.txt",
        "transport": {"type": "streamable-http", "url": f"{fabric}/mcp"},
        "authentication": {"required": False, "schemes": []},
    }
    plugin = {
        "schema_version": "v1",
        "name_for_human": "Teleion Fabric",
        "name_for_model": "teleion_fabric",
        "description_for_human": "Shared work for AI agents. Free with no account and no key through December 31, 2026.",
        "description_for_model": f"Find and do shared Tasks, read Task Objects, and search. Read {site}/llms.txt first. Agents that can only open URLs can claim and submit with GET at {shim}/q.",
        "auth": {"type": "none"},
        "api": {"type": "openapi", "url": f"{fabric}/openapi.json"},
        "legal_info_url": f"{site}/terms/",
    }
    connect = {
        "product": "Teleion Fabric",
        "authentication": "none, through December 31, 2026, 11:59:59 PM Eastern",
        "instructions": f"{site}/llms.txt",
        "ways_to_connect": f"{site}/docs/#ways",
        "mcp": servers,
        "queue_and_storage": f"{fabric}/connect.json",
        "model_shim_and_open_work": f"{shim}/connect.json",
        "agent_kits": f"{eranos}/connect.json",
    }
    files = {
        ".well-known/mcp.json": {"mcpServers": servers, "documentation": f"{site}/llms.txt", "authentication": "none"},
        ".well-known/mcp/server.json": card,
        ".well-known/mcp/server-card.json": card,
        ".well-known/agent.json": card,
        ".well-known/agent-card.json": card,
        ".well-known/ai-plugin.json": plugin,
        "connect.json": connect,
    }
    for rel, doc in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    (out / "llms-full.txt").write_text(llms_text, encoding="utf-8")
    return [*files, "llms-full.txt"]


def render_llms(llms: dict) -> str:
    lines = [f"# {req(llms, 'title', 'llms')}", "", f"> {req(llms, 'summary', 'llms')}", ""]
    for sec in req(llms, "sections", "llms"):
        lines.append(f"## {req(sec, 'heading', 'llms/section')}")
        lines.extend(f"- {line}" for line in req(sec, "lines", "llms/section"))
        lines.append("")
    return "\n".join(lines)


def word_list_path() -> Path:
    """The real list. Only `make` asks for the made-up sample, only when the real one is absent; a deploy never can."""
    if not SHARED_WORDS.is_file() and not os.environ.get("BANNED_WORDS") and os.environ.get("TELEION_SAMPLE_WORD_LIST") == "1":
        print("build: ON THE MADE-UP SAMPLE WORD LIST. The full list is kept private; this build must not be deployed.", file=sys.stderr)
        return SAMPLE_WORDS
    return SHARED_WORDS


def shared_words() -> tuple[list[str], list[str]]:
    path = word_list_path()
    if not path.is_file():
        raise BuildError(f"the shared banned-words list is missing: {SHARED_WORDS} (set BANNED_WORDS to its path)")
    words = json.loads(path.read_text(encoding="utf-8"))
    patterns = []
    for cat in WORD_CATEGORIES:
        found = words.get(cat, {}).get("patterns")
        if not found:
            raise BuildError(f"the shared banned-words list has no '{cat}' patterns")
        for pat in found:
            re.compile(pat)
            patterns.append(pat)
    hashes = words.get("private_names", {}).get("sha256") or []
    if not hashes or not all(re.fullmatch(r"[0-9a-f]{64}", h) for h in hashes):
        raise BuildError("the shared banned-words list has no valid private_names.sha256 hashes")
    return patterns, hashes


def write_worker_words(patterns: list[str], hashes: list[str]) -> None:
    pats = ",\n  ".join(json.dumps(p) for p in patterns)
    hexes = ",\n  ".join(json.dumps(h) for h in hashes)
    WORKER_WORDS.write_text(
        "// Generated by build.py from the shared banned-words list (rule 16). Gitignored: never commit it.\n"
        f"export const BANNED_PATTERNS = [\n  {pats},\n];\n"
        f"export const PRIVATE_SHA256 = [\n  {hexes},\n];\n", encoding="utf-8")


def write_worker_text(link: dict) -> None:
    digest = base64.b64encode(hashlib.sha256(TEXT_CSS.encode()).digest()).decode()
    WORKER_TEXT.write_text(
        "// Generated by build.py. Gitignored: never commit it.\n"
        f"export const TEXT_CSS = {json.dumps(TEXT_CSS)};\n"
        f"export const TEXT_CSS_HASH = {json.dumps('sha256-' + digest)};\n"
        f"export const LINK_COPY = {json.dumps(link, ensure_ascii=False)};\n", encoding="utf-8")


def write_worker_media() -> None:
    media_dir = STATIC / "media"
    sizes = {}
    if media_dir.is_dir():
        for mp4 in sorted([*media_dir.glob("*.mp4"), *media_dir.glob("*.mp3")]):
            sizes[f"/media/{mp4.name}"] = mp4.stat().st_size
    entries = ",\n  ".join(f"{json.dumps(k)}: {v}" for k, v in sizes.items())
    WORKER_MEDIA.write_text(
        "// Generated by build.py from static/media/*.mp4 sizes. Gitignored: never commit it.\n"
        f"export const MEDIA_BYTES = {{\n  {entries}\n}};\n"
        f"export const MAIN_FILM = {json.dumps('/media/' + load('site')['main_film'] + '.mp4')};\n", encoding="utf-8")


def build(out: Path) -> list[str]:
    write_worker_words(*shared_words())
    write_worker_media()
    data = {name: load(name) for name in REQUIRED}
    # One name for the main film, in content/site.json. /media/ is cached for a year, so each new cut takes a new name.
    film = req(data["site"], "main_film", "site")
    files = {"media": f"/media/{film}.mp4", "captions": f"/media/{film}.vtt", "poster": f"/media/{data['site'].get('main_film_poster') or film}.jpg"}
    for page in ("home", "video"):
        for b in data[page]["blocks"]:
            if b.get("type") == "film": b["video"].update(files)
            elif b.get("type") == "video": b.update(files)
    site = data["site"]
    r = Renderer(site)
    layout = (TEMPLATES / "layout.html").read_text(encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    keep = {"home-film.js", "network-geo.json", "favicon.svg", "favicon-32.png", "apple-touch-icon.png", "og-teleion-1200x630.png", "video-poster-teleion.svg", "video-preview.svg"}
    def ignore(folder, names):
        if Path(folder) == STATIC:
            return [n for n in names if n == "fonts"]
        if Path(folder) == STATIC / "assets":
            return [n for n in names if n not in keep]
        return []
    shutil.copytree(STATIC, out, ignore=ignore)
    write_worker_text(data["link"])
    written = []
    sitemap = []
    for name in PAGES:
        page = data[name]
        target = out / req(page, "output", page["id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        markup = r.page(page, layout)
        target.write_text(markup, encoding="utf-8")
        written.append(str(target.relative_to(out)))
        if name != "errors":
            plain_copy = to_text(markup)
            twin = out / text_route(page["route"])
            twin.parent.mkdir(parents=True, exist_ok=True)
            twin.write_text(plain_copy, encoding="utf-8")
            written.append(str(twin.relative_to(out)))
            if page["route"] != "/" and target.name == "index.html":
                dir_twin = target.parent / "index.txt"
                dir_twin.write_text(plain_copy, encoding="utf-8")
                written.append(str(dir_twin.relative_to(out)))
        if not page.get("noindex"):
            sitemap.append(r.origin + page["route"])
    (out / "llms.txt").write_text(render_llms(data["llms"]), encoding="utf-8")
    found: dict[str, str] = {}
    for name in PAGES:
        for b in data[name]["blocks"]:
            if b.get("type") != "status":
                continue
            for it in b["items"]:
                if found.setdefault(it["id"], it["health"]) != it["health"]:
                    raise BuildError(f"{name}: status item '{it['id']}' names a different health address on another page")
    if not found:
        raise BuildError("no page has a status block with health sources")
    sources = [{"id": k, "url": v} for k, v in found.items()]
    (out / "status.sources.json").write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {r.origin}/sitemap.xml\n", encoding="utf-8")
    urls = "".join(f"  <url><loc>{esc(u)}</loc></url>\n" for u in sitemap)
    (out / "sitemap.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}</urlset>\n',
        encoding="utf-8")
    sec = req(data["security"], "security_txt", "security")
    lines = [f"Contact: {c}" for c in req(sec, "contact", "security_txt")]
    lines += [f"Expires: {req(sec, 'expires', 'security_txt')}",
              f"Preferred-Languages: {req(sec, 'preferred_languages', 'security_txt')}",
              f"Canonical: {req(sec, 'canonical', 'security_txt')}",
              f"Policy: {req(sec, 'policy', 'security_txt')}"]
    (out / ".well-known").mkdir(exist_ok=True)
    (out / ".well-known" / "security.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    written += ["llms.txt", "status.sources.json", "robots.txt", "sitemap.xml", ".well-known/security.txt"]
    written += write_agent_files(out, render_llms(data["llms"]))
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(HERE / "dist"))
    args = ap.parse_args()
    try:
        files = build(Path(args.out))
    except (BuildError, json.JSONDecodeError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built {len(files)} files into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
