"""The JSON Schema that constrains Haiku's plan output.

Structured outputs support a subset of JSON Schema. Notably absent: `pattern`,
`minLength`/`maxLength`, numeric bounds, and recursion. Every object must set
`additionalProperties: false`. Because we cannot express "HH:MM" as a regex
constraint, times are plain strings with a descriptive hint and are normalized
client-side in llm.py.
"""

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "tasks"],
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "One or two sentences addressed to the student, explaining the "
                "shape of the plan and any assumption you had to make."
            ),
        },
        "tasks": {
            "type": "array",
            "description": (
                "The daily tasks, in chronological order. Usually 3-8; scale "
                "with the size of the assignment."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "date", "start_time", "end_time", "detail"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Short imperative name for the sitting, at most 60 "
                            "characters. Example: 'Gather PC Components'."
                        ),
                    },
                    "date": {
                        "type": "string",
                        "format": "date",
                        "description": (
                            "Calendar day as YYYY-MM-DD. MUST fall on or between "
                            "the activity's start date and due date, inclusive, "
                            "and MUST be a day the student said they are free."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": (
                            "Local start time in 24-hour HH:MM form, e.g. '10:00' "
                            "or '14:30'. Must sit inside the student's stated "
                            "available hours."
                        ),
                    },
                    "end_time": {
                        "type": "string",
                        "description": (
                            "Local end time in 24-hour HH:MM form. Must be later "
                            "than start_time on the same day, and the gap must "
                            "not exceed the student's stated focus limit."
                        ),
                    },
                    "detail": {
                        "type": "string",
                        "description": (
                            "One to three sentences describing exactly what to do "
                            "in this sitting, concrete enough to start without "
                            "re-reading the assignment."
                        ),
                    },
                },
            },
        },
    },
}
