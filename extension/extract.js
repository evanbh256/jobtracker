// Rule-based job posting extractor. No network, no LLM.
// Pure functions over a DOM Document + URL string, so the same code runs
// injected into a page (popup.js) and under Node/jsdom in tests.
//
// Priority per field: (1) schema.org JobPosting JSON-LD, (2) platform
// selectors, (3) generic fallbacks. Each field carries a confidence:
//   high   - JSON-LD or a platform-specific selector
//   medium - derived (e.g. company from a URL slug)
//   low    - generic guess (og:title, page title, domain, biggest text block)
//   ""     - nothing found
(function (root) {
  "use strict";

  // ---------- URL handling ----------

  const PLATFORM_HOSTS = [
    ["greenhouse", /(^|\.)greenhouse\.io$/],
    ["lever", /(^|\.)lever\.co$/],
    ["workday", /(^|\.)myworkday(jobs|site)\.com$/],
    ["handshake", /(^|\.)joinhandshake\.com$/],
    ["linkedin", /(^|\.)linkedin\.com$/],
    ["icims", /(^|\.)icims\.com$/],
    ["ashby", /(^|\.)ashbyhq\.com$/],
    ["smartrecruiters", /(^|\.)smartrecruiters\.com$/],
  ];

  // Query params that identify a job and must survive canonicalization.
  const KEEP_PARAMS = new Set(["gh_jid", "jobid", "job_id", "jid", "jk", "reqid",
    "requisitionid", "job", "id", "token", "pid", "postingid", "currentjobid"]);

  function detectPlatform(url, doc) {
    let host = "";
    try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return "other"; }
    for (const [name, re] of PLATFORM_HOSTS) if (re.test(host)) return name;
    // Greenhouse boards embedded on a company's own careers site.
    try { if (new URL(url).searchParams.has("gh_jid")) return "greenhouse"; } catch (e) { /* */ }
    if (doc && doc.querySelector('#grnhse_app, iframe[src*="greenhouse.io"]')) return "greenhouse";
    return "other";
  }

  function canonicalUrl(url) {
    let u;
    try { u = new URL(url); } catch (e) { return url || ""; }
    const platform = detectPlatform(url);
    u.hash = "";
    u.hostname = u.hostname.toLowerCase();
    if (platform === "linkedin") {
      const id = u.searchParams.get("currentJobId") || (u.pathname.match(/\/jobs\/view\/(?:[^/]*?-)?(\d+)/) || [])[1];
      if (id) return `https://www.linkedin.com/jobs/view/${id}/`;
    }
    // Apply-form subpages collapse to the posting itself.
    if (platform === "lever") u.pathname = u.pathname.replace(/\/apply\/?$/, "");
    if (platform === "ashby") u.pathname = u.pathname.replace(/\/application\/?$/, "");
    if (platform === "workday") u.pathname = u.pathname.replace(/\/apply(\/.*)?$/, "");
    if (platform === "smartrecruiters") u.pathname = u.pathname.replace(/\/apply\/?$/, "");
    const keep = [];
    for (const [k, v] of u.searchParams) {
      if (KEEP_PARAMS.has(k.toLowerCase()) && !(platform === "linkedin")) keep.push([k, v]);
    }
    keep.sort((a, b) => a[0].localeCompare(b[0]));
    u.search = keep.length ? "?" + keep.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&") : "";
    let out = u.toString();
    if (out.endsWith("/") && u.pathname !== "/" && !u.search) out = out.slice(0, -1);
    return out;
  }

  function jobIdFromUrl(url, platform) {
    let u;
    try { u = new URL(url); } catch (e) { return ""; }
    const p = u.pathname;
    const q = (k) => u.searchParams.get(k) || "";
    const m = (re) => (p.match(re) || [])[1] || "";
    switch (platform) {
      case "greenhouse": return q("gh_jid") || m(/\/jobs\/(\d+)/) || q("token");
      case "lever": return m(/^\/[^/]+\/([0-9a-f]{8}-[0-9a-f-]{27,})/i);
      case "workday": return m(/_([A-Za-z]*-?\d[\w-]*)(?:\/|$)/);
      case "handshake": return m(/\/(?:jobs|job-search)\/(\d+)/) || q("jobId");
      case "linkedin": return q("currentJobId") || m(/\/jobs\/view\/(?:[^/]*?-)?(\d+)/);
      case "icims": return m(/\/jobs\/(\d+)/);
      case "ashby": return m(/^\/[^/]+\/([0-9a-f]{8}-[0-9a-f-]{27,})/i);
      case "smartrecruiters": return m(/^\/[^/]+\/(\d{6,})/);
      default: return q("gh_jid") || q("jobId") || q("jobid") || q("jk") || q("job_id") || "";
    }
  }

  function titleCaseSlug(s) {
    return (s || "").replace(/[-_]+/g, " ").replace(/\s+/g, " ").trim()
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function companyFromUrl(url, platform) {
    let u;
    try { u = new URL(url); } catch (e) { return ""; }
    const seg = u.pathname.split("/").filter(Boolean);
    const host = u.hostname.toLowerCase();
    switch (platform) {
      case "greenhouse":
        if (/greenhouse\.io$/.test(host)) return seg[0] === "embed" ? titleCaseSlug(u.searchParams.get("for")) : titleCaseSlug(seg[0]);
        return "";
      case "lever": case "ashby": return titleCaseSlug(seg[0]);
      case "smartrecruiters": return /^(jobs|careers)\./.test(host) ? titleCaseSlug(seg[0]) : "";
      case "workday": return titleCaseSlug(host.split(".")[0]);
      case "icims": return titleCaseSlug(host.split(".")[0].replace(/^(careers|jobs|uscareers|external)-/, ""));
      default: return "";
    }
  }

  function companyFromDomain(url) {
    let host;
    try { host = new URL(url).hostname.toLowerCase(); } catch (e) { return ""; }
    const parts = host.replace(/^www\./, "").split(".");
    const label = parts.length >= 2 ? parts[parts.length - 2] : parts[0];
    return titleCaseSlug(label);
  }

  // ---------- text helpers ----------

  const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "UL", "OL", "LI", "H1", "H2", "H3",
    "H4", "H5", "H6", "TABLE", "TR", "BR", "HR", "HEADER", "FOOTER", "MAIN", "DL", "DT", "DD",
    "BLOCKQUOTE", "PRE"]);
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "BUTTON", "INPUT",
    "SELECT", "TEXTAREA", "IFRAME", "FORM"]);

  // innerText-like conversion that also works in jsdom (which lacks innerText layout).
  function nodeText(node) {
    const out = [];
    (function walk(n) {
      if (n.nodeType === 3) { out.push(n.nodeValue.replace(/\s+/g, " ")); return; }
      if (n.nodeType !== 1 && n.nodeType !== 9 && n.nodeType !== 11) return;
      const tag = n.tagName || "";
      if (SKIP.has(tag)) return;
      if (n.getAttribute && (n.getAttribute("aria-hidden") === "true" || n.hasAttribute("hidden"))) return;
      const block = BLOCK.has(tag);
      if (block) out.push("\n");
      if (tag === "LI") out.push("- ");
      for (const c of n.childNodes) walk(c);
      if (block) out.push("\n");
      if (tag === "TD" || tag === "TH") out.push("\t");
    })(node);
    let text = out.join("")
      .split("\n").map((l) => l.replace(/[ \t ]+/g, " ").trim()).join("\n")
      .replace(/\n{3,}/g, "\n\n").replace(/^- \n/gm, "").trim();
    // Keep list items tight: "- a\n\n- b" -> "- a\n- b".
    let prev;
    do { prev = text; text = text.replace(/^(- .*)\n\n(?=- )/m, "$1\n"); } while (text !== prev);
    return text;
  }

  function clean(s) {
    return (s || "").replace(/\s+/g, " ").trim();
  }

  function decodeEntities(doc, s) {
    if (!/&[#a-z0-9]+;/i.test(s)) return s;
    const t = parseHtml(doc, "<textarea>" + s.replace(/</g, "&lt;") + "</textarea>");
    return t.querySelector("textarea").value;
  }

  // Inert parse: DOMParser documents never run scripts or load images.
  function parseHtml(doc, html) {
    const win = (doc && doc.defaultView) || root;
    const P = (win && win.DOMParser) || root.DOMParser;
    return new P().parseFromString(html, "text/html");
  }

  function htmlToText(doc, html) {
    if (!html) return "";
    // JSON-LD descriptions are sometimes entity-escaped HTML.
    let s = String(html);
    if (/&lt;\w/.test(s) && !/<\w/.test(s)) s = decodeEntities(doc, s);
    return nodeText(parseHtml(doc, s).body);
  }

  function isoDate(s) {
    if (!s) return "";
    const m = String(s).match(/^(\d{4}-\d{2}-\d{2})/);
    if (m) return m[1];
    const d = new Date(s);
    if (!isNaN(d)) return d.toISOString().slice(0, 10);
    return clean(s);
  }

  // ---------- (1) JSON-LD ----------

  function findJobPosting(doc) {
    const nodes = doc.querySelectorAll('script[type="application/ld+json"]');
    const isJob = (o) => o && typeof o === "object" &&
      [].concat(o["@type"] || []).some((t) => String(t).toLowerCase() === "jobposting");
    for (const n of nodes) {
      let data;
      try { data = JSON.parse(n.textContent.trim().replace(/^\s*\/\/.*$/gm, "")); } catch (e) { continue; }
      const stack = [data];
      while (stack.length) {
        const o = stack.pop();
        if (Array.isArray(o)) { stack.push(...o); continue; }
        if (!o || typeof o !== "object") continue;
        if (isJob(o)) return o;
        if (o["@graph"]) stack.push(o["@graph"]);
        if (o.mainEntity) stack.push(o.mainEntity);
      }
    }
    return null;
  }

  function ldLocation(jp) {
    const out = [];
    for (const loc of [].concat(jp.jobLocation || [])) {
      const a = (loc && loc.address) || loc;
      if (!a) continue;
      if (typeof a === "string") { out.push(a); continue; }
      const country = typeof a.addressCountry === "object" ? (a.addressCountry.name || "") : (a.addressCountry || "");
      const parts = [a.addressLocality, a.addressRegion, country].map(clean).filter(Boolean);
      // Drop a trailing country when it's the US default and we already have city/state.
      if (parts.length === 3 && /^(us|usa|united states)$/i.test(parts[2])) parts.pop();
      if (parts.length) out.push(parts.join(", "));
      else if (loc.name) out.push(clean(loc.name));
    }
    const uniq = [...new Set(out)];
    let s = uniq.slice(0, 3).join("; ") + (uniq.length > 3 ? ` (+${uniq.length - 3} more)` : "");
    if (String(jp.jobLocationType || "").toUpperCase().includes("TELECOMMUTE")) s = s ? `Remote; ${s}` : "Remote";
    return s;
  }

  function fromJsonLd(doc) {
    const jp = findJobPosting(doc);
    if (!jp) return {};
    const org = jp.hiringOrganization;
    const company = typeof org === "string" ? org : (org && org.name) || "";
    const ident = jp.identifier;
    const jobId = typeof ident === "object" && ident ? (ident.value || ident["@value"] || "") : (ident || "");
    return {
      title: clean(decodeEntities(doc, String(jp.title || ""))),
      company: clean(decodeEntities(doc, String(company))),
      location: ldLocation(jp),
      job_id: clean(String(jobId)),
      posted_date: isoDate(jp.datePosted),
      jd_text: htmlToText(doc, jp.description),
    };
  }

  // ---------- (2) platform selectors ----------
  // Hand-written against the platforms' known markup. Sites change their DOM;
  // when a selector misses we fall through to generic rules with low confidence.

  const PLATFORM_SELECTORS = {
    greenhouse: {
      title: [".job__title h1", "h1.section-header", "h1.app-title", "#header h1", ".job-title"],
      company: [".company-name", "#header .company-name"],
      location: [".job__location", "#header .location", ".location"],
      jd: [".job__description", "#content", "#job_description"],
    },
    lever: {
      title: [".posting-headline h2", ".posting-header h2"],
      company: [],
      location: [".posting-categories .location", ".posting-category.location", ".sort-by-time.posting-category"],
      jd: ['[data-qa="job-description"]', ".section-wrapper.page-full-width", ".content .section-wrapper"],
    },
    workday: {
      title: ['[data-automation-id="jobPostingHeader"]', 'h2[data-automation-id="jobPostingHeader"]'],
      company: [],
      location: ['[data-automation-id="locations"] dd', '[data-automation-id="locations"]'],
      job_id: ['[data-automation-id="requisitionId"] dd', '[data-automation-id="requisitionId"]'],
      posted_date: ['[data-automation-id="postedOn"] dd', '[data-automation-id="postedOn"]'],
      jd: ['[data-automation-id="jobPostingDescription"]'],
    },
    handshake: {
      title: ['[data-hook="job-title"]', 'h1[class*="title"]', "main h1", "h1"],
      company: ['[data-hook="employer-name"]', 'a[href*="/employers/"] h2', 'a[href*="/e/"]', 'a[href*="/employers/"]'],
      location: ['[data-hook="job-location"]', '[class*="location"]'],
      jd: ['[data-hook="job-description"]', '[class*="description"]'],
    },
    linkedin: {
      title: [".job-details-jobs-unified-top-card__job-title h1", ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title", ".top-card-layout__title", "h1.topcard__title"],
      company: [".job-details-jobs-unified-top-card__company-name a", ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name", ".topcard__org-name-link", ".topcard__flavor a"],
      location: [".job-details-jobs-unified-top-card__primary-description-container .tvm__text:first-child",
        ".job-details-jobs-unified-top-card__bullet", ".topcard__flavor--bullet", ".jobs-unified-top-card__bullet"],
      jd: ["#job-details", ".jobs-description__content", ".show-more-less-html__markup", ".description__text"],
    },
    icims: {
      title: [".iCIMS_Header h1", "h1.iCIMS_Header", ".iCIMS_JobHeaderTitle", "h1"],
      company: [],
      location: [".iCIMS_JobHeaderGroup .iCIMS_JobHeaderData", ".header.left span:not(.field-label)"],
      jd: [".iCIMS_JobContent", ".iCIMS_InfoMsg_Job", "#iCIMS_Content"],
    },
    ashby: {
      title: ['h1[class*="title"]', "h1"],
      company: [],
      location: ['[class*="_location"]', '[class*="locationValue"]'],
      jd: ['[class*="descriptionText"]', "#overview", '[class*="_description"]'],
    },
    smartrecruiters: {
      title: ["h1.job-title", '[itemprop="title"]', "h1"],
      company: ['[itemprop="hiringOrganization"] [itemprop="name"]', ".header-logo img[alt]"],
      location: ["spl-job-location[formattedaddress]", '[itemprop="jobLocation"]', ".job-detail-location"],
      jd: ['[itemprop="description"]', ".job-sections", ".job-description"],
    },
  };

  function firstText(doc, selectors) {
    for (const sel of selectors || []) {
      let el;
      try { el = doc.querySelector(sel); } catch (e) { continue; }
      if (!el) continue;
      if (el.tagName === "META") { const c = clean(el.getAttribute("content")); if (c) return c; continue; }
      if (el.tagName === "IMG") { const a = clean(el.getAttribute("alt")); if (a) return a.replace(/\s+logo$/i, ""); continue; }
      if (el.tagName === "SPL-JOB-LOCATION") { const a = clean(el.getAttribute("formattedaddress")); if (a) return a; continue; }
      const t = clean(nodeText(el));
      if (t) return t;
    }
    return "";
  }

  function firstBlock(doc, selectors) {
    for (const sel of selectors || []) {
      let el;
      try { el = doc.querySelector(sel); } catch (e) { continue; }
      if (!el) continue;
      const t = nodeText(el);
      if (t.length > 80) return t;
    }
    return "";
  }

  function fromPlatform(doc, platform) {
    const s = PLATFORM_SELECTORS[platform];
    if (!s) return {};
    const out = {
      title: firstText(doc, s.title),
      company: firstText(doc, s.company),
      location: firstText(doc, s.location),
      jd_text: firstBlock(doc, s.jd),
    };
    if (s.job_id) out.job_id = firstText(doc, s.job_id);
    if (s.posted_date) out.posted_date = firstText(doc, s.posted_date).replace(/^posted\s*(on)?\s*/i, "");
    // Lever/Workday/Ashby/iCIMS page titles are reliably "Company - Title" or "Title - Company".
    if (!out.company && platform === "lever") {
      const t = clean(doc.title).split(/\s+[-–|]\s+/);
      if (t.length >= 2) out.company = t[0];
    }
    // Greenhouse legacy renders "at Company".
    out.company = out.company.replace(/^at\s+/i, "");
    // Strip label prefixes like "Location" / "Locations" that platforms render.
    out.location = out.location.replace(/^locations?\s*:?\s*/i, "");
    out.job_id = (out.job_id || "").replace(/^(job requisition id|job id|req(uisition)? id)\s*:?\s*/i, "");
    if (platform === "linkedin" && out.title.length > 200) out.title = "";
    return out;
  }

  // ---------- (3) generic fallbacks ----------

  function meta(doc, prop) {
    const el = doc.querySelector(`meta[property="${prop}"], meta[name="${prop}"]`);
    return el ? clean(el.getAttribute("content")) : "";
  }

  function splitTitle(raw, company) {
    // "Software Engineer Intern - Acme | Careers" -> title part
    let t = clean(raw);
    const parts = t.split(/\s+[|–—-]\s+|\s+at\s+/i).map(clean).filter(Boolean);
    const generic = /^(careers?|jobs?|job board|home|apply|job application|job details)$/i;
    const cands = parts.filter((p) => !generic.test(p) && !(company && p.toLowerCase() === company.toLowerCase()));
    return cands[0] || t;
  }

  function largestTextBlock(doc) {
    const body = doc.body;
    if (!body) return "";
    const scores = new Map();
    for (const p of body.querySelectorAll("p, li")) {
      if (p.closest("nav, header, footer, aside, form")) continue;
      const len = clean(p.textContent).length;
      if (!len) continue;
      for (let a = p.parentElement; a && a !== body.parentElement; a = a.parentElement) {
        scores.set(a, (scores.get(a) || 0) + len);
      }
    }
    let max = 0;
    for (const v of scores.values()) max = Math.max(max, v);
    if (max < 200) return "";
    // Deepest container that still holds most of the paragraph text.
    let best = null, bestDepth = -1;
    for (const [elm, v] of scores) {
      if (v < 0.8 * max) continue;
      let d = 0;
      for (let a = elm; a; a = a.parentElement) d++;
      if (d > bestDepth) { best = elm; bestDepth = d; }
    }
    return best ? nodeText(best) : "";
  }

  function fromGeneric(doc, url) {
    const siteName = meta(doc, "og:site_name");
    const company = siteName || companyFromDomain(url);
    return {
      title: splitTitle(meta(doc, "og:title") || doc.title || "", company),
      company,
      location: "",
      jd_text: largestTextBlock(doc),
    };
  }

  // ---------- merge ----------

  const PLATFORM_NAMES = /^(linkedin|greenhouse|lever|workday|handshake|icims|ashby|smartrecruiters|indeed|glassdoor|careers?|jobs?)$/i;

  function extract(doc, url) {
    const platform = detectPlatform(url, doc);
    const ld = fromJsonLd(doc);
    const pf = fromPlatform(doc, platform);
    const gen = fromGeneric(doc, url);
    const slugCompany = companyFromUrl(url, platform);

    const fields = {};
    function pick(name, sources) {
      for (const [val, conf, src] of sources) {
        const v = clean(val);
        if (!v) continue;
        if (name === "company" && PLATFORM_NAMES.test(v)) continue;
        if (name === "title" && v.length > 200) continue;
        fields[name] = { value: v, confidence: conf, source: src };
        return;
      }
      fields[name] = { value: "", confidence: "", source: "" };
    }
    pick("title", [[ld.title, "high", "json-ld"], [pf.title, "high", platform], [gen.title, "low", "page title"]]);
    pick("company", [[ld.company, "high", "json-ld"], [pf.company, "high", platform],
      [slugCompany, "medium", "url"], [gen.company, "low", "domain"]]);
    pick("location", [[ld.location, "high", "json-ld"], [pf.location, "high", platform]]);
    pick("job_id", [[ld.job_id, "high", "json-ld"], [pf.job_id, "high", platform],
      [jobIdFromUrl(url, platform), "high", "url"]]);
    pick("posted_date", [[ld.posted_date, "high", "json-ld"], [pf.posted_date, "medium", platform]]);

    // JD keeps its line breaks, so pick it without clean().
    const jdSources = [[ld.jd_text, "high", "json-ld"], [pf.jd_text, "high", platform], [gen.jd_text, "low", "largest text block"]];
    const jd = jdSources.find(([v]) => v && v.trim().length > 80);
    fields.jd_text = jd ? { value: jd[0].trim(), confidence: jd[1], source: jd[2] } : { value: "", confidence: "", source: "" };

    const canon = canonicalUrl(url);
    fields.url = { value: canon, confidence: canon ? "high" : "", source: "page url" };
    fields.source = { value: platform, confidence: platform === "other" ? "medium" : "high", source: "url" };
    return fields;
  }

  // Score used to choose between frames (e.g. iCIMS renders the job in an iframe).
  function score(fields) {
    let s = 0;
    for (const k of ["title", "company", "location", "job_id"]) {
      const c = fields[k] && fields[k].confidence;
      s += c === "high" ? 3 : c === "medium" ? 2 : c === "low" ? 1 : 0;
    }
    s += Math.min(5, ((fields.jd_text && fields.jd_text.value) || "").length / 500);
    return s;
  }

  const api = { extract, score, canonicalUrl, detectPlatform, jobIdFromUrl, companyFromUrl,
    htmlToText, nodeText, findJobPosting, splitTitle };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JobExtract = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
