import re

from tutor_lead_monitor.config import ProcessingConfig
from tutor_lead_monitor.domain.enums import Intent, Subject
from tutor_lead_monitor.processing.models import Classification, Evidence


def classify(text: str, config: ProcessingConfig) -> Classification:
    matches = [
        (rule, match)
        for rule in config.rules
        for match in re.finditer(rule.pattern, text, re.IGNORECASE)
    ]
    intent_rules = sorted(
        (rule for rule, _ in matches if rule.intent is not None),
        key=lambda rule: (-rule.priority, rule.id),
    )
    subjects = {rule.subject for rule, _ in matches if rule.subject is not None}
    if Subject.LITERATURE in subjects and Subject.RUSSIAN_LANGUAGE in subjects:
        subject = Subject.RUSSIAN_AND_LITERATURE
    else:
        subject = next(
            (
                s
                for s in (
                    Subject.RUSSIAN_AND_LITERATURE,
                    Subject.LITERATURE,
                    Subject.RUSSIAN_LANGUAGE,
                    Subject.OTHER,
                )
                if s in subjects
            ),
            Subject.UNKNOWN,
        )
    return Classification(
        intent=(intent_rules[0].intent or Intent.UNCERTAIN) if intent_rules else Intent.UNCERTAIN,
        subject=subject,
        confidence=intent_rules[0].confidence if intent_rules else 0.0,
        matched_rule_ids=tuple(dict.fromkeys(rule.id for rule, _ in matches)),
        evidence=tuple(Evidence(rule.id, match.start(), match.end()) for rule, match in matches),
        version=config.version,
    )
