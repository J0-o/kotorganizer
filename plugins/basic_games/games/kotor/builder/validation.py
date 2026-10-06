import re



def versions_equal(left: str, right: str) -> bool:
    left_parts = version_parts(left)
    right_parts = version_parts(right)
    if not left_parts or not right_parts:
        return left.strip() == right.strip()
    max_len = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (max_len - len(left_parts)))
    right_parts.extend([0] * (max_len - len(right_parts)))
    return left_parts == right_parts



def version_parts(value: str) -> list[int]:
    text = str(value).strip().lstrip("vV").strip()
    if not text:
        return []
    parts: list[int] = []
    for chunk in text.split("."):
        match = re.match(r"^(\d+)", chunk)
        if not match:
            return []
        parts.append(int(match.group(1)))
    return parts
