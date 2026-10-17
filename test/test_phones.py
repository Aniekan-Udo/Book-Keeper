import pytest

from tools.phones import display_phone, normalize_nigerian_phone


@pytest.mark.parametrize("raw", ["0803 123 4567", "08031234567", "2348031234567", "+234 803 123 4567"])
def test_every_way_nigerians_write_a_number_gives_one_e164(raw):
    assert normalize_nigerian_phone(raw) == "+2348031234567"


@pytest.mark.parametrize("raw", ["0803", "12345678901", "2349000000000", "+447911123456", "abc", ""])
def test_numbers_that_are_not_real_nigerian_numbers_are_refused(raw):
    with pytest.raises(ValueError, match="valid Nigerian phone number"):
        normalize_nigerian_phone(raw)


def test_display_is_the_local_form():
    assert display_phone("+2348031234567") == "0803 123 4567"
