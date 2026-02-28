import unicodedata

def normalize_text(text: str) -> str:
    """
    Normalizes text by converting to lower case, stripping whitespace, and collapsing internal whitespace and newlines.
    """
    if not text:
        return ""
    return " ".join(text.lower().split())

def is_zalgo_text(text: str, min_diacritics: int, ratio_threshold: float) -> bool:
    """
    Проверяет, является ли текст Zalgo, анализируя количество и соотношение
    комбинированных диакритических знаков к базовым символам.
    Текст считается Zalgo, если он превышает и минимальное количество, и пороговое соотношение.
    """
    if not text:
        return False

    # NFD-нормализация разделяет символы типа 'é' на 'e' и '´'.
    # Это дает более точный подсчет базовых символов и диакритических знаков.
    try:
        normalized_text = unicodedata.normalize('NFD', text)
    except TypeError:
        return False

    diacritics_count = 0
    base_chars_count = 0
    for char in normalized_text:
        # Проверяем по самым распространенным Unicode категориям для комбинированных символов
        if unicodedata.category(char) in ('Mn', 'Mc', 'Me'):
            diacritics_count += 1
        else:
            base_chars_count += 1

    if diacritics_count < min_diacritics:
        return False

    if base_chars_count == 0:
        return diacritics_count > 0

    ratio = diacritics_count / base_chars_count
    
    return ratio >= ratio_threshold