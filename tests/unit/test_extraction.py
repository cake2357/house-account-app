import json
import math
from pathlib import Path

import pytest

from house_account.domain.ocr import OcrBlock, OcrResult
from house_account.ocr.extraction import extract_candidates
from house_account.ocr.serialization import result_from_dict
from tests.conftest import make_block


def test_fixture_groups_coordinates_removes_prices_and_retains_duplicate_items(
    fixture_dir: Path,
) -> None:
    fixture = json.loads((fixture_dir / "ocr_result.json").read_text(encoding="utf-8"))
    result = result_from_dict(fixture["result"])
    candidates = extract_candidates(result)
    assert [candidate.name for candidate in candidates] == fixture["expected_names"]
    assert len({candidate.id for candidate in candidates}) == len(candidates)
    assert candidates[0].block_indexes == (2, 0)
    assert candidates[-1].review_reasons == ("price_not_found", "low_confidence")
    assert "小計" in result.raw_text
    assert candidates == extract_candidates(result)


@pytest.mark.parametrize(
    "text",
    [
        "小計 ¥1,000",
        "合 計 1,000円",
        "内消費税 80",
        "8%対象 1,000",
        "現金 1,000",
        "お預り 1,000",
        "お釣り 100",
        "支払金額 1,000",
        "2026/10/31 12:00",
        "2026年10月31日",
        "12:30",
        "TEL 03-1234-5678",
        "領収書",
        "¥198",
        "(商品代金 ¥148)",
        "(値引合計 -68)",
        "税率 8%対象 -68",
        "值引額 -20",
        "**值引* -20",
        "(值引合計 -20)",
        "税率8%对象 -20",
        "",
    ],
)
def test_metadata_and_price_only_lines_are_excluded(text: str) -> None:
    assert not extract_candidates(OcrResult((make_block(text, 0),), {}, {}))


@pytest.mark.parametrize(
    ("text", "expected", "needs_review"),
    [
        ("牛乳 ¥198", "牛乳", False),
        ("牛乳￥１９８", "牛乳", False),
        ("牛乳198円", "牛乳", False),
        ("おいしい牛乳 198*", "おいしい牛乳", False),
        ("合計満足パン 198", "合計満足パン", False),
        ("税込みそ 198", "税込みそ", False),
        ("電話のおもちゃ 198", "電話のおもちゃ", False),
        ("緑茶500ml", "緑茶500ml", True),
        ("ビタミンC1000", "ビタミンC1000", True),
        ("商品名だけ", "商品名だけ", True),
    ],
)
def test_names_are_not_inferred_or_excluded_by_substring(
    text: str, expected: str, needs_review: bool
) -> None:
    (candidate,) = extract_candidates(OcrResult((make_block(text, 0),), {}, {}))
    assert candidate.name == expected
    assert candidate.needs_review == needs_review


@pytest.mark.parametrize(("confidence", "needs_review"), [(0.80, False), (0.799, True)])
def test_confidence_boundary(confidence: float, needs_review: bool) -> None:
    result = OcrResult((make_block("りんご 100", 0, confidence=confidence),), {}, {})
    assert extract_candidates(result)[0].needs_review == needs_review


def test_price_and_low_confidence_on_separate_blocks() -> None:
    result = OcrResult(
        (
            make_block("パン", 0),
            make_block("¥198", 2, x=300, confidence=0.70),
            make_block("パン", 30),
            make_block("¥198", 31, x=300),
        ),
        {},
        {},
    )
    candidates = extract_candidates(result)
    assert [candidate.name for candidate in candidates] == ["パン", "パン"]
    assert candidates[0].review_reasons == ("low_confidence",)
    assert not candidates[1].needs_review


def test_empty_result_is_successfully_empty() -> None:
    assert extract_candidates(OcrResult((), {}, {})) == ()


