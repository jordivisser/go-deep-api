"""
deep_research.py — Project intelligence research engine.

Given a project name (+ optional owner/location), searches the public web
for RFQ/RFP documents, procurement platforms, attendee lists, key dates,
contacts, and news. Extracts structured data and identifies gaps.

Uses Brave Search API (same as competitive_intel_finder.py) + Claude for
extraction. Matches discovered organizations against Zoho Accounts.
"""

import os
import re
import json
import requests
from dataclasses import dataclass, field
from go_deep_ranker import get_zoho_accounts, normalize_org, fuzzy_match


# ============================================================
# Config
# ============================================================

MAX_QUERIES = 15
MAX_RESULTS_PER_QUERY = 5
BRAVE_TIMEOUT = 10
CLAUDE_TIMEOUT = 90


# ============================================================
# Data model
# ============================================================

@dataclass
class ResearchFinding:
    category: str = ""        # rfq, rfp, platform, attendee_list, addendum, news, etc.
    title: str = ""
    url: str = ""
    snippet: str = ""
    source: str = ""
    confidence: str = "low"   # high, medium, low
    extracted_data: dict = field(default_factory=dict)


@dataclass
class ResearchResult:
    project_name: str = ""
    owner: str = ""
    location: str = ""
    findings: list = field(default_factory=list)
    procurement_platform: dict = field(default_factory=dict)
    procurement_contact: dict = field(default_factory=dict)
    key_dates: list = field(default_factory=list)
    organizations_found: list = field(default_factory=list)
    news: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    next_actions: list = field(default_factory=list)


# ============================================================
# Brave Search
# ============================================================

def brave_search(query: str, api_key: str) -> list[dict]:
    """Run a single Brave search query. Returns list of result dicts."""
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            params={"q": query, "count": MAX_RESULTS_PER_QUERY},
            timeout=BRAVE_TIMEOUT,
        )
        if r.status_code != 200:
            print(f"Brave search failed ({r.status_code}): {query}")
            return []
        results = r.json().get("web", {}).get("results", [])
        return [{"title": r.get("title", ""), "url": r.get("url", ""),
                 "description": r.get("description", "")} for r in results]
    except Exception as e:
        print(f"Brave search error: {e}")
        return []


def generate_queries(project_name: str, owner: str = "", location: str = "") -> list[tuple[str, str]]:
    """Generate targeted search queries. Returns [(query, category)]."""
    pn = project_name
    queries = []

    # RFQ / RFP / Solicitation
    queries.append((f'"{pn}" RFQ', "rfq"))
    queries.append((f'"{pn}" RFP', "rfp"))
    queries.append((f'"{pn}" solicitation', "rfq"))

    # Procurement platform detection
    queries.append((f'"{pn}" PlanetBids OR Bonfire OR DemandStar OR PublicPurchase', "platform"))
    queries.append((f'"{pn}" procurement portal bid', "platform"))

    # Pre-proposal / industry forum / attendee list
    queries.append((f'"{pn}" pre-proposal conference OR industry forum', "attendee_list"))
    queries.append((f'"{pn}" attendee list OR sign-in sheet OR registrant', "attendee_list"))

    # Addenda / Q&A
    queries.append((f'"{pn}" addendum OR addenda', "addendum"))

    # Shortlist / Award
    queries.append((f'"{pn}" shortlist OR "short list" OR award', "shortlist"))

    # Project website / news
    queries.append((f'"{pn}" project construction', "news"))
    queries.append((f'"{pn}" construction project announcement', "news"))

    # Owner-specific searches
    if owner:
        queries.append((f'"{owner}" "{pn}" RFQ OR RFP', "rfq"))
        queries.append((f'site:{_domain_guess(owner)} "{pn}"', "rfq"))

    # Location-specific
    if location:
        queries.append((f'"{pn}" {location} construction project', "news"))

    return queries[:MAX_QUERIES]


def _domain_guess(owner: str) -> str:
    """Guess a domain from an owner name. 'Sites Project Authority' -> 'sitesproject.org'."""
    words = re.findall(r'[a-zA-Z]+', owner.lower())
    # Remove common suffixes
    skip = {"authority", "agency", "department", "office", "commission", "board",
            "county", "city", "state", "of", "the", "and", "for"}
    words = [w for w in words if w not in skip]
    if words:
        return "".join(words[:2]) + ".org"
    return ""


