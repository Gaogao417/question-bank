"""Route explicit image issues to manual capture; other issues remain AI work."""

import re


def is_image_issue(issue):
    value = issue if isinstance(issue, dict) else issue.model_dump()
    kind, target = value.get("type", "").lower(), value.get("target", "").lower()
    return bool(
        re.search(r"image|figure|diagram|crop|asset|media", kind)
        or re.match(r"^(?:images?|evidence|assets?)(?:$|[.\[])", target)
        or re.search(r"缺图|题图|插图|图片|图像|截图|裁图", value.get("reason", ""))
    )


def ai_repair_needed(detail):
    return any(
        issue["state"] == "open" and not is_image_issue(issue)
        for issue in detail.get("issues", [])
    ) or bool(detail.get("check") and not detail["check"]["passed"])