def test_item_section_keeps_unknown_lines_and_excludes_known_metadata() -> None:
    texts = [
        "架空スーパー",
        "お知らせの説明文",
        "牛乳 ¥198",
        "商品名のみ",
        "3コ×単104 ¥312",
        "値引額 -20",
        "合計満足パン 100",
        "割引前合計 ¥590",
        "食料品割引 -10",
        "広告文",
    ]
    result = OcrResult(
        tuple(make_block(text, index * 30) for index, text in enumerate(texts)), {}, {}
    )
    candidates = extract_candidates(result)
    assert [candidate.name for candidate in candidates] == [
        "架空スーパー",
        "牛乳",
        "商品名のみ",
        "合計満足パン",
    ]
    assert candidates[0].review_reasons == ("price_not_found",)
    assert candidates[2].review_reasons == ("price_not_found",)
    assert "広告文" in result.raw_text


def test_first_item_with_quantity_price_on_next_line_is_kept() -> None:
    result = OcrResult(
        tuple(
            make_block(text, index * 30)
            for index, text in enumerate(["架空店", "牛乳", "2コ×単100 ¥200", "合計 ¥200"])
        ),
        {},
        {},
    )
    header, candidate = extract_candidates(result)
    assert header.name == "架空店"
    assert header.review_reasons == ("price_not_found",)
    assert candidate.name == "牛乳"
    assert candidate.needs_review


@pytest.mark.parametrize(
    ("texts", "expected_names", "unpriced_count"),
    [
        (["牛乳", "食パン 198", "合計 398"], ["牛乳", "食パン"], 1),
        (["架空店", "牛乳", "パン 100", "合計 298"], ["架空店", "牛乳", "パン"], 2),
        (
            ["架空店", "牛乳", "食パン", "2コ×単100 ¥200", "合計 ¥200"],
            ["架空店", "牛乳", "食パン"],
            3,
        ),
    ],
)
def test_unpriced_leading_lines_are_retained_for_review(
    texts: list[str], expected_names: list[str], unpriced_count: int
) -> None:
    result = OcrResult(
        tuple(make_block(text, index * 30) for index, text in enumerate(texts)), {}, {}
    )
    candidates = extract_candidates(result)
    assert [candidate.name for candidate in candidates] == expected_names
    assert all(candidate.needs_review for candidate in candidates[:unpriced_count])
    assert [candidate.review_reasons for candidate in candidates] == [
        ("price_not_found",)
    ] * unpriced_count + [()] * (len(candidates) - unpriced_count)


@pytest.mark.parametrize(
    "total",
    ["合計 448", "合 計 ４４８円", "小計 ¥448", "総計￥448", "割引前合計 ¥448", "合計: 448"],
)
def test_total_requires_a_complete_label_and_amount(total: str) -> None:
    blocks = (
        make_block("牛乳 198", 0),
        make_block("合計", 30),
        make_block("満足パン", 30, x=140),
        make_block("100", 30, x=300),
        make_block("りんご 150", 60),
        make_block(total, 90),
        make_block("会計後の説明", 120),
    )
    result = OcrResult(blocks, {}, {})
    candidates = extract_candidates(result)
    assert [candidate.name for candidate in candidates] == ["牛乳", "合計 満足パン", "りんご"]
    assert candidates[1].block_indexes == (1, 2, 3)
    assert all(not candidate.needs_review for candidate in candidates)
    assert "会計後の説明" in result.raw_text


def test_slanted_receipt_pairs_names_and_prices_by_adjusted_coordinates() -> None:
    blocks = (
        OcrBlock("牛乳", ((0, 0), (120, 12), (120, 32), (0, 20)), 0.99),
        OcrBlock("¥198", ((400, 40), (520, 52), (520, 72), (400, 60)), 0.99),
        OcrBlock("りんご", ((0, 45), (120, 57), (120, 77), (0, 65)), 0.99),
        OcrBlock("¥100", ((400, 85), (520, 97), (520, 117), (400, 105)), 0.99),
    )
    candidates = extract_candidates(OcrResult(blocks, {}, {}))
    assert [candidate.name for candidate in candidates] == ["牛乳", "りんご"]
    assert all(not candidate.needs_review for candidate in candidates)


@pytest.mark.parametrize("confidence", [-0.1, 1.1, math.nan, math.inf])
def test_invalid_confidence_is_rejected(confidence: float) -> None:
    with pytest.raises(ValueError, match="信頼度"):
        make_block("商品", 0, confidence=confidence)