# ============================================================
# Result classification and scoring
# ============================================================

PLATFORM_KEYWORDS = {
    "planetbids": "PlanetBids",
    "bonfirehub": "Bonfire",
    "demandstar": "DemandStar",
    "publicpurchase": "PublicPurchase",
    "bidnet": "BidNet",
    "govwin": "GovWin",
    "bidexpress": "BidExpress",
    "questcdn": "QuestCDN",
    "ebidexchange": "eBidExchange",
    "negometrix": "Negometrix",
    "jaggaer": "Jaggaer",
    "procurement.org": "Custom Portal",
}

DOCUMENT_KEYWORDS = {
    "rfq": ["request for qualifications", "rfq", "statement of qualifications", "soq"],
    "rfp": ["request for proposal", "rfp", "request for proposals"],
    "attendee_list": ["attendee", "sign-in", "signin", "sign in", "registrant", "registration list"],
    "addendum": ["addendum", "addenda", "amendment"],
    "shortlist": ["shortlist", "short list", "short-list", "selected firms", "award"],
    "platform": ["procurement", "bid opportunity", "solicitation", "vendor registration"],
    "news": ["announced", "approved", "funded", "groundbreaking", "construction"],
}


def classify_result(result: dict, query_category: str) -> ResearchFinding:
    """Classify a search result into a finding category."""
    url = result.get("url", "").lower()
    title = result.get("title", "").lower()
    desc = result.get("description", "").lower()
    combined = f"{title} {desc} {url}"

    # Detect procurement platform from URL
    platform = None
    for keyword, name in PLATFORM_KEYWORDS.items():
        if keyword in url:
            platform = name
            break

    # Score relevance
    confidence = "low"
    category = query_category

    # Check for document type indicators
    for cat, keywords in DOCUMENT_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                if cat in ("rfq", "rfp", "attendee_list", "addendum", "shortlist"):
                    confidence = "high" if kw in title else "medium"
                    category = cat
                    break

    # PDF/document indicators boost confidence
    if url.endswith(".pdf") or "download" in url:
        confidence = "high"

    # Platform detection is always high confidence
    if platform:
        category = "platform"
        confidence = "high"

    return ResearchFinding(
        category=category,
        title=result.get("title", ""),
        url=result.get("url", ""),
        snippet=result.get("description", ""),
        source=platform or _extract_domain(result.get("url", "")),
        confidence=confidence,
        extracted_data={"platform": platform} if platform else {},
    )


def _extract_domain(url: str) -> str:
    """Extract domain from URL."""
    match = re.search(r'https?://(?:www\.)?([^/]+)', url)
    return match.group(1) if match else ""


# ============================================================
# Claude analysis of findings
# ============================================================

ANALYSIS_PROMPT = """You are a construction procurement analyst. Given these search results about a construction project, extract structured intelligence.

Project: {project_name}
Owner: {owner}
Location: {location}

Search Results:
{findings_text}

Return ONLY valid JSON, no markdown fences:
{{
  "project_identity": {{
    "confirmed_name": "string",
    "alternate_names": ["string"],
    "owner": "string",
    "location": "string",
    "estimated_value": "string or null",
    "delivery_method": "string or null",
    "current_stage": "string or null"
  }},
  "procurement_platform": {{
    "name": "PlanetBids, Bonfire, etc. or null",
    "url": "string or null",
    "solicitation_number": "string or null",
    "registration_required": true/false
  }},
  "procurement_contact": {{
    "name": "string or null",
    "email": "string or null",
    "phone": "string or null"
  }},
  "key_dates": [
    {{"event": "string", "date": "string"}}
  ],
  "organizations_mentioned": [
    {{"name": "string", "role": "prime, designer, owner, sub, consultant, or unknown"}}
  ],
  "documents_found": [
    {{"type": "RFQ, RFP, Addendum, etc.", "url": "string", "title": "string"}}
  ],
  "missing_information": [
    "string description of what was NOT found"
  ],
  "recommended_next_actions": [
    "string - specific actionable step"
  ],
  "news_summary": "1-2 sentence summary of any news coverage"
}}

Be specific. If information is not in the results, list it as missing. Recommend concrete next actions (e.g., 'Register on PlanetBids solicitation #X', 'Email procurement@agency.gov requesting attendee list')."""


