"""Deterministic job fit scoring for job_monitor."""

import re


DEFAULT_SCORE_THRESHOLD = 45
STRICT_SCORE_THRESHOLD = 60

DEFAULT_MAX_YEARS_EXPERIENCE = 3

JUNIOR_PATTERNS = [
    r"\b0\s*-\s*[12]\s+years?\b",
    r"\b1\s*-\s*[23]\s+years?\b",
    r"\b1\+?\s+years?\b",
    r"\bno experience necessary\b",
    r"\brecent graduate\b",
    r"\bentry[- ]level\b",
    r"\bnew grad(?:uate)?\b",
    r"\bearly career\b",
    r"\bjunior\b",
]

SENIOR_PATTERNS = [
    r"\bextensive experience\b",
]

# "3+ years", "3-5 years", "minimum of 4 years", "5 plus years" ... tied to
# experience. The lower bound of a range is what the posting requires.
YEARS_PATTERN = re.compile(
    r"(?<![\d.$])(?P<low>\d{1,2})\s*(?:\+|plus)?\s*"
    r"(?:(?:-|\u2013|\u2014|to)\s*(?P<high>\d{1,2})\s*\+?\s*)?"
    r"years?(?:'s)?\b(?P<after>[^.;\n]{0,80})",
    re.IGNORECASE,
)
EXPERIENCE_WORDS = re.compile(
    r"\bexperience|\bexperienced\b|\bbackground\b|\bworking\b|\bsimilar role\b|\bin the field\b",
    re.IGNORECASE,
)
SUBSTITUTION_WORDS = re.compile(r"\bin lieu\b|\bequivalent\b|\binstead of\b", re.IGNORECASE)

SALARY_CONTEXT_PATTERN = re.compile(
    r"\b(salary|compensation|pay|base pay|base salary|annually|annualized|per year)\b|/year|/yr",
    re.I,
)
SALARY_NUMBER_PATTERN = re.compile(
    r"(?P<dollar>\$)?\s*(?P<amount>[0-9]{2,3}(?:,[0-9]{3})?|[0-9]{2,3})\s*(?P<suffix>k)?",
    re.I,
)


def normalize_category(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def contains_phrase(text, phrase):
    phrase = str(phrase).strip()
    if not phrase:
        return False
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE))


def regex_match_any(text, patterns):
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def category_terms(keyword_categories, profile):
    terms_by_category = {}
    if isinstance(keyword_categories, dict):
        for label, terms in keyword_categories.items():
            key = normalize_category(label)
            terms_by_category.setdefault(key, {"label": str(label), "terms": []})
            if isinstance(terms, dict):
                for values in terms.values():
                    terms_by_category[key]["terms"].extend(values or [])
            else:
                terms_by_category[key]["terms"].extend(terms or [])
    else:
        terms_by_category["general"] = {
            "label": "General",
            "terms": list(keyword_categories or []),
        }

    for key, values in (profile.get("skills") or {}).items():
        normalized = normalize_category(key)
        label = terms_by_category.get(normalized, {}).get("label", str(key).replace("_", " ").title())
        terms_by_category.setdefault(normalized, {"label": label, "terms": []})
        terms_by_category[normalized]["terms"].extend(values or [])
    return terms_by_category


def skill_match_score(text, keyword_categories, profile):
    categories = category_terms(keyword_categories, profile)
    if not categories:
        return 0, [], []

    matched = []
    failed = []
    for item in categories.values():
        unique_terms = [term for term in dict.fromkeys(item["terms"]) if str(term).strip()]
        if any(contains_phrase(text, term) for term in unique_terms):
            matched.append(item["label"])
        else:
            failed.append(item["label"])

    score = round((len(matched) / len(categories)) * 55) if categories else 0
    return score, matched, failed


def has_title_or_context_category_match(title, context, keyword_categories):
    if not keyword_categories:
        return True

    title_text = str(title or "")
    context_text = str(context or "")
    combined_text = " | ".join(
        value.strip() for value in (title_text, context_text) if value.strip()
    )
    if isinstance(keyword_categories, dict):
        for terms in keyword_categories.values():
            if isinstance(terms, dict):
                title_terms = terms.get("title", [])
                context_terms = terms.get("context", [])
                if any(contains_phrase(title_text, term) for term in title_terms):
                    return True
                if any(contains_phrase(context_text, term) for term in context_terms):
                    return True
            elif any(contains_phrase(combined_text, term) for term in terms or []):
                return True
        return False

    return any(contains_phrase(combined_text, term) for term in keyword_categories or [])


def required_years(text):
    """Return the most years of experience the text asks for, or None.

    Only numbers tied to experience count ("3+ years of experience"), and a
    degree substitution ("4 years of experience in lieu of a degree") is
    ignored. For a range, the lower bound is the requirement.
    """
    text = str(text or "")
    found = []
    for match in YEARS_PATTERN.finditer(text):
        after = match.group("after")
        before = text[max(0, match.start() - 40) : match.start()]
        if not EXPERIENCE_WORDS.search(after) and not re.search(r"experience\W*$", before, re.I):
            continue
        if SUBSTITUTION_WORDS.search(after) or SUBSTITUTION_WORDS.search(before):
            continue
        low = int(match.group("low"))
        if low <= 20:
            found.append(low)
    return max(found) if found else None


