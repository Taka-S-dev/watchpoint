"""SharePoint addresses: the file's own one is used, links made for a browser are explained, failures say what next."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from watchpoint.config import Target
from watchpoint.editing import describe_source
from watchpoint.fetchers.office import _office_timeout, _open_failed
from watchpoint.sharepoint import classify

FILE = "https://c.sharepoint.com/sites/A/Shared%20Documents/%E8%B3%AA%E5%95%8F.xlsx"


@pytest.mark.parametrize(
    ("url", "form", "file_url"),
    [
        (FILE, "file", FILE),
        (FILE + "?web=1", "file", FILE),  # opened in the browser otherwise
        (
            "https://c.sharepoint.com/:x:/r/sites/A/Shared%20Documents/%E8%B3%AA%E5%95%8F.xlsx?d=w12&csf=1&web=1&e=Ab",
            "sharing",
            FILE,
        ),
        ("https://c.sharepoint.com/:x:/s/A/EaBcDeF?e=xyz", "sharing", None),
        ("https://c.sharepoint.com/:w:/g/personal/u/EaBc", "sharing", None),
        (
            "https://c.sharepoint.com/sites/A/_layouts/15/Doc.aspx?sourcedoc=%7B1A2B%7D&file=q.xlsx&action=default",
            "browser",
            None,
        ),
        (  # on-premises SharePoint names the file in sourcedoc
            "https://sp.example.com/sites/A/_layouts/15/WopiFrame.aspx?sourcedoc=/sites/A/Shared%20Documents/q.xlsx",
            "browser",
            "https://sp.example.com/sites/A/Shared%20Documents/q.xlsx",
        ),
    ],
)
def test_addresses_are_told_apart(url: str, form: str, file_url: str | None) -> None:
    a = classify(url)
    assert a is not None and (a.form, a.file_url) == (form, file_url)


@pytest.mark.parametrize(
    "url",
    [
        "https://c.sharepoint.com/sites/A/SitePages/Home.aspx",  # a page, watched as a web page
        "https://c.sharepoint.com/:f:/r/sites/A/Shared%20Documents",  # a folder link
        "https://example.com/files/q.xlsx",
        "https://wiki.example.com/display/SP/sharepoint",
    ],
)
def test_other_addresses_are_left_alone(url: str) -> None:
    assert classify(url) is None


def test_a_sharing_link_in_the_config_names_the_file_to_write_instead() -> None:
    link = "https://c.sharepoint.com/:x:/r/sites/A/Shared%20Documents/%E8%B3%AA%E5%95%8F.xlsx?d=w12&web=1"
    with pytest.raises(ValidationError, match="共有リンク") as e:
        Target(name="a", source=link)
    assert FILE in str(e.value)


def test_a_browser_view_in_the_config_says_how_to_find_the_file() -> None:
    # it used to be taken for a web page, and the sign-in page would have been compared
    view = "https://c.sharepoint.com/sites/A/_layouts/15/Doc.aspx?sourcedoc=%7B1A2B%7D&file=q.xlsx"
    with pytest.raises(ValidationError, match="パスのコピー"):
        Target(name="a", source=view)


def test_the_file_address_is_fetched_through_office() -> None:
    assert Target(name="a", source=FILE + "?web=1").kind == "office"


def test_the_form_offers_the_file_address_for_a_link() -> None:
    d = describe_source("https://c.sharepoint.com/:x:/r/sites/A/Shared%20Documents/%E8%B3%AA%E5%95%8F.xlsx?web=1")
    assert d["kind"] is None and d["link"]["file_url"] == FILE and "共有リンク" in d["link"]["advice"]
    d = describe_source("https://c.sharepoint.com/:x:/s/A/EaBcDeF")
    assert d["link"]["file_url"] is None and "パスのコピー" in d["link"]["advice"]
    assert "link" not in describe_source(FILE)


class _ComError(Exception):
    """The shape of pywintypes.com_error: (hresult, text, excepinfo, argerr)."""

    def __init__(self, description: str):
        super().__init__(
            -2147352567, "例外が発生しました。", (0, "Microsoft Excel", description, None, 0, -2146827284), None
        )


@pytest.mark.parametrize(
    ("said", "cause", "next_step"),
    [
        ("申し訳ございません。q.xlsx が見つかりません。", "見つかりません", "パスのコピー"),
        ("Sorry, we couldn't find q.xlsx.", "見つかりません", "パスのコピー"),
        ("このファイルにアクセスする権限がありません。", "権限がありません", "アカウント"),
        ("何かがうまくいきませんでした。", "開けませんでした", "ショートカットを追加"),
    ],
)
def test_an_open_failure_says_the_likely_cause_and_what_next(said: str, cause: str, next_step: str) -> None:
    msg = str(_open_failed("Excel.Application", FILE, _ComError(said)))
    first, then = msg.split("\n次に: ")
    assert cause in first and said in first and FILE in first
    assert next_step in then and "{app}" not in then


def test_a_timeout_points_to_the_office_sign_in_and_to_syncing() -> None:
    msg = str(_office_timeout(120, "Excel.Application", FILE))
    assert "サインイン" in msg and "［ファイル］→［アカウント］" in msg and "ショートカットを追加" in msg
