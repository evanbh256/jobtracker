// Extractor tests against hand-built fixtures (see tests/fixtures/README.md).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { JSDOM } = require("jsdom");
const X = require("../extension/extract.js");

function run(fixture, url) {
  const html = fs.readFileSync(path.join(__dirname, "fixtures", fixture), "utf8");
  const dom = new JSDOM(html, { url });
  return X.extract(dom.window.document, url);
}
const v = (f) => Object.fromEntries(Object.entries(f).map(([k, x]) => [k, x.value]));

test("greenhouse: JSON-LD wins, tracking params stripped", () => {
  const f = run("greenhouse.html", "https://job-boards.greenhouse.io/acmerobotics/jobs/4567890?gh_src=abc123&utm_source=linkedin#app");
  const r = v(f);
  assert.equal(r.title, "Robotics Software Intern (Summer 2027)");
  assert.equal(r.company, "Acme Robotics");
  assert.equal(r.location, "Pittsburgh, PA");
  assert.equal(r.url, "https://job-boards.greenhouse.io/acmerobotics/jobs/4567890");
  assert.equal(r.source, "greenhouse");
  assert.equal(r.job_id, "4567890");
  assert.equal(r.posted_date, "2026-09-14");
  assert.match(r.jd_text, /About the role\n/);
  assert.match(r.jd_text, /- Build perception pipelines\n- Ship code to real robots/);
  assert.equal(f.company.source, "json-ld");
  assert.equal(f.title.confidence, "high");
});

test("greenhouse legacy: selectors, 'at Company' stripped", () => {
  const r = v(run("greenhouse_legacy.html", "https://boards.greenhouse.io/contoso/jobs/112233?t=abc"));
  assert.equal(r.title, "Security Engineering Intern");
  assert.equal(r.company, "Contoso");
  assert.equal(r.location, "Remote - US");
  assert.equal(r.job_id, "112233");
  assert.equal(r.url, "https://boards.greenhouse.io/contoso/jobs/112233");
  assert.match(r.jd_text, /- Write detection rules in Sigma/);
});

test("greenhouse embedded on company site keeps gh_jid", () => {
  const url = "https://www.contoso.com/careers/job?gh_jid=778899&utm_campaign=fall";
  assert.equal(X.canonicalUrl(url), "https://www.contoso.com/careers/job?gh_jid=778899");
  assert.equal(X.detectPlatform(url), "greenhouse");
  assert.equal(X.jobIdFromUrl(url, "greenhouse"), "778899");
});

test("lever: company from page title, /apply and lever-source stripped", () => {
  const f = run("lever.html", "https://jobs.lever.co/northwind/0a1b2c3d-1111-4222-8333-944455566677/apply?lever-source=LinkedIn");
  const r = v(f);
  assert.equal(r.title, "Software Engineering Intern, Summer 2027");
  assert.equal(r.company, "Northwind");
  assert.equal(r.location, "San Francisco, CA");
  assert.equal(r.url, "https://jobs.lever.co/northwind/0a1b2c3d-1111-4222-8333-944455566677");
  assert.equal(r.job_id, "0a1b2c3d-1111-4222-8333-944455566677");
  assert.match(r.jd_text, /logistics software/);
  assert.match(r.jd_text, /- Write Go services/);
});

