"""Lightweight acceptance rules shared by service and read-only reporting."""

# An open suspected-wrong-answer issue may explain an intentionally blank answer.
# It remains a review issue; declaration never means the answer is correct.
ANSWER_ISSUE_TYPES = frozenset(
    {"missing_answer", "unknown_answer", "suspected_wrong_answer"}
)
SOLUTION_ISSUE_TYPES = frozenset({"missing_solution"})
# Explanations are optional for every question type, including review/export.
# Keep the shared policy for service validation and repair-queue consumers.
SOLUTION_REQUIRED_TYPES = frozenset()


def student_visible_image_keys(content) -> set[str]:
    """Effective disclosure shared by proposal checks and assignment export.

    Referenced teacher-only stem bindings remain private. Unreferenced stem
    cards follow the question pane, while solution/evidence cards stay private.
    Import models only when needed so read-only reporting stays lightweight.
    """
    from question_bank.models import marker_keys

    stem_keys = set(marker_keys(content.stem))
    referenced = (
        stem_keys
        | set(marker_keys(content.answer))
        | set(marker_keys(content.solution))
    )
    return {
        image.key
        for image in content.images
        if image.role == "stem"
        and (image.key in stem_keys or image.key not in referenced)
    }