def analyze_findings(
    project_name: str,
    owner: str,
    location: str,
    findings: list[ResearchFinding],
) -> dict | None:
    """Use Claude to analyze and synthesize findings."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set -- skipping Claude analysis")
        return None

    # Build findings text
    findings_text = ""
    for i, f in enumerate(findings[:30]):
        findings_text += f"\n[{i+1}] Category: {f.category} | Confidence: {f.confidence}\n"
        findings_text += f"    Title: {f.title}\n"
        findings_text += f"    URL: {f.url}\n"
        findings_text += f"    Snippet: {f.snippet}\n"
        if f.extracted_data:
            findings_text += f"    Extracted: {f.extracted_data}\n"

    prompt = ANALYSIS_PROMPT.format(
        project_name=project_name,
        owner=owner or "Unknown",
        location=location or "Unknown",
        findings_text=findings_text,
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 3000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=CLAUDE_TIMEOUT,
        )

        if r.status_code != 200:
            print(f"Claude API error: {r.status_code}")
            return None

        text = ""
        for block in r.json().get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"Claude JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"Claude analysis error: {e}")
        return None


# ============================================================
# Zoho matching on discovered organizations
# ============================================================

def match_orgs_to_zoho(orgs: list[dict], accounts: dict[str, str]) -> list[dict]:
    """Add Zoho relationship status to discovered organizations."""
    for org in orgs:
        name = org.get("name", "")
        best_score = 0
        matched = ""
        for account in accounts:
            s = fuzzy_match(name, account)
            if s > best_score:
                best_score = s
                matched = account
        if best_score >= 85:
            org["zoho_status"] = accounts[matched]
            org["zoho_match"] = matched
        else:
            org["zoho_status"] = "Cold"
            org["zoho_match"] = ""
    return orgs


# ============================================================
# Main research pipeline
# ============================================================

def deep_research(
    project_name: str,
    owner: str = "",
    location: str = "",
) -> dict:
    """Run the full deep research pipeline. Returns structured JSON."""

    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not brave_key:
        return {"error": "BRAVE_SEARCH_API_KEY not set"}

    print(f"\n=== Deep Research: {project_name} ===")

    # Step 1: Generate queries
    queries = generate_queries(project_name, owner, location)
    print(f"Generated {len(queries)} search queries")

    # Step 2: Run Brave searches
    all_findings: list[ResearchFinding] = []
    seen_urls = set()

    for query, category in queries:
        results = brave_search(query, brave_key)
        for result in results:
            url = result.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)
            finding = classify_result(result, category)
            all_findings.append(finding)

    print(f"Found {len(all_findings)} unique results")

    # Step 3: Claude analysis
    analysis = analyze_findings(project_name, owner, location, all_findings)

    # Step 4: Zoho matching
    zoho_accounts = get_zoho_accounts()
    if analysis and analysis.get("organizations_mentioned") and zoho_accounts:
        analysis["organizations_mentioned"] = match_orgs_to_zoho(
            analysis["organizations_mentioned"], zoho_accounts
        )

    # Step 5: Build response
    response = {
        "project_name": project_name,
        "owner": owner,
        "location": location,
        "query_count": len(queries),
        "result_count": len(all_findings),
    }

    if analysis:
        response["project_identity"] = analysis.get("project_identity", {})
        response["procurement_platform"] = analysis.get("procurement_platform", {})
        response["procurement_contact"] = analysis.get("procurement_contact", {})
        response["key_dates"] = analysis.get("key_dates", [])
        response["organizations"] = analysis.get("organizations_mentioned", [])
        response["documents_found"] = analysis.get("documents_found", [])
        response["missing"] = analysis.get("missing_information", [])
        response["next_actions"] = analysis.get("recommended_next_actions", [])
        response["news_summary"] = analysis.get("news_summary", "")
    else:
        # Fallback: return raw findings grouped by category
        response["findings"] = []
        for f in all_findings:
            response.setdefault("findings", []).append({
                "category": f.category,
                "title": f.title,
                "url": f.url,
                "snippet": f.snippet,
                "confidence": f.confidence,
            })
        response["missing"] = ["Claude analysis unavailable -- showing raw search results"]
        response["next_actions"] = ["Review the search results manually"]

    # Always include raw source links
    response["sources"] = [
        {"category": f.category, "title": f.title, "url": f.url, "confidence": f.confidence}
        for f in all_findings if f.confidence in ("high", "medium")
    ]

    print(f"Deep Research complete: {len(response.get('sources', []))} high/medium confidence sources")
    return response