test("workday: automation ids; company from subdomain is medium confidence", () => {
  const f = run("workday.html", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Data-Science-Intern--Summer-2027_R-104233?source=LinkedIn");
  const r = v(f);
  assert.equal(r.title, "Data Science Intern, Summer 2027 (Austin, TX)");
  assert.equal(r.company, "Acme");
  assert.equal(f.company.confidence, "medium");
  assert.equal(r.location, "Austin, TX");
  assert.equal(r.job_id, "R-104233");
  assert.equal(r.posted_date, "5 Days Ago");
  assert.equal(r.url, "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Austin-TX/Data-Science-Intern--Summer-2027_R-104233");
  assert.match(r.jd_text, /forecasting models/);
});

test("handshake", () => {
  const r = v(run("handshake.html", "https://app.joinhandshake.com/jobs/9876543?ref=preview-header-click&searchId=abc"));
  assert.equal(r.title, "Cybersecurity Intern");
  assert.equal(r.company, "Globex Corporation");
  assert.equal(r.location, "Springfield, IL");
  assert.equal(r.job_id, "9876543");
  assert.equal(r.url, "https://app.joinhandshake.com/jobs/9876543");
  assert.match(r.jd_text, /vulnerability management/);
});

test("linkedin: search URL with currentJobId canonicalizes to /jobs/view/<id>/", () => {
  const r = v(run("linkedin.html", "https://www.linkedin.com/jobs/search/?currentJobId=3999888777&keywords=security%20intern&trk=public_jobs"));
  assert.equal(r.title, "Cloud Security Intern");
  assert.equal(r.company, "Initech");
  assert.equal(r.location, "New York, NY");
  assert.equal(r.job_id, "3999888777");
  assert.equal(r.url, "https://www.linkedin.com/jobs/view/3999888777/");
  assert.match(r.jd_text, /Terraform/);
  assert.equal(X.canonicalUrl("https://www.linkedin.com/jobs/view/cloud-security-intern-at-initech-3999888777?trk=x&refId=y"),
    "https://www.linkedin.com/jobs/view/3999888777/");
});

test("icims: company from subdomain, iframe params stripped", () => {
  const f = run("icims.html", "https://careers-globex.icims.com/jobs/12345/software-developer-intern/job?in_iframe=1&mobile=false&width=1200");
  const r = v(f);
  assert.equal(r.title, "Software Developer Intern");
  assert.equal(r.company, "Globex");
  assert.equal(r.location, "US-IL-Chicago");
  assert.equal(r.job_id, "12345");
  assert.equal(r.url, "https://careers-globex.icims.com/jobs/12345/software-developer-intern/job");
  assert.match(r.jd_text, /- Write Java microservices/);
});

test("ashby: JSON-LD inside @graph with entity-escaped description, remote", () => {
  const r = v(run("ashby.html", "https://jobs.ashbyhq.com/hooli/7b1c2d3e-4f50-4a6b-8c9d-0e1f2a3b4c5d/application?utm_source=x"));
  assert.equal(r.title, "Product Security Intern");
  assert.equal(r.company, "Hooli");
  assert.equal(r.location, "Remote; Seattle, Washington");
  assert.equal(r.job_id, "7b1c2d3e-4f50-4a6b-8c9d-0e1f2a3b4c5d");
  assert.equal(r.posted_date, "2026-09-30");
  assert.equal(r.url, "https://jobs.ashbyhq.com/hooli/7b1c2d3e-4f50-4a6b-8c9d-0e1f2a3b4c5d");
  assert.match(r.jd_text, /Hooli's product security team reviews designs & code\./);
  assert.match(r.jd_text, /- Fuzzing internal parsers/);
});

test("smartrecruiters: microdata + spl-job-location", () => {
  const r = v(run("smartrecruiters.html", "https://jobs.smartrecruiters.com/UmbrellaCorp/743999123456789-security-analyst-intern?trid=abc&dcr_ci=x"));
  assert.equal(r.title, "Security Analyst Intern");
  assert.equal(r.company, "Umbrella Corp");
  assert.equal(r.location, "Raleigh, NC, United States");
  assert.equal(r.job_id, "743999123456789");
  assert.equal(r.url, "https://jobs.smartrecruiters.com/UmbrellaCorp/743999123456789-security-analyst-intern");
  assert.match(r.jd_text, /phishing triage/);
});

test("generic fallback: og tags + largest text block, all flagged low", () => {
  const f = run("generic.html", "https://careers.piedpiper.com/jobs/ml-intern?utm_medium=email");
  const r = v(f);
  assert.equal(r.title, "Machine Learning Intern");
  assert.equal(r.company, "Pied Piper");
  assert.equal(f.title.confidence, "low");
  assert.equal(f.company.confidence, "low");
  assert.equal(r.location, "");
  assert.equal(f.location.confidence, "");
  assert.equal(r.source, "other");
  assert.match(r.jd_text, /compression models/);
  assert.doesNotMatch(r.jd_text, /All rights reserved|Follow us/);
  assert.equal(r.url, "https://careers.piedpiper.com/jobs/ml-intern");
});

test("page with no posting: empty fields, nothing invented", () => {
  const f = run("nothing.html", "https://login.example.com/signin");
  assert.equal(f.location.value, "");
  assert.equal(f.jd_text.value, "");
  assert.equal(f.title.confidence, "low");
});

test("platform names are never accepted as the company", () => {
  // LinkedIn page whose company selector misses: og:site_name "LinkedIn" must not be used.
  const html = '<html><head><title>Intern | LinkedIn</title><meta property="og:site_name" content="LinkedIn"></head><body></body></html>';
  const url = "https://www.linkedin.com/jobs/view/123/";
  const f = X.extract(new JSDOM(html, { url }).window.document, url);
  assert.notEqual(f.company.value, "LinkedIn");
});
