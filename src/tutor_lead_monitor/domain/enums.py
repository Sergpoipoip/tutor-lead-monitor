from enum import StrEnum


class Intent(StrEnum):
    SEEKING_TUTOR = "seeking_tutor"
    OFFERING_TUTORING = "offering_tutoring"
    SCHOOL_OR_AGENCY_AD = "school_or_agency_ad"
    TEACHING_JOB = "teaching_job"
    INFORMATIONAL = "informational"
    UNCERTAIN = "uncertain"


class Subject(StrEnum):
    LITERATURE = "literature"
    RUSSIAN_AND_LITERATURE = "russian_and_literature"
    RUSSIAN_LANGUAGE = "russian_language"
    OTHER = "other"
    UNKNOWN = "unknown"


class LessonFormat(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    EITHER = "either"
    UNKNOWN = "unknown"


class Urgency(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    UNKNOWN = "unknown"


class Goal(StrEnum):
    SCHOOL_SUPPORT = "school_support"
    ESSAY = "essay"
    OGE = "oge"
    EGE = "ege"
    OLYMPIAD = "olympiad"
    VSOSH = "vsosh"
    ADMISSIONS = "admissions"
    ADULT_STUDY = "adult_study"
    OTHER = "other"


class LeadStatus(StrEnum):
    NEW = "new"
    NOTIFIED = "notified"
    INTERESTED = "interested"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    CLOSED = "closed"
    EXPIRED = "expired"
