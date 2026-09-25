import re


def normalize_nigerian_phone(raw: str) -> str:
    """Accepts 0803..., 234803..., +234 803 ... and returns E.164 (+234803...)."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("234"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Enter a valid Nigerian phone number, e.g. 0803 123 4567")
    return "+234" + digits


def display_phone(e164: str) -> str:
    """+2348031234567 → 0803 123 4567, the way Nigerians write numbers."""
    local = "0" + e164.removeprefix("+234")
    return f"{local[:4]} {local[4:7]} {local[7:]}"
