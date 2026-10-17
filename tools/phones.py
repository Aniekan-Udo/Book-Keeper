import phonenumbers

INVALID = "Enter a valid Nigerian phone number, e.g. 0803 123 4567"


def normalize_nigerian_phone(raw: str) -> str:
    """Accepts 0803..., 234803..., +234 803 ... and returns E.164 (+234803...).

    Validated against libphonenumber's real Nigerian number ranges, not just a digit count.
    """
    text = raw.strip()
    if text[:3] == "234":  # international form written without the +
        text = "+" + text
    try:
        number = phonenumbers.parse(text, "NG")
    except phonenumbers.NumberParseException:
        raise ValueError(INVALID) from None
    if number.country_code != 234 or not phonenumbers.is_valid_number(number):
        raise ValueError(INVALID)
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def display_phone(e164: str) -> str:
    """+2348031234567 → 0803 123 4567, the way Nigerians write numbers."""
    local = "0" + e164.removeprefix("+234")
    return f"{local[:4]} {local[4:7]} {local[7:]}"
