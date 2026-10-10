"""Request validation.

Two layers, because they have different consequences:

* Request-level problems (bad JSON, missing event name, empty list, too many
  recipients) make the whole request meaningless -> HTTP 400, nothing stored.
* Recipient-level problems affect one row only -> the row is stored as FAILED
  with a reason and every other recipient is still processed.
"""
import re
from datetime import date

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
MAX_NAME = 100
MAX_ACHIEVEMENT = 150
MAX_EVENT = 150
MAX_TITLE = 80
MAX_ISSUER = 100
# The certificate uses PDF standard fonts (WinAnsi / cp1252). Names outside that
# range would render as blanks, so they are rejected up front instead.
PDF_ENCODING = "cp1252"


class RequestValidationError(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or []


def _clean_str(value):
    if not isinstance(value, str):
        return None
    # collapse internal whitespace, strip control characters
    value = "".join(ch for ch in value if ch.isprintable() or ch == " ")
    return re.sub(r"\s+", " ", value).strip()


def validate_job_payload(payload, max_recipients):
    """Validate the job-level fields. Returns a cleaned dict (recipients untouched)."""
    if not isinstance(payload, dict):
        raise RequestValidationError("Request body must be a JSON object.")

    problems = []

    event = _clean_str(payload.get("event_name"))
    if not event:
        problems.append({"field": "event_name", "message": "event_name is required."})
    elif len(event) > MAX_EVENT:
        problems.append({"field": "event_name", "message": f"Must be at most {MAX_EVENT} characters."})

    title = payload.get("title", "Certificate of Completion")
    title = _clean_str(title)
    if not title or len(title) > MAX_TITLE:
        problems.append({"field": "title", "message": f"title must be 1-{MAX_TITLE} characters."})

    issuer = payload.get("issuer", "Organizing Committee")
    issuer = _clean_str(issuer)
    if not issuer or len(issuer) > MAX_ISSUER:
        problems.append({"field": "issuer", "message": f"issuer must be 1-{MAX_ISSUER} characters."})

    raw_date = payload.get("issue_date")
    if raw_date is None:
        issue_date = date.today().isoformat()
    else:
        try:
            issue_date = date.fromisoformat(raw_date).isoformat()
        except (TypeError, ValueError):
            problems.append({"field": "issue_date", "message": "issue_date must be an ISO date (YYYY-MM-DD)."})
            issue_date = None

    for field, text in (("event_name", event), ("title", title), ("issuer", issuer)):
        if text and not _encodable(text):
            problems.append({"field": field, "message": "Contains characters the certificate font cannot render."})

    recipients = payload.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        problems.append({"field": "recipients", "message": "recipients must be a non-empty list."})
    elif len(recipients) > max_recipients:
        problems.append({
            "field": "recipients",
            "message": f"At most {max_recipients} recipients per request; split the list into several jobs.",
        })

    if problems:
        raise RequestValidationError("Invalid request.", problems)

    return {
        "event_name": event,
        "title": title,
        "issuer": issuer,
        "issue_date": issue_date,
        "recipients": recipients,
    }


def _encodable(text):
    try:
        text.encode(PDF_ENCODING)
        return True
    except UnicodeEncodeError:
        return False


def validate_recipient(raw, seen_emails):
    """Validate one recipient.

    Returns (clean, error). `clean` always has name/email/achievement keys
    (possibly None) so the row can be stored and reported even when invalid.
    """
    if not isinstance(raw, dict):
        return {"name": None, "email": None, "achievement": None}, "Recipient must be a JSON object."

    name = _clean_str(raw.get("name"))
    email = _clean_str(raw.get("email"))
    email = email.lower() if email else email
    achievement = raw.get("achievement")
    achievement_type_error = achievement is not None and not isinstance(achievement, str)
    achievement = _clean_str(achievement) if achievement is not None else None

    clean = {"name": name, "email": email, "achievement": achievement or None}
    errors = []

    if not name:
        errors.append("name is required")
    elif len(name) > MAX_NAME:
        errors.append(f"name must be at most {MAX_NAME} characters")
    elif not _encodable(name):
        errors.append("name contains characters the certificate font cannot render")

    if not email:
        errors.append("email is required")
    elif len(email) > 254 or not EMAIL_RE.match(email):
        errors.append("email is not a valid address")

    if achievement_type_error:
        errors.append("achievement must be a string")
    elif achievement is not None:
        if len(achievement) > MAX_ACHIEVEMENT:
            errors.append(f"achievement must be at most {MAX_ACHIEVEMENT} characters")
        elif not _encodable(achievement):
            errors.append("achievement contains characters the certificate font cannot render")

    if not errors and email in seen_emails:
        errors.append("duplicate email in this job")

    if errors:
        return clean, "; ".join(errors)

    seen_emails.add(email)
    return clean, None