def title_or_context_categories(title, context, keyword_categories):
    """Category labels whose role terms appear in the title or team/department."""
    if not keyword_categories:
        return ["General"]
    title_text = str(title or "")
    context_text = str(context or "")
    combined_text = " | ".join(v.strip() for v in (title_text, context_text) if v.strip())
    if not isinstance(keyword_categories, dict):
        if any(contains_phrase(combined_text, term) for term in keyword_categories):
            return ["General"]
        return []
    labels = []
    for label, terms in keyword_categories.items():
        if isinstance(terms, dict):
            hit = any(contains_phrase(title_text, term) for term in terms.get("title", [])) or any(
                contains_phrase(context_text, term) for term in terms.get("context", [])
            )
        else:
            hit = any(contains_phrase(combined_text, term) for term in terms or [])
        if hit:
            labels.append(str(label))
    return labels


def parse_salary_low(text):
    text = str(text or "")
    values = []
    for match in SALARY_NUMBER_PATTERN.finditer(text):
        raw = match.group("amount")
        suffix = match.group("suffix")
        has_dollar = bool(match.group("dollar"))
        context_start = max(0, match.start() - 50)
        context_end = min(len(text), match.end() + 50)
        context = text[context_start:context_end]
        if not has_dollar and not SALARY_CONTEXT_PATTERN.search(context):
            continue
        if match.start() > 0 and text[match.start() - 1].isdigit():
            continue
        if match.end() < len(text) and text[match.end() : match.end() + 1].isalpha():
            continue
        value = int(raw.replace(",", ""))
        if suffix or value < 1000:
            value *= 1000
        if value >= 20000:
            values.append(value)
    return min(values) if values else None


def _veto(reason, summary):
    return {
        "score": 0,
        "matched": [],
        "failed": [reason],
        "veto": True,
        "categories": [],
        "strong_category_match": False,
        "summary": summary,
    }


def score_job(
    title,
    location,
    context,
    description,
    source,
    profile,
    location_matches=True,
):
    """Score a posting 0-100 and record which gates it passed or failed.

    Gates (any one vetoes): a senior title, a non-role title, a profile
    deal-breaker phrase, or a stated experience requirement at or above
    ``preferences.max_years_experience`` (default 3).

    Score, used for the threshold and for ordering:
      role    40 title match, 30 team/department match only, 0 otherwise
      depth   0-15, 3 per distinct skill term found in the description
      level   25 early-career, 15 not stated, 10 two years asked, 0 "extensive"
    Location is a separate requirement and is not scored.
    """
    text = " | ".join(str(value or "") for value in (title, context, description))
    matched = []
    failed = []
    preferences = profile.get("preferences") or {}

    for keyword in source.get("exclude_keywords", []):
        if contains_phrase(title, keyword):
            return _veto(f"title seniority: {keyword}", "veto: title seniority")

    for keyword in source.get("non_role_title_keywords", []):
        if contains_phrase(title, keyword):
            return _veto(f"not a specific role: {keyword}", "veto: not a specific role")

    for phrase in preferences.get("deal_breaker_phrases", []):
        if contains_phrase(description, phrase):
            return _veto(f"deal-breaker: {phrase}", "veto: deal-breaker")

    max_years = int(preferences.get("max_years_experience", DEFAULT_MAX_YEARS_EXPERIENCE))
    years = required_years(description)
    if years is not None and years >= max_years:
        return _veto(f"experience: {years}+ years asked", "veto: years of experience")

    keyword_categories = source.get("keyword_categories", source.get("keywords", []))
    role_categories = title_or_context_categories(title, context, keyword_categories)
    title_categories = title_or_context_categories(title, "", keyword_categories)
    strong_category_match = bool(role_categories)
    if title_categories:
        role_score = 40
    elif role_categories:
        role_score = 30
    else:
        role_score = 0
        failed.append("no role category in title or team")

    categories_info = category_terms(keyword_categories, profile)
    _, body_categories, _ = skill_match_score(description, keyword_categories, profile)
    skill_terms = {
        str(term).casefold()
        for item in categories_info.values()
        for term in item["terms"]
        if str(term).strip()
    }
    depth_hits = sorted(term for term in skill_terms if contains_phrase(description, term))
    depth_score = min(15, 3 * len(depth_hits))
    categories = list(dict.fromkeys(role_categories + body_categories))
    if role_categories:
        matched.extend(f"role: {label}" for label in role_categories)
    if depth_hits:
        matched.append(f"skills in description: {len(depth_hits)}")

    if regex_match_any(description, JUNIOR_PATTERNS) or any(
        contains_phrase(title, phrase) for phrase in source.get("early_career_keywords", [])
    ):
        seniority_score = 25
        matched.append("early-career")
    elif years == 2:
        seniority_score = 10
        failed.append("2 years asked")
    elif regex_match_any(description, SENIOR_PATTERNS):
        seniority_score = 0
        failed.append("extensive experience asked")
    else:
        seniority_score = 15
        matched.append("ambiguous seniority")

    if location_matches:
        matched.append("location")
    else:
        failed.append("location")

    salary_low = parse_salary_low(text)
    min_salary = preferences.get("min_salary")
    if salary_low and min_salary and salary_low < int(min_salary):
        failed.append(f"compensation below floor: ${salary_low:,}")
    elif salary_low and min_salary:
        matched.append(f"compensation >= ${int(min_salary):,}")

    score = min(100, role_score + depth_score + seniority_score)
    summary_bits = categories[:2] + [item for item in ("early-career", "location") if item in matched]
    return {
        "score": score,
        "matched": matched,
        "failed": failed,
        "veto": False,
        "categories": categories,
        "strong_category_match": strong_category_match,
        "summary": ", ".join(summary_bits) or "no strong match",
    }


def score_threshold(profile):
    early_career = (profile.get("search") or {}).get("early_career", True)
    return DEFAULT_SCORE_THRESHOLD if early_career else STRICT_SCORE_THRESHOLD
