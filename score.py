"""Deterministic job fit scoring for job_monitor."""

import re


DEFAULT_SCORE_THRESHOLD = 45
STRICT_SCORE_THRESHOLD = 60

JUNIOR_PATTERNS = [
    r"\b0\s*-\s*2\s+years?\b",
    r"\b0\s*-\s*1\s+years?\b",
    r"\bno experience necessary\b",
    r"\brecent graduate\b",
    r"\bentry[- ]level\b",
    r"\bnew grad(?:uate)?\b",
    r"\bearly career\b",
    r"\bjunior\b",
]

SENIOR_PATTERNS = [
    r"\b5\+?\s+years?\b",
    r"\b7\+?\s+years?\b",
    r"\b10\+?\s+years?\b",
    r"\bextensive experience\b",
]


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


def parse_salary_low(text):
    matches = re.findall(r"\$?\s*([0-9]{2,3}(?:,[0-9]{3})?|[0-9]{2,3})\s*(k)?", text, re.I)
    values = []
    for raw, suffix in matches:
        value = int(raw.replace(",", ""))
        if suffix or value < 1000:
            value *= 1000
        if value >= 20000:
            values.append(value)
    return min(values) if values else None


def score_job(
    title,
    location,
    context,
    description,
    source,
    profile,
    location_matches=True,
):
    text = " | ".join(str(value or "") for value in (title, context, description))
    matched = []
    failed = []

    exclude_keywords = source.get("exclude_keywords", [])
    for keyword in exclude_keywords:
        if contains_phrase(title, keyword):
            return {
                "score": 0,
                "matched": [],
                "failed": [f"title seniority: {keyword}"],
                "veto": True,
                "categories": [],
                "summary": "veto: title seniority",
            }

    deal_breakers = (profile.get("preferences") or {}).get("deal_breaker_phrases", [])
    for phrase in deal_breakers:
        if contains_phrase(description, phrase):
            return {
                "score": 0,
                "matched": [],
                "failed": [f"deal-breaker: {phrase}"],
                "veto": True,
                "categories": [],
                "summary": "veto: deal-breaker",
            }

    skill_score, categories, missing_categories = skill_match_score(
        text, source.get("keyword_categories", source.get("keywords", [])), profile
    )
    if categories:
        matched.extend([f"skills: {category}" for category in categories])
    else:
        failed.append("skills")

    seniority_score = 15
    if regex_match_any(description, JUNIOR_PATTERNS) or any(
        contains_phrase(title, phrase) for phrase in source.get("early_career_keywords", [])
    ):
        seniority_score = 25
        matched.append("early-career")
    elif regex_match_any(description, SENIOR_PATTERNS):
        seniority_score = 0
        failed.append("senior experience")
    else:
        matched.append("ambiguous seniority")

    location_score = 20 if location_matches else 0
    if location_matches:
        matched.append("location")
    else:
        failed.append("location")

    salary_low = parse_salary_low(text)
    min_salary = (profile.get("preferences") or {}).get("min_salary")
    if salary_low and min_salary and salary_low < int(min_salary):
        failed.append(f"compensation below floor: ${salary_low:,}")
    elif salary_low and min_salary:
        matched.append(f"compensation >= ${int(min_salary):,}")

    score = min(100, skill_score + seniority_score + location_score)
    summary_bits = categories[:2] + [item for item in ("early-career", "location") if item in matched]
    return {
        "score": score,
        "matched": matched,
        "failed": failed + [f"missing {category}" for category in missing_categories if not categories],
        "veto": False,
        "categories": categories,
        "summary": ", ".join(summary_bits) or "no strong match",
    }


def score_threshold(profile):
    early_career = (profile.get("search") or {}).get("early_career", True)
    return DEFAULT_SCORE_THRESHOLD if early_career else STRICT_SCORE_THRESHOLD